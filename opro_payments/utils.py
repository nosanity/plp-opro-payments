# coding: utf-8

import json
import logging
import re
import time
import urllib
from django.conf import settings
from django.utils import timezone
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from django.shortcuts import get_object_or_404
from django.core.exceptions import ObjectDoesNotExist
from django.template.loader import render_to_string
from django.utils.translation import ugettext as _
import requests
from raven import Client
from payments.helpers import payment_for_participant_complete
from payments.models import YandexPayment
from payments.sources.yandex_money.signals import payment_completed
from plp.models import Course, Participant, EnrollmentReason, SessionEnrollmentType, User, CourseSession, GiftPaymentInfo
from plp.utils.edx_enrollment import EDXEnrollmentError
from plp.utils.webhook import ZapierInformer
from plp_edmodule.models import EducationalModuleEnrollmentType, EducationalModuleEnrollment, \
    EducationalModuleEnrollmentReason, EducationalModule, PromoCode
from plp_edmodule.signals import edmodule_payed
from plp.notifications.base import get_host_url
from .models import UpsaleLink, ObjectEnrollment

# Стандартные значения для Яндекс.Кассы для передачи оператору фискальных данных
TAX_RATE = 1 # Без НДС
QUANTITY = 1 # Товар всегда продается в единичном экземпляре

RAVEN_CONFIG = getattr(settings, 'RAVEN_CONFIG', {})
client = None

if RAVEN_CONFIG:
    client = Client(RAVEN_CONFIG.get('dsn'))

def get_merchant_receipt(contact, products):
    items = []
    for product in products:
        items.append({
            'quantity': QUANTITY,
            'price': {
                "amount": "%.2f" % float(product['price'])
            },
            "tax": TAX_RATE,
            "text": product['title']
        })

    receipt = {
        'customerContact': contact,
        'items': items
    }

    return receipt

def get_object_info(request, session_id, module_id):
    obj_model = CourseSession if session_id else EducationalModule
    obj_id = session_id or module_id
    obj = get_object_or_404(obj_model, id=obj_id)
    verified_enrollment = obj.get_verified_mode_enrollment_type()

    upsale_link_ids = [i for i in request.GET.getlist('upsale_link_ids') if i.isdigit()]
    upsale_links = UpsaleLink.objects.filter(id__in=upsale_link_ids, is_active=True)
    upsales = []
    for upsale in upsale_links:
        s = upsale.content_object
        if s and isinstance(s, obj_model) and s.id == obj.id:
            upsales.append(upsale)    

    return obj, verified_enrollment, upsales

def get_obj_price(session_id, verified_enrollment, only_first_course, obj, upsales, new_price=None):
    session = None
    first_session_id = None
    products = []
    if session_id:
        obj_price = verified_enrollment.price
        products.append({ 
            'title': verified_enrollment.session.course.title, 
            'price': obj_price 
        })
    else:
        if only_first_course:
            try:
                session, price = obj.get_first_session_to_buy(None)
                obj_price = price
                first_session_id = session.id
                products.append({
                    'title': session.course.title,
                    'price': obj_price 
                })
            except TypeError:
                return HttpResponseServerError()
        else:
            obj_price = obj.get_price_list()['whole_price']
            products.append({
                'title': obj.title, 
                'price': obj_price 
            })
            
    if new_price:
        obj_price = new_price
        products[0]['price'] = new_price

    upsales_price = 0
    for i in upsales:
        upsale_price = i.get_payment_price()
        upsales_price += upsale_price
        products.append({
            'title': i.upsale.title,
            'price': upsale_price
        })

    total_price = float(obj_price) + upsales_price

    return session, first_session_id, obj_price, total_price, products

def get_or_create_user(first_name, email, lazy_send_mail=False):
    """
    Возвращает пользователя, если его нет - создает
    Для прохождения упрощенного сценарция задает пользователю переданное имя и пустую фамилию
    """

    post_data = { 'emails': [email], 'lazy_send_mail': lazy_send_mail }
    request_url = '{}/users/simple_mass_registration/'.format(settings.SSO_NPOED_URL)
    r = requests.post(
        request_url,
        json = post_data,
        headers = { 'X-SSO-Api-Key': settings.SSO_API_KEY },
        timeout = settings.CONNECTION_TIMEOUT
    )

    sso_data = r.json().get('users', [])
    email = sso_data[0]['email']

    user = User.objects.get(email=email)
    user.username = re.sub('[^a-zA-Z0-9]', '_', email)
    user.first_name = first_name
    user.last_name = ' '
    user.save()

    return user

def get_payment_urls(request, obj, user, session_id, utm_data):
    """
    Возвращает значения для редиректа пользователя после успешной / неуспешной оплаты
    """

    host_url = get_host_url(request)
    payment_fail = host_url + reverse('landing_op_payment_status', kwargs={
        'status': 'fail',
        'obj_id': obj.id,
        'user_id': user.id,
        'payment_type': 'session' if session_id else 'edmodule',
    })
    payment_success = host_url + reverse('landing_op_payment_status', kwargs={
        'status': 'success',
        'obj_id': obj.id,
        'user_id': user.id,
        'payment_type': 'session' if session_id else 'edmodule',
    })
    if utm_data:
        payment_success = '{}?{}'.format(payment_success, utm_data)

    urls = {
        'payment_fail': payment_fail,
        'payment_success': payment_success
    }

    return urls

def get_gift_payment_urls(request, obj, user, session_id, utm_data):
    """
    Возвращает значения для редиректа пользователя после успешной / неуспешной оплаты
    """

    host_url = get_host_url(request)
    payment_fail = host_url + reverse('gift_op_payment_status', kwargs={
        'status': 'fail',
        'obj_id': obj.id,
        'user_id': user.id,
        'payment_type': 'session' if session_id else 'edmodule',
    })
    payment_success = host_url + reverse('gift_op_payment_status', kwargs={
        'status': 'success',
        'obj_id': obj.id,
        'user_id': user.id,
        'payment_type': 'session' if session_id else 'edmodule',
    })
    if utm_data:
        payment_success = '{}?{}'.format(payment_success, utm_data)

    urls = {
        'payment_fail': payment_fail,
        'payment_success': payment_success
    }

    return urls

def payment_for_user(request, enrollment_type, upsale_links, price, create=True, only_first_course=False,
                     first_session_id=None, order_number=None, user=None, gift_receiver=None, promocode=None):
    """
    Создание объекта YandexPayment для пользователя с сохранением в бд или без
    :param request: объект request
    :param enrollment_type: SessionEnrollmentType или EducationalModuleEnrollmentType
    :param upsale_links: список UpsaleLink
    :param price: int
    :param create: bool - сохранять созданый объект или нет
    :param promocode: str - промокод, по которому была совершена оплата
    :param only_first_course: bool - используется в случае оплаты модуля
    :param first_session_id: int - обязательный аргумент в случае only_first_course=True
    :param order_number: str - взять заданный order_number вместо его генерации (действует только для модуля)
    :return: YandexPayment
    """
    assert enrollment_type.active == True
    user = user if user else request.user
    # Яндекс-Касса не даст провести оплату два раза по одному и тому же order_number
    upsales = '-'.join([str(i.id) for i in upsale_links])
    if isinstance(enrollment_type, SessionEnrollmentType):
        order_number = "{}-{}-{}-{}".format(enrollment_type.mode, enrollment_type.session.id, user.id, upsales)
    else:
        if create and order_number:
            # запись ранее сгенерированного order_number во избежание ошибки несовпадения этого параметра
            # вследствие использования int(time.time()) как часть order_number
            order_number = order_number
        else:
            order_number = "edmodule-{}-{}-{}-{}".format(
                enrollment_type.module.id, user.id, int(time.time()), upsales)
    if len(order_number) > 64:
        logging.info('Order number exceeds max length: %s' % order_number)
        order_number = order_number[:64]

    metadata = {
        'user': {
            'id': user.id,
            'sso_id': user.sso_id,
            'username': user.username,
            'first_name': user.first_name,
            'email': user.email
        },
        'upsale_links': [i.id for i in upsale_links],
    }
    if isinstance(enrollment_type, SessionEnrollmentType):
        metadata['new_mode'] = {
            'id': enrollment_type.id,
            'mode': enrollment_type.mode
        }
    else:
        metadata['edmodule'] = {
            'id': enrollment_type.module.id,
            'mode': enrollment_type.mode,
            'only_first_course': only_first_course
        }
        if only_first_course:
            metadata['edmodule']['first_session_id'] = first_session_id

    # add google analytics
    if create:
        if isinstance(enrollment_type, SessionEnrollmentType):
            data = prepare_ga_data(order_number, request, price, enrollment_type.session)
        else:
            fsi = first_session_id if only_first_course else None
            data = prepare_ga_data(order_number, request, price, enrollment_type.module, fsi)
        metadata['google_analytics'] = data

    if promocode:
        metadata['promocode'] = promocode

    if gift_receiver:
        metadata['gift_receiver'] = {
            'id': gift_receiver.id,
            'first_name': gift_receiver.first_name,
            'email': gift_receiver.email
        }

    try:
        payment = YandexPayment.objects.get(order_number=order_number)
        if payment.order_amount != price:
            assert not payment.is_payed
            logging.warning(
                '[payment_for_user] price changed from %d to %d, updating order_amount for payment %s' %
                (payment.order_amount, price, payment)
            )
            payment.order_amount = price
            payment.save()
    except YandexPayment.DoesNotExist:
        payment_dict = dict(order_number=order_number,
                            order_amount=price,
                            customer_number=user.username,
                            metadata=json.dumps(metadata),
                            user=user)
        if create:
            payment = YandexPayment.objects.create(**payment_dict)
        else:
            payment = YandexPayment(**payment_dict)

    return payment


def payment_for_user_complete(sender, **kwargs):
    """
    Обработчик сигнала оплаты от яндекс-кассы.
    :param sender: объект models.YandexPayment

    """
    assert isinstance(sender, YandexPayment)
    payment = sender
    metadata = json.loads(payment.metadata or "{}")

    user = metadata.get('gift_receiver') if metadata.get('gift_receiver') else metadata.get('user')
    new_mode = metadata.get('new_mode')
    upsale_links = metadata.get('upsale_links')
    edmodule = metadata.get('edmodule')
    course_payment = True

    if (user and new_mode and upsale_links is not None):
        _payment_for_session_complete(payment, metadata, user, new_mode, upsale_links)
    elif (user and edmodule and upsale_links is not None):
        _payment_for_module_complete(payment, metadata, user, edmodule, upsale_links)
        course_payment = False
    push_google_analytics_for_payment(payment)
    ga_data = metadata.get('google_analytics', [])
    cid = ''
    if ga_data:
        cid = ga_data[0].get('cid')
    user = User.objects.get(id=user['id'])
    kwargs = {'cookie': cid, 'user': user, 'payment': payment}
    if course_payment:
        enr_type = SessionEnrollmentType.objects.get(id=new_mode['id'])
        session = enr_type.session
        ZapierInformer().push(ZapierInformer.ACTION.plp_course_pay, session=session, **kwargs)
    else:
        enr_type = EducationalModuleEnrollmentType.objects.get(module__id=edmodule['id'], mode=edmodule['mode'])
        module = enr_type.module
        ZapierInformer().push(ZapierInformer.ACTION.plp_edmodule_pay, module=module, **kwargs)


def outer_payment_for_user(user, sku_parts, new_mode, upsale_links):
    user_data = {'id': user.id}
    if sku_parts['type'] == 'course':
        _payment_for_session_complete(None, None, user_data, new_mode, upsale_links, with_yandex=False)
    elif sku_parts['type'] == 'edmodule':
        _payment_for_module_complete(None, None, user_data, new_mode, upsale_links, with_yandex=False)


def _payment_for_session_complete(payment, metadata, user, new_mode, upsale_links, with_yandex=True):
    if with_yandex:
        logging.info('[payment_for_user_complete] got payment information from yandex.kassa: metadata=%s payment=%s',
                     metadata, payment)

    enr_type = SessionEnrollmentType.objects.get(id=new_mode['id'])
    session = enr_type.session
    user = User.objects.get(id=user['id'])
    participant, created = Participant.objects.get_or_create(session=session, user=user)

    upsales = UpsaleLink.objects.filter(id__in=upsale_links)
    promocodes = []
    object_enrollment_defaults = {
        'enrollment_type': ObjectEnrollment.ENROLLMENT_TYPE_CHOICES.paid,
        'payment_type': ObjectEnrollment.PAYMENT_TYPE_CHOICES.yandex if with_yandex else ObjectEnrollment.PAYMENT_TYPE_CHOICES.other,
        'payment_order_id': payment.order_number if with_yandex else '',
        'is_active': True,
    }
    for u in upsales:
        obj, created = ObjectEnrollment.objects.update_or_create(
            user=user,
            upsale=u,
            defaults=object_enrollment_defaults
        )
        if created:
            data = obj.jsonfield or {}
            promo = data.get('promo_code')
            if promo:
                promocodes.append((u.upsale.title, promo))

    params = dict(
        participant=participant,
        session_enrollment_type=enr_type,
        payment_type=EnrollmentReason.PAYMENT_TYPE.YAMONEY if with_yandex else EnrollmentReason.PAYMENT_TYPE.OTHER,
        payment_order_id=payment.order_number if with_yandex else '',
    )
    if not EnrollmentReason.objects.filter(**params).exists():
        try:
            paid_for_session = EnrollmentReason.objects.filter(
                participant=participant,
                session_enrollment_type__mode='verified'
            ).exists()
            reason = EnrollmentReason.objects.create(**params)
            Participant.objects.filter(id=participant.id).update(sent_to_edx=timezone.now())
            if not metadata.get('gift_receiver'):
                try:
                    reason.send_confirmation_email(upsales=upsales, promocodes=promocodes,
                                                   paid_for_session=paid_for_session)
                except Exception as e:
                    logging.error(
                        u'Failed to send course payment message. '
                        u'User: %s, upsale_links: %s, enrollment_reason %s, error: %s' %
                        (user.email, upsale_links, reason.id, e))
        except EDXEnrollmentError as e:
            logging.error('Failed to push verified enrollment %s to edx for user %s: %s' % (
                session, user, e
            ))
            if client:
                client.captureMessage('Failed to push verified enrollment to edx', extra={
                    'user': user.username,
                    'session_id': session.id,
                    'error': str(e)
                })

    if metadata.get('gift_receiver'):
        gift_payment_info = GiftPaymentInfo.objects.filter(
            gift_receiver__id=metadata.get('gift_receiver').get('id'),
            gift_sender__id=metadata.get('user').get('id'),
            course_id=session.id
        )

        if len(gift_payment_info) == 1: 
            ctx = {
                'gift_receiver': metadata.get('gift_receiver').get('first_name'),
                'gift_receiver_email': metadata.get('gift_receiver').get('email'),
                'gift_sender': metadata.get('user').get('first_name'),
                'gift_sender_email': metadata.get('user').get('email'),
                'course_name': u'Дизайнер интерфейсов' if gift_payment_info[0].product == 'ux' else u'VR-разработчик'
            }
            try:
                send_mail(
                    _(u'Успешная оплата курса «{}» в подарок'.format(ctx['course_name'])),
                    render_to_string('emails/gift_sender.txt', ctx),
                    'OpenProfession <welcome@openprofession.ru>',
                    [user.email],
                    html_message=render_to_string('emails/gift_sender.html', ctx)
                )
            except Exception as e:
                logging.error(u'Failed to send message for gift receiver. Email: %s, ctx: %s, error: %s' %
                              (user.email, ctx, e))

            gift_payment_info[0].has_paid = True
            gift_payment_info[0].save() 

    logging.debug('[payment_for_user_complete] participant=%s new_mode=%s', participant.id, new_mode['mode'])


def _payment_for_module_complete(payment, metadata, user, edmodule, upsale_links, with_yandex=True):
    if with_yandex:
        logging.info('[payment_for_user_complete] got payment information from yandex.kassa: metadata=%s payment=%s',
                     metadata, payment)

    enr_type = EducationalModuleEnrollmentType.objects.get(module__id=edmodule['id'], mode=edmodule['mode'])
    module = enr_type.module
    user = User.objects.get(id=user['id'])
    enrollment, new_enrollment = EducationalModuleEnrollment.objects.update_or_create(
        module=module, user=user, defaults={'is_paid': True, 'is_active': True})

    upsales = UpsaleLink.objects.filter(id__in=upsale_links)
    promocodes, bought_upsales = [], []
    upsales_defaults = {
        'enrollment_type': ObjectEnrollment.ENROLLMENT_TYPE_CHOICES.paid,
        'payment_type': ObjectEnrollment.PAYMENT_TYPE_CHOICES.yandex if with_yandex else ObjectEnrollment.PAYMENT_TYPE_CHOICES.other,
        'payment_order_id': payment.order_number if with_yandex else '',
        'is_active': True,
    }
    for u in upsales:
        bought_upsale, _created = ObjectEnrollment.objects.update_or_create(
            user=user,
            upsale=u,
            defaults=upsales_defaults
        )
        if _created:
            bought_upsales.append(u)
            data = bought_upsale.jsonfield or {}
            promo = data.get('promo_code')
            if promo:
                promocodes.append((u.upsale.title, promo))
    edmodule_reason, created = EducationalModuleEnrollmentReason.objects.get_or_create(
        enrollment=enrollment,
        module_enrollment_type=enr_type,
        payment_type=EducationalModuleEnrollmentReason.PAYMENT_TYPE.YAMONEY if with_yandex else EducationalModuleEnrollmentReason.PAYMENT_TYPE.OTHER,
        payment_order_id=payment.order_number if with_yandex else '',
        full_paid=not edmodule['only_first_course']
    )
    try:
        edmodule_payed.send(EducationalModuleEnrollmentReason, instance=edmodule_reason,
                            new_enrollment=new_enrollment, promocodes=promocodes, upsale_links=bought_upsales)
    except Exception as e:
        logging.error(u'Failed to send edmodule payment message. '
                      u'User: %s, bought_upsales: %s, edmodule_reason: %s, error: %s' %
                      (user.email, bought_upsales, edmodule_reason.id, e))

    if edmodule['only_first_course'] and edmodule.get('first_session_id'):
        session = CourseSession.objects.get(id=edmodule['first_session_id'])
        participant, created = Participant.objects.get_or_create(session=session, user=user)
        session_enr_type = SessionEnrollmentType.objects.get(session=session, mode='verified')
        params = dict(
            participant=participant,
            session_enrollment_type=session_enr_type,
            payment_type=EnrollmentReason.PAYMENT_TYPE.YAMONEY if with_yandex else EnrollmentReason.PAYMENT_TYPE.OTHER,
            payment_order_id=payment.order_number if with_yandex else '',
        )
        if not EnrollmentReason.objects.filter(**params).exists():
            try:
                reason = EnrollmentReason.objects.create(**params)
                Participant.objects.filter(id=participant.id).update(sent_to_edx=timezone.now())
                if not metadata.get('gift_receiver'):
                    try:
                        reason.send_confirmation_email()
                    except Exception as e:
                        logging.error(u'Failed to send edmodule first course payment message. '
                                      u'User: %s, edmodule_reason: %s, reason: %s, error: %s' %
                                      (user.email, edmodule_reason.id, reason.id, e))
            except EDXEnrollmentError as e:
                logging.error('Failed to push verified enrollment %s to edx for user %s: %s' % (
                    session, user, e
                ))
                if client:
                    client.captureMessage('Failed to push verified enrollment to edx', extra={
                        'user': user.username,
                        'session_id': session.id,
                        'error': str(e)
                    })

    logging.debug('[payment_for_user_complete] enrollment=%s new_mode=%s', enrollment.id, edmodule['mode'])


def prepare_ga_data(order_number, request, price, obj, first_session_id=None):
    """
    Подготовка массива строк с данными для гугл аналитики
    """
    user_cookie = request.COOKIES.get('_ga', '') or ''
    user_cookie = re.search(r'[\d.]+$', user_cookie)
    if not user_cookie:
        return []
    user_cookie = user_cookie.group()
    split = user_cookie.split('.')
    if len(split) > 2: 
        user_cookie = "{}.{}".format(split[-2],split[-1]) 
    try:
        google_id = settings.GOOGLE_ANALYTICS_ID
    except AttributeError:
        if client:
            client.captureMessage('settings.GOOGLE_ANALYTICS_ID is not set')
        logging.error('settings.GOOGLE_ANALYTICS_ID is not set')
        return []
    params = {
        'v': '1',
        'tid': google_id,
        'cid': user_cookie,
        'ti': order_number,
        'cu': 'RUB',
    }
    data = []
    # transaction
    _params = params.copy()
    _params.update({
        't': 'transaction',
        'tr': price,
    })
    data.append(_params)
    # items
    if isinstance(obj, CourseSession):
        _params = params.copy()
        _params.update({
            't': 'item',
            'in': obj.course.title,
            'ic': obj.course.slug,
            'iv': 'course',
            'ip': obj.get_verified_mode_price(),
        })
        data.append(_params)
    elif isinstance(obj, EducationalModule):
        if first_session_id:
            session = CourseSession.objects.get(id=first_session_id)
            session_price = session.get_verified_mode_price()
            _params = params.copy()
            _params.update({
                't': 'item',
                'in': obj.title,
                'ic': obj.code,
                'iv': 'edmodule',
                'ip': session_price,
            })
            data.append(_params)

            _params = params.copy()
            session = CourseSession.objects.get(id=first_session_id)
            _params.update({
                't': 'item',
                'in': session.course.title,
                'ic': session.course.slug,
                'iv': 'course',
                'ip': session_price,
            })
            data.append(_params)
        else:
            price_data = obj.get_price_list()
            _params = params.copy()
            _params.update({
                't': 'item',
                'in': obj.title,
                'ic': obj.code,
                'iv': 'edmodule',
                'ip': price_data['whole_price'],
            })
            data.append(_params)
            for course, course_price in price_data.get('courses', []):
                if not course_price:
                    continue
                _params = params.copy()
                _params.update({
                    't': 'item',
                    'in': course.title,
                    'ic': course.slug,
                    'iv': 'course',
                    'ip': course_price,
                })
                data.append(_params)
    return data


def push_google_analytics_for_payment(payment):
    """
    получение массива строк гугл аналитики из данных платежа и отправка на сервер
    """
    def _prepare_str(params):
        return urllib.urlencode({k: unicode(v).encode('utf-8') for k, v in params.iteritems()})

    url = 'https://www.google-analytics.com/batch'
    metadata = json.loads(payment.metadata)
    data = metadata.get('google_analytics', [])
    data = map(_prepare_str, data)
    if data:
        try:
            requests.post(url, data=u'\n'.join(data), timeout=settings.CONNECTION_TIMEOUT)
        except requests.RequestException as e:
            if client:
                client.captureMessage('Failed to send google analytics data', extra={
                    'payment_id': payment.id,
                    'exception': str(e),
                })
                logging.error('Failed to send google analytics data for payment %s: %s' % (payment.id, e))


def increase_promocode_usage(promocode, payment_id):
    if promocode:
        try:
            obj = PromoCode.objects.get(code=promocode)
            obj.used += 1
            obj.save()
        except ObjectDoesNotExist:
            logging.error('Promocode %s wasn\'t found for payment %s' % (
                promocode, payment_id
            ))

payment_completed.disconnect(payment_for_participant_complete)
payment_completed.connect(payment_for_user_complete)
