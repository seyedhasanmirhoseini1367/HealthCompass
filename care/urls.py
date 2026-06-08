from django.urls import path
from . import views

app_name = 'care'

urlpatterns = [
    # Main hub
    path('',                              views.care_home,            name='home'),

    # Check-in
    path('checkin/',                      views.do_checkin,           name='checkin'),

    # Medications
    path('medication/add/',               views.add_medication,       name='add_medication'),
    path('medication/<int:pk>/delete/',   views.delete_medication,    name='delete_medication'),
    path('medication/<int:pk>/log/',      views.log_medication,       name='log_medication'),

    # Caregivers
    path('caregiver/add/',                views.add_caregiver,        name='add_caregiver'),
    path('caregiver/<int:pk>/remove/',    views.remove_caregiver,     name='remove_caregiver'),

    # Settings
    path('settings/',                     views.care_settings,        name='settings'),

    # Public caregiver dashboard
    path('view/<uuid:token>/',            views.caregiver_view,       name='caregiver_view'),

    # Voice check-in
    path('voice/',                        views.voice_checkin,        name='voice_checkin'),
    path('voice/save/',                   views.voice_save,           name='voice_save'),

    # Escalation acknowledge (no login — one-click from email)
    path('ack/<uuid:token>/',             views.acknowledge_by_token, name='acknowledge_by_token'),

    # Escalation acknowledge (AJAX, login required)
    path('escalation/<int:pk>/ack/',      views.acknowledge_escalation, name='acknowledge_escalation'),

    # Quick "I'm fine" (no login)
    path('quick/<uuid:token>/',           views.quick_checkin,        name='quick_checkin'),

    # Pain Body Map
    path('pain/add/',                     views.add_pain_log,         name='add_pain_log'),
    path('pain/<int:pk>/delete/',         views.delete_pain_log,      name='delete_pain_log'),

    # Medication Interaction Checker (AJAX)
    path('interactions/',                 views.check_interactions,   name='check_interactions'),
]
