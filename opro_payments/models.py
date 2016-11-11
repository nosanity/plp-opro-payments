# coding: utf-8

import os
import linecache
import logging
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.utils.translation import ugettext_lazy as _
from imagekit.models import ImageSpecField
from imagekit.processors import Resize
from jsonfield import JSONField


class Upsale(models.Model):
    ICON_THUMB_SIZE = (
        getattr(settings, 'UPSALE_ICON_SIZE', (100, 100))[0],
        getattr(settings, 'UPSALE_ICON_SIZE', (100, 100))[1]
    )
    IMAGE_THUMB_SIZE = (
        getattr(settings, 'UPSALE_IMAGE_SIZE', (180, 180))[0],
        getattr(settings, 'UPSALE_IMAGE_SIZE', (180, 180))[1]
    )
    slug = models.SlugField(verbose_name=_(u'Код'), unique=True)
    title = models.CharField(max_length=255, verbose_name=_(u'Название'))
    short_description = models.CharField(
        max_length=80,
        verbose_name=_(u'Короткое описание'),
        help_text=_(u'Отображается везде в интерфейсе рядом с названием')
    )
    description = models.CharField(
        max_length=400,
        verbose_name=_(u'Полное описание'),
        default='',
        blank=True,
        help_text=_(u'Отображается в раскрывающемся окне с подробной информацией по апсейлам')
    )
    additional_info = models.TextField(
        verbose_name=_(u'Дополнительная информация'),
        default='',
        blank=True,
        help_text=_(u'Дополнительная информация для отображения на странице продажи апсейлов. Возможно использование html тегов'))
    icon = models.ImageField(verbose_name=_(u'Иконка'), upload_to='upsale_icons')
    icon_thumbnail = ImageSpecField(source='icon', processors=[Resize(*ICON_THUMB_SIZE)])
    image = models.ImageField(verbose_name=_(u'Изображение услуги'), upload_to='upsale_images')
    image_thumbnail = ImageSpecField(source='image', processors=[Resize(*IMAGE_THUMB_SIZE)])
    max_per_session = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_(u'Максимальное количество услуг на 1 сессию'),
        help_text=_(u'0 - если нет ограничений. 1,2,3... - максимальное количество проданных апсейлов к 1 сессии курса')
    )
    price = models.PositiveIntegerField(verbose_name=_(u'Стоимость услуги'), default=0,
                                        help_text=_(u'0 - если бесплатно.'))
    discount_price = models.PositiveIntegerField(verbose_name=_(u'Стоимость услуги со скидкой'), blank=True, null=True,
                                                 help_text=_(u'Пусто - если нет скидки.'))
    days_to_buy = models.PositiveSmallIntegerField(
        verbose_name=_(u'В течение скольких дней с начала сессии возможно купить услугу на текущую сессию?'),
        blank=True, null=True
    )
    days_to_return = models.PositiveSmallIntegerField(
        verbose_name=_(u'За сколько дней до конца сессии прекращать возврат денег за услугу?'),
        blank=True, null=True
    )
    required = models.CommaSeparatedIntegerField(
        verbose_name=_(u'Необходимые услуги'),
        blank=True,
        null=True,
        max_length=100,
        help_text=_(u'Укажите id апсейлов, без которых текущий апсейл не может быть приобретен, через '
                    u'запятую без пробелов')
    )
    emails = models.CharField(
        verbose_name=_(u'Email ответственных за услугу'),
        blank=True,
        default='',
        max_length=255,
        help_text=_(u'Введите 1 или несколько адресов через запятую. По этим адресам будут '
                    u'приходить уведомления о записи на данную услугу с контактами пользователя.')
    )

    class Meta:
        verbose_name = _(u'Апсейл')
        verbose_name_plural = _(u'Апсейлы')

    def __unicode__(self):
        return u'%s - %s' % (self.slug, self.title)


class UpsaleLink(models.Model):
    class IS_PAID_CHOICES(object):
        free = 0
        paid = 1
        choices = (
            (free, 'Free'),
            (paid, 'Paid'),
        )
    limit_models = models.Q(app_label='plp', model='coursesession') | \
                   models.Q(app_label='plp_edmodule', model='EducationalModule')

    upsale = models.ForeignKey('Upsale', verbose_name=_(u'Апсейл'), related_name='upsale_links')
    content_type = models.ForeignKey(ContentType, limit_choices_to=limit_models,
                                     verbose_name=_(u'Тип объекта, к которому апсейл'))
    object_id = models.PositiveIntegerField(verbose_name=_(u'Объект, к которому апсейл'))
    content_object = GenericForeignKey('content_type', 'object_id')
    is_active = models.BooleanField(verbose_name=_(u'Апсейл активен'), default=True)
    is_paid = models.PositiveSmallIntegerField(verbose_name=_(u'Объект оплачивается'), choices=IS_PAID_CHOICES.choices)
    is_detachable = models.BooleanField(verbose_name=_(u'Отделяем от основного объекта'), default=False)
    price = models.PositiveIntegerField(verbose_name=_(u'Цена'), blank=True, null=True)
    discount_price = models.PositiveIntegerField(verbose_name=_(u'Стоимость услуги со скидкой'), blank=True, null=True)
    days_to_buy = models.PositiveSmallIntegerField(
        verbose_name=_(u'В течение скольких дней с начала сессии возможно купить услугу на текущую сессию?'),
        blank=True, null=True
    )
    days_to_return = models.PositiveSmallIntegerField(
        verbose_name=_(u'За сколько дней до конца сессии прекращать возврат денег за услугу?'),
        blank=True, null=True
    )
    additional_info = JSONField(blank=True, null=True,
                                help_text=_(u'json вида {"promo": {"file":"123.txt", "already_sent": 4}}. '
                                            u'"already_sent" - служебное поле, не менять'))

    def get_price(self):
        return self.price if self.price is not None else self.upsale.price

    def get_discount_price(self):
        return self.discount_price if self.discount_price is not None else self.upsale.discount_price

    def get_payment_price(self):
        price, discount = self.get_price(), self.get_discount_price()
        return discount if discount is not None else price

    class Meta:
        unique_together = ('content_type', 'object_id', 'upsale')

    def __unicode__(self):
        try:
            assert self.content_object is not None
            return u'%s - %s' % (self.upsale, self.content_object)
        except (ObjectDoesNotExist, AssertionError):
            return ''

    @classmethod
    def get_promocode_dir(cls):
        path = getattr(settings, 'UPSALE_PROMOCODES_DIR', None)
        if path is None:
            return os.path.dirname(os.path.dirname(settings.BASE_DIR))
        return path


class ObjectEnrollment(models.Model):
    class ENROLLMENT_TYPE_CHOICES(object):
        free = 0
        paid = 1
        choices = (
            (free, 'Free'),
            (paid, 'Paid'),
        )

    class PAYMENT_TYPE_CHOICES(object):
        none = 0
        yandex = 1
        other = 2
        choices = (
            (none, 'None'),
            (yandex, 'Yandex'),
            (other, 'Other'),
        )

    user = models.ForeignKey('plp.User', verbose_name=_(u'Пользователь'), related_name='bought_objects')
    upsale = models.ForeignKey('UpsaleLink', verbose_name=_(u'Запись на объект'), related_name='bought_objects')
    enrollment_type = models.PositiveSmallIntegerField(verbose_name=_(u'Тип записи'), choices=ENROLLMENT_TYPE_CHOICES.choices)
    payment_type = models.PositiveSmallIntegerField(verbose_name=_(u'Способ платежа'), choices=PAYMENT_TYPE_CHOICES.choices)
    payment_order_id = models.CharField(max_length=64, null=True, blank=True,
                                        verbose_name=_(u'Номер договора/заказа'))
    payment_descriptions = models.TextField(null=True, blank=True,
                                            verbose_name=_(u'Описание платежа\заказа'))
    is_active = models.BooleanField(verbose_name=_(u'Статус записи'))
    jsonfield = JSONField(blank=True, null=True)

    class Meta:
        verbose_name = _(u'Запись на объект')
        verbose_name_plural = _(u'Записи на объекты')

    def __unicode__(self):
        return u'%s - %s' % (self.user, self.upsale)

    def save(self, **kwargs):
        if self.id:
            super(ObjectEnrollment, self).save()
            return
        with transaction.atomic():
            info = UpsaleLink.objects.get(id=self.upsale_id).additional_info or {}
            promo_file = info.get('promo', {}).get('file')
            if promo_file:
                file_path = os.path.join(UpsaleLink.get_promocode_dir(), promo_file)
                if not os.path.exists(file_path):
                    logging.error('Upsale promocodes path does not exist: %s' % file_path)
                try:
                    line = int(info['promo'].get('already_sent')) + 1
                except (TypeError, ValueError):
                    line = 1
                code = linecache.getline(file_path, line).strip()
                if not code:
                    logging.error('Failed to read %s line from %s' % (line, file_path))
                else:
                    info['promo']['already_sent'] = line
                    self.jsonfield = {'promo_code': code}
                    self.upsale.additional_info = info
                    self.upsale.save()
            super(ObjectEnrollment, self).save()
