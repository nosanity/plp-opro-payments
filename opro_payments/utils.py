# coding: utf-8

import json
import logging
import time
from django.conf import settings
from django.utils import timezone
from raven import Client
from payments.helpers import payment_for_participant_complete
from payments.models import YandexPayment
from payments.sources.yandex_money.signals import payment_completed
from plp.models import Participant, EnrollmentReason, SessionEnrollmentType, User
from plp.utils.edx_enrollment import EDXEnrollmentError
from plp_edmodule.models import EducationalModuleEnrollmentType, EducationalModuleEnrollment, \
    EducationalModuleEnrollmentReason
from .models import UpsaleLink, ObjectEnrollment

RAVEN_CONFIG = getattr(settings, 'RAVEN_CONFIG', {})
client = None

if RAVEN_CONFIG:
    client = Client(RAVEN_CONFIG.get('dsn'))


def payment_for_user(user, enrollment_type, upsale_links, price, create=True, only_first_course=False):
    assert enrollment_type.active == True
    # Яндекс-Касса не даст провести оплату два раза по одному и тому же order_number
    upsales = '-'.join([str(i.id) for i in upsale_links])
    if isinstance(enrollment_type, SessionEnrollmentType):
        order_number = "{}-{}-{}-{}".format(enrollment_type.mode, enrollment_type.session.id, user.id, upsales)
    else:
        order_number = "edmodule-{}-{}-{}-{}".format(
            enrollment_type.module.id, user.id, int(time.time()), upsales)
    if len(order_number) > 64:
        logging.info('Order number exceeds max length: %s' % order_number)
        order_number = order_number[:64]

    metadata = {
        'user': {
            'id': user.id,
            'username': user.username
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

    user = metadata.get('user')
    new_mode = metadata.get('new_mode')
    upsale_links = metadata.get('upsale_links')
    edmodule = metadata.get('edmodule')

    if (user and new_mode and upsale_links is not None):
        return _payment_for_session_complete(payment, metadata, user, new_mode, upsale_links)
    elif (user and edmodule and upsale_links is not None):
        return _payment_for_module_complete(payment, metadata, user, edmodule, upsale_links)


def _payment_for_session_complete(payment, metadata, user, new_mode, upsale_links):
    logging.info('[payment_for_user_complete] got payment information from yandex.kassa: metadata=%s payment=%s',
                 metadata, payment)

    enr_type = SessionEnrollmentType.objects.get(id=new_mode['id'])
    session = enr_type.session
    user = User.objects.get(id=user['id'])
    participant, created = Participant.objects.get_or_create(session=session, user=user)

    upsales = UpsaleLink.objects.filter(id__in=upsale_links)
    for u in upsales:
        ObjectEnrollment.objects.update_or_create(
            user=user,
            upsale=u,
            defaults={
                'enrollment_type': ObjectEnrollment.ENROLLMENT_TYPE_CHOICES.paid,
                'payment_type': ObjectEnrollment.PAYMENT_TYPE_CHOICES.yandex,
                'payment_order_id': payment.order_number,
                'is_active': True,
            }
        )

    params = dict(
        participant=participant,
        session_enrollment_type=enr_type,
        payment_type=EnrollmentReason.PAYMENT_TYPE.YAMONEY,
        payment_order_id=payment.order_number,
    )
    if not EnrollmentReason.objects.filter(**params).exists():
        try:
            EnrollmentReason.objects.create(**params)
            Participant.objects.filter(id=participant.id).update(sent_to_edx=timezone.now())
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

    logging.debug('[payment_for_user_complete] participant=%s new_mode=%s', participant.id, new_mode['mode'])


def _payment_for_module_complete(payment, metadata, user, edmodule, upsale_links):
    logging.info('[payment_for_user_complete] got payment information from yandex.kassa: metadata=%s payment=%s',
                 metadata, payment)

    enr_type = EducationalModuleEnrollmentType.objects.get(module__id=edmodule['id'], mode=edmodule['mode'])
    module = enr_type.module
    user = User.objects.get(id=user['id'])
    enrollment, created = EducationalModuleEnrollment.objects.update_or_create(
        module=module, user=user, defaults={'is_paid': True, 'is_active': True})

    upsales = UpsaleLink.objects.filter(id__in=upsale_links)
    for u in upsales:
        ObjectEnrollment.objects.update_or_create(
            user=user,
            upsale=u,
            defaults={
                'enrollment_type': ObjectEnrollment.ENROLLMENT_TYPE_CHOICES.paid,
                'payment_type': ObjectEnrollment.PAYMENT_TYPE_CHOICES.yandex,
                'payment_order_id': payment.order_number,
                'is_active': True,
            }
        )
    EducationalModuleEnrollmentReason.objects.get_or_create(
        enrollment=enrollment,
        module_enrollment_type=enr_type,
        payment_type=EducationalModuleEnrollmentReason.PAYMENT_TYPE.YAMONEY,
        payment_order_id=payment.order_number,
        full_paid=not edmodule['only_first_course']
    )

    if edmodule['only_first_course']:
        course, session = module.get_closest_course_with_session()
        participant, created = Participant.objects.get_or_create(session=session, user=user)
        session_enr_type = SessionEnrollmentType.objects.get(session=session, mode='verified')
        params = dict(
            participant=participant,
            session_enrollment_type=session_enr_type,
            payment_type=EnrollmentReason.PAYMENT_TYPE.OTHER,
        )
        if not EnrollmentReason.objects.filter(**params).exists():
            try:
                EnrollmentReason.objects.create(**params)
                Participant.objects.filter(id=participant.id).update(sent_to_edx=timezone.now())
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


payment_completed.disconnect(payment_for_participant_complete)
payment_completed.connect(payment_for_user_complete)
