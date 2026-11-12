"""
OpenAPI annotations for the function-based API views (audit ARCH-08).

drf-spectacular cannot infer request or response shapes from a function view,
and a decorator on each of ~60 functions would be noise next to the code. The
shapes are declared here instead and applied to each route's generated APIView
class when the URLconf loads: every operation gets a unique operationId (the
route name) and, where one exists, the serializer it really reads or returns.
Routes not listed are documented as a free-form JSON object.
"""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as s

from . import serializers as api

_PAGE = [OpenApiParameter('page', int, description='Page number (20 per page).'),
         OpenApiParameter('page_size', int, description='Items per page, at most 100.')]

_TOKENS = inline_serializer('TokenPair', {
    'access': s.CharField(), 'refresh': s.CharField(), 'user': api.UserSerializer()})
_LOGIN = inline_serializer('LoginRequest', {'email': s.CharField(), 'password': s.CharField()})
_ERROR = inline_serializer('Error', {'error': s.CharField(), 'code': s.CharField(required=False),
                                     'reference': s.CharField(required=False)})
_DETAIL = inline_serializer('Detail', {'detail': s.CharField()})
_SHARE = inline_serializer('ShareOffer', {'identifier': s.EmailField(),
                                          'scopes': s.ListField(child=s.CharField())})
_ASK = inline_serializer('AssistantQuestion', {
    'query': s.CharField(), 'session_id': s.UUIDField(required=False),
    'subject': s.CharField(required=False)})
_ANSWER = inline_serializer('AssistantAnswer', {
    'answer': s.CharField(), 'sources': s.ListField(child=s.DictField()),
    'session_id': s.UUIDField(), 'message_id': s.UUIDField(),
    'consent_required': s.CharField(required=False)})
_UPLOAD = inline_serializer('FileUpload', {'file': s.FileField(), 'notes': s.CharField(required=False)})

SCHEMAS = {
    'login':            dict(request=_LOGIN, responses={200: _TOKENS, 401: _ERROR, 403: _ERROR}),
    'register':         dict(request=api.RegisterSerializer, responses={201: _TOKENS, 400: _ERROR}),
    'logout':           dict(request=inline_serializer('LogoutRequest', {
                                 'refresh': s.CharField(), 'fcm_token': s.CharField(required=False)}),
                             responses={205: None}),
    'logout_all':       dict(request=None, responses={200: inline_serializer(
                                 'LogoutAll', {'revoked': s.IntegerField()})}),
    'account_delete':   dict(request=inline_serializer('AccountDelete', {
                                 'password': s.CharField(required=False),
                                 'token': s.CharField(required=False)}),
                             responses={204: None, 403: _ERROR}),
    'account_delete_request': dict(request=None, responses={200: _DETAIL}),
    'me':               dict(responses=api.UserSerializer),
    'profile_update':   dict(request=api.ProfileUpdateSerializer, responses={200: api.UserSerializer}),
    'change_password':  dict(request=inline_serializer('ChangePassword', {
                                 'old_password': s.CharField(), 'new_password': s.CharField()}),
                             responses={200: _TOKENS, 400: _ERROR}),
    'forgot_password':  dict(request=inline_serializer('ForgotPassword', {'email': s.EmailField()}),
                             responses={200: _DETAIL}),
    'records_list':     dict(parameters=_PAGE, responses=api.MedicalRecordListSerializer(many=True)),
    'record_detail':    dict(responses=api.MedicalRecordDetailSerializer),
    'record_upload':    dict(request={'multipart/form-data': _UPLOAD},
                             responses={201: api.MedicalRecordSerializer, 400: _ERROR}),
    'upload_pdf':       dict(request={'multipart/form-data': _UPLOAD},
                             responses={201: api.MedicalRecordSerializer, 400: _ERROR}),
    'upload_wearable':  dict(request={'multipart/form-data': _UPLOAD},
                             responses={201: api.MedicalRecordSerializer, 400: _ERROR}),
    'alerts_list':      dict(parameters=_PAGE, responses=api.HealthAlertSerializer(many=True)),
    'notifications':    dict(parameters=_PAGE, responses=api.NotificationSerializer(many=True)),
    'my_predictions':   dict(parameters=_PAGE, responses=api.ModelPredictionSerializer(many=True)),
    'ai_models':        dict(parameters=_PAGE, responses=api.AIModelListSerializer(many=True)),
    'appointments':     dict(parameters=_PAGE, request=api.AppointmentSerializer,
                             responses=api.AppointmentSerializer(many=True)),
    'assistant_ask':    dict(request=_ASK, responses={200: _ANSWER, 400: _ERROR, 404: _ERROR}),
    'assistant_stream': dict(request=_ASK, responses={(200, 'text/event-stream'): OpenApiTypes.STR}),
    'create_share':     dict(request=_SHARE, responses={202: _DETAIL, 400: _ERROR}),
    'accept_share':     dict(request=None, responses={200: api.SharingGrantSerializer, 404: _ERROR}),
    'decline_share':    dict(request=None, responses={204: None, 404: _ERROR}),
    'revoke_share':     dict(request=None, responses={204: None, 403: _ERROR}),
    'care_tasks':       dict(parameters=_PAGE, responses=api.CareTaskSerializer(many=True)),
    'care_occurrences': dict(parameters=_PAGE, responses=api.TaskOccurrenceSerializer(many=True)),
    'care_reports':     dict(parameters=_PAGE, responses=api.PatientReportSerializer(many=True)),
}

_GENERIC = dict(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)


def _own_handler(handler):
    """A distinct function per HTTP method. @api_view binds one handler object
    to every method, so annotating it in place would let the last method's
    schema overwrite the others."""
    def method_handler(self, *args, **kwargs):
        return handler(self, *args, **kwargs)
    method_handler.__name__ = getattr(handler, '__name__', 'handler')
    return method_handler


def annotate(urlpatterns):
    """Attach schema information to every named function-view route."""
    for pattern in urlpatterns:
        view_cls = getattr(pattern.callback, 'cls', None)
        name = getattr(pattern, 'name', None)
        if view_cls is None or not name or name in ('schema', 'docs', 'token_refresh'):
            continue
        methods = [m for m in ('get', 'post', 'put', 'patch', 'delete')
                   if m in view_cls.http_method_names and hasattr(view_cls, m)]
        for method in methods:
            spec = dict(SCHEMAS.get(name, _GENERIC))
            if method in ('get', 'delete'):
                spec.pop('request', None)
            if method != 'get':
                spec.pop('parameters', None)
                if 'parameters' in SCHEMAS.get(name, {}) and 'responses' in spec:
                    # A list GET's response is not what a POST returns.
                    spec['responses'] = OpenApiTypes.OBJECT
            if method in ('post', 'put', 'patch') and 'request' not in spec:
                spec['request'] = OpenApiTypes.OBJECT
            spec['operation_id'] = name if len(methods) == 1 else f'{name}_{method}'
            setattr(view_cls, method, extend_schema(**spec)(_own_handler(getattr(view_cls, method))))
