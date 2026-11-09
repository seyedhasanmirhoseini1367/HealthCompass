from django import template

from apps.appointments.calendar_export import google_url

register = template.Library()


@register.filter
def google_calendar_url(appt):
    return google_url(appt)
