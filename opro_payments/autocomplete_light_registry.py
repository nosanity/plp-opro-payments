# coding: utf-8

from django.contrib.contenttypes.models import ContentType
import autocomplete_light
from plp.models import CourseSession
from plp_edmodule.models import EducationalModule
from .models import Upsale


class StaffAutocomplete(autocomplete_light.AutocompleteModelBase):
    def choices_for_request(self):
        if self.request.user.is_staff:
            return super(StaffAutocomplete, self).choices_for_request()
        return []


class UpsaleLinkMultisearch(autocomplete_light.AutocompleteGenericBase):
    """
    Подсказки по связанным объектам UpsaleLink.
    При расширении content_type обновлять choices и search_fields здесь
    """
    choices = [
        CourseSession.objects.all(),
        EducationalModule.objects.all()
    ]
    search_fields = [
        ('slug', 'course__title'),
        ('code', 'title'),
    ]

    def choices_for_request(self):
        if not self.request.user.is_staff:
            return []
        ctype_id = self.request.GET.get('ctype_id')
        if ctype_id and ctype_id.isdigit():
            ct = ContentType.objects.filter(id=ctype_id).first()
            if ct:
                model_class = ct.model_class()
                index = [ind for ind, queryset in enumerate(self.choices) if queryset.model == model_class]
                if index:
                    index = index[0]
                    self.choices = [self.choices[index]]
                    self.search_fields = [self.search_fields[index]]
        return super(UpsaleLinkMultisearch, self).choices_for_request()

autocomplete_light.register(Upsale, StaffAutocomplete, name='UpsaleAutocomplete',
                            search_fields=['slug', 'title'],
                            attrs={'data-autocomplete-minimum-characters': 1})
autocomplete_light.register(UpsaleLinkMultisearch, name='UpsaleLinkMulticomplete',
                            attrs={'data-autocomplete-minimum-characters': 1})
