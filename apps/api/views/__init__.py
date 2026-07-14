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
from .assistant import assistant_ask, assistant_sessions, assistant_session_detail
from .icu import icu_dashboard_api, seizure_realtime_analyze

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
]
