# coding: utf-8

import json
import logging
from django.conf import settings
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from django.core.urlresolvers import reverse
from payments.models import YandexPayment
from plp.models import CourseSession
from plp.notifications.base import get_host_url
from plp_edmodule.models import EducationalModule
from .models import UpsaleLink, ObjectEnrollment
from .utils import payment_for_user, client


@login_required
def op_payment_view(request):
    session_id = request.GET.get('course_session_id', '')
    module_id = request.GET.get('edmodule_id', '')
    only_first_course = bool(request.GET.get('only_first_course', False))
    if bool(session_id) == bool(module_id):
        # ожидаем или course_session_id или module_id
        raise Http404
    if (session_id and not session_id.isdigit()) or (module_id and not module_id.isdigit()):
        raise Http404

    obj_model = CourseSession if session_id else EducationalModule
    obj_id = session_id or module_id
    obj = get_object_or_404(obj_model, id=obj_id)
    verified_enrollment = obj.get_verified_mode_enrollment_type()
    if not verified_enrollment:
        raise Http404
    upsale_link_ids = [i for i in request.GET.getlist('upsale_link_ids') if i.isdigit()]
    upsale_links = UpsaleLink.objects.filter(id__in=upsale_link_ids, is_active=True)
    upsales = []
    for upsale in upsale_links:
        s = upsale.content_object
        if s and isinstance(s, obj_model) and s.id == obj.id:
            upsales.append(upsale)

    first_session_id = None
    if session_id:
        obj_price = verified_enrollment.price
    else:
        if only_first_course:
            try:
                session, price = obj.get_first_session_to_buy(request.user)
            except TypeError:
                return HttpResponseServerError()
            obj_price = price
            first_session_id = session.id
        else:
            obj_price = obj.get_price_list(request.user)['whole_price']
    total_price = obj_price + sum([i.get_payment_price() for i in upsales])

    if request.method == 'POST' and request.is_ajax():
        # действительно создаем платеж только перед отправкой
        if total_price:
            payment_for_user(request.user, verified_enrollment, upsales, total_price,
                             only_first_course=only_first_course, first_session_id=first_session_id)
        return JsonResponse({'status': 0})

    payment = payment_for_user(request.user, verified_enrollment, upsales, total_price, create=False,
                               only_first_course=only_first_course, first_session_id=first_session_id)
    host_url = get_host_url(request)
    payment_fail = host_url + reverse('op_payment_status', kwargs={
        'status': 'fail',
        'obj_id': obj.id,
        'user_id': request.user.id,
        'payment_type': 'session' if session_id else 'edmodule',
    })
    payment_success = host_url + reverse('op_payment_status', kwargs={
        'status': 'success',
        'obj_id': obj.id,
        'user_id': request.user.id,
        'payment_type': 'session' if session_id else 'edmodule',
    })

    context = {
        'upsale_links': upsales,
        'total_price': total_price,
        'fields': {
            "shopId": settings.YANDEX_MONEY_SHOP_ID,
            "scid": settings.YANDEX_MONEY_SCID,
            "orderNumber": payment.order_number,
            "customerNumber": payment.customer_number,
            "sum": payment.order_amount,
            "cps_email": request.user.email,
            "cps_phone": "",
            "shopFailURL": payment_fail,
            "shopSuccessURL": payment_success
        },
        'shop_url': settings.YANDEX_MONEY_SHOP_URL,
    }
    if session_id:
        context['session'] = obj
    else:
        context['module'] = obj
    return render(request, 'opro_payments/op_payment.html', context)


@csrf_exempt
@login_required
def op_payment_status(request, payment_type, obj_id, user_id, status):
    # TODO: payment_type == edmodule
    # не показываем чужие промокоды
    if str(request.user.id) != user_id:
        raise Http404

    template_path = "profile/payment_{}.html".format(status)

    session = get_object_or_404(CourseSession, id=obj_id)
    user = request.user

    context = {
        'session': session,
    }

    if status == 'success':
        order_number = "{}-{}-{}-".format('verified', session.id, user.id)
        # считаем, что к моменту перехода на страницу подтверждения оплаты, нам пришел ответ от Яндекса
        # и были созданы "записи на объекты", иначе пользователь не увидит промокоды
        payment = YandexPayment.objects.filter(order_number__startswith=order_number).order_by('-id').first()
        if not payment:
            raise Http404
        if not payment.is_payed:
            logging.error('User %s was redirected to successfull payment page before payment %s was processed' % (
                user.id, payment.id
            ))
            if client:
                client.captureMessage('User was redirected to successfull payment page before payment was processed',
                                      extra={'user_id': user.id, 'payment_id': payment.id})
        metadata = json.loads(payment.metadata or '{}')
        upsale_links = metadata.get('upsale_links', [])
        upsales = UpsaleLink.objects.filter(id__in=upsale_links)
        object_enrollments = ObjectEnrollment.objects.filter(user=user, upsale__id__in=upsale_links)
        promocodes = []
        for obj in object_enrollments:
            data = obj.jsonfield or {}
            promo = data.get('promo_code')
            if promo:
                promocodes.append((obj.upsale.upsale.title, promo))
        context.update({
            'promocodes': promocodes,
            'upsale_links': upsales,
            'shop_url': getattr(settings, 'OPRO_PAYMENT_SHOP_URL', ''),
        })

    return render(request, template_path, context)
