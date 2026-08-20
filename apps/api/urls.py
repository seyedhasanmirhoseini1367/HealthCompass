from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

app_name = 'api'

urlpatterns = [
    # Auth
    path('auth/register/',         views.register,         name='register'),
    path('auth/login/',            views.login,            name='login'),
    path('auth/refresh/',          TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/me/',               views.me,               name='me'),
    path('auth/profile/',          views.profile_update,   name='profile_update'),
    path('auth/change-password/',  views.change_password,  name='change_password'),
    path('auth/forgot-password/',  views.forgot_password,  name='forgot_password'),
    path('auth/emergency-card/',   views.emergency_card,   name='emergency_card'),
    path('auth/fcm-token/',        views.register_fcm_token, name='fcm_token'),

    # Medical records
    path('records/upload/',           views.record_upload,       name='record_upload'),
    path('records/upload/pdf/',       views.upload_pdf_api,      name='upload_pdf'),
    path('records/upload/text/',      views.upload_text_api,     name='upload_text'),
    path('records/upload/kanta/',     views.upload_kanta_api,    name='upload_kanta'),
    path('records/upload/wearable/',  views.upload_wearable_api, name='upload_wearable'),
    path('records/upload/scan/',      views.scan_ocr_api,        name='scan_ocr'),
    path('records/',                 views.records_list,    name='records_list'),
    # Before the <str:pk> route, which would otherwise swallow the literal.
    path('records/<str:pk>/',        views.record_detail,   name='record_detail'),
    path('records/<str:pk>/delete/', views.record_delete,   name='record_delete'),

    # Dashboard
    path('dashboard/',             views.dashboard_summary,  name='dashboard'),

    # Analytics & Insights
    path('analytics/',             views.analytics,          name='analytics'),
    path('alerts/',                views.alerts_list,        name='alerts_list'),
    path('alerts/<str:pk>/read/',  views.alert_mark_read,    name='alert_read'),

    # My predictions
    path('predictions/',              views.my_predictions,       name='my_predictions'),
    path('predictions/<str:pk>/',     views.prediction_detail_api, name='prediction_detail'),

    # Notifications
    path('notifications/',                 views.notifications_list,     name='notifications'),
    path('notifications/<str:pk>/read/',   views.notification_mark_read, name='notification_read'),

    # AI Models
    path('ai-models/',                    views.ai_models_list,      name='ai_models'),
    path('ai-models/<str:slug>/',         views.ai_model_detail,     name='ai_model_detail'),
    path('ai-models/<str:slug>/run/',     views.run_model_prediction, name='run_model'),

    # Appointments
    path('appointments/',             views.appointments_list_create, name='appointments'),
    path('appointments/<uuid:pk>/',   views.appointment_detail,       name='appointment_detail'),

    # AI Assistant
    path('assistant/ask/',                        views.assistant_ask,            name='assistant_ask'),
    path('assistant/stream/',                     views.assistant_stream,         name='assistant_stream'),
    path('assistant/sessions/',                   views.assistant_sessions,       name='assistant_sessions'),
    path('assistant/sessions/<str:session_id>/',  views.assistant_session_detail, name='assistant_session_detail'),

    # Consent (privacy)
    path('consent/',          views.consent_list,         name='consent_list'),
    path('consent/grant/',    views.consent_grant,        name='consent_grant'),
    path('consent/revoke/',   views.consent_revoke,       name='consent_revoke'),
    path('consent/history/',  views.consent_history_view, name='consent_history'),

    # Data export (GDPR Art. 15 / 20)
    path('export/',          views.export_status,   name='export_status'),
    path('export/download/', views.export_download, name='export_download'),

    # Seizure Analysis
    path('seizure-analysis/',      views.seizure_analysis,      name='seizure_analysis'),

    # Profile picture
    path('auth/profile/picture/',  views.profile_picture_upload, name='profile_picture'),

    # Population insights
    path('population/',            views.population_insights,    name='population_insights'),

    # ICU Demo
    path('icu/',                          views.icu_dashboard_api,        name='icu_dashboard_api'),

    # Seizure Realtime
    path('seizure-realtime/analyze/',     views.seizure_realtime_analyze, name='seizure_realtime_analyze'),

    # Sharing / companions
    path('sharing/companions/',            views.sharing_companions,     name='sharing_companions'),
    path('sharing/companions/create/',     views.create_share,           name='create_share'),
    path('sharing/companions/<int:pk>/revoke/', views.revoke_share,      name='revoke_share'),
    path('sharing/companions/<int:pk>/',   views.shared_patient_detail,  name='shared_patient_detail'),

    # Care monitoring
    path('care/tasks/',                  views.care_tasks_list_create, name='care_tasks'),
    path('care/tasks/<uuid:pk>/stop/',   views.care_task_stop,         name='care_task_stop'),
    path('care/occurrences/',            views.care_occurrences_list,  name='care_occurrences'),
    path('care/occurrences/<uuid:pk>/respond/', views.occurrence_respond, name='occurrence_respond'),
    path('care/reports/',                views.patient_reports_list_create, name='care_reports'),
]
