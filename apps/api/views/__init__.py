from .auth import (
    login, register, forgot_password, me, register_fcm_token,
    profile_update, profile_picture_upload, change_password, emergency_card,
)
from .records import (
    records_list, record_detail, record_delete, record_upload,
    upload_pdf_api, upload_text_api, upload_kanta_api,
    upload_wearable_api, scan_ocr_api,
)
from .analytics import dashboard_summary, analytics, population_insights
from .alerts import (
    alerts_list, alert_mark_read,
    notifications_list, notification_mark_read,
)
from .predictions import (
    my_predictions, prediction_detail_api,
    ai_models_list, ai_model_detail, run_model_prediction,
    seizure_analysis,
)
from .appointments import appointments_list_create, appointment_detail
from .assistant import assistant_ask, assistant_stream, assistant_sessions, assistant_session_detail
from .icu import icu_dashboard_api, seizure_realtime_analyze
from .consent import (consent_list, consent_grant, consent_revoke,
                      consent_history_view)
from .export import export_status, export_download
from .sharing import (sharing_companions, create_share, revoke_share,
                      shared_patient_detail)
from .care import (care_tasks_list_create, care_task_stop,
                   care_occurrences_list, occurrence_respond,
                   patient_reports_list_create)

__all__ = [
    'login', 'register', 'forgot_password', 'me', 'register_fcm_token',
    'profile_update', 'profile_picture_upload', 'change_password', 'emergency_card',
    'records_list', 'record_detail', 'record_delete', 'record_upload',
    'upload_pdf_api', 'upload_text_api', 'upload_kanta_api',
    'upload_wearable_api', 'scan_ocr_api',
    'dashboard_summary', 'analytics', 'population_insights',
    'alerts_list', 'alert_mark_read', 'notifications_list', 'notification_mark_read',
    'my_predictions', 'prediction_detail_api',
    'ai_models_list', 'ai_model_detail', 'run_model_prediction', 'seizure_analysis',
    'appointments_list_create', 'appointment_detail',
    'assistant_ask', 'assistant_sessions', 'assistant_session_detail',
    'icu_dashboard_api', 'seizure_realtime_analyze',
    'consent_list', 'consent_grant', 'consent_revoke', 'consent_history_view',
    'export_status', 'export_download',
    'sharing_companions', 'create_share', 'revoke_share', 'shared_patient_detail',
    'care_tasks_list_create', 'care_task_stop', 'care_occurrences_list',
    'occurrence_respond', 'patient_reports_list_create',
]
