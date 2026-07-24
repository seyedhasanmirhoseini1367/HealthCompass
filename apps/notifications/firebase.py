import json
import logging

logger = logging.getLogger(__name__)
_app = None


def _get_app():
    global _app
    if _app is not None:
        return _app
    try:
        import firebase_admin
        from firebase_admin import credentials
        from django.conf import settings
        cred_json = getattr(settings, 'FIREBASE_CREDENTIALS_JSON', None)
        if not cred_json:
            logger.warning('FIREBASE_CREDENTIALS_JSON is not set — push notifications are disabled.')
            return None
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
        _app = firebase_admin.initialize_app(cred)
    except Exception as e:
        logger.warning('Firebase Admin init failed: %s', e)
    return _app


def send_push(user, title: str, body: str, data: dict | None = None):
    app = _get_app()
    if app is None:
        return
    try:
        from firebase_admin import messaging
        from .models import FCMDevice
        tokens = list(FCMDevice.objects.filter(user=user).values_list('token', flat=True))
        if not tokens:
            return
        for token in tokens:
            try:
                messaging.send(messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={k: str(v) for k, v in (data or {}).items()},
                    android=messaging.AndroidConfig(priority='high'),
                    token=token,
                ), app=app)
            except Exception as e:
                logger.debug('FCM send failed for token %s: %s', token[:20], e)
                if 'registration-token-not-registered' in str(e):
                    FCMDevice.objects.filter(token=token).delete()
    except Exception as e:
        logger.warning('send_push error: %s', e)
