"""
One error body for the API (audit ARCH-08): {"error": str, "code": str,
"reference": str?}.

DRF's own exceptions (validation, auth, throttling, 404) keep their status and
detail, re-shaped into `error` and `code`; field-level validation errors keep
their per-field dict so clients can show them next to inputs. Anything DRF does
not handle is logged with a reference id through healthcompass.errors and
answered with a generic 500, never a traceback or exception text.
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from healthcompass.errors import client_error


def _first_message(detail):
    if isinstance(detail, dict):
        for value in detail.values():
            return _first_message(value)
    if isinstance(detail, (list, tuple)) and detail:
        return _first_message(detail[0])
    return str(detail)


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        view = context.get('view')
        body = client_error(exc, context=getattr(view, '__name__', '') or type(view).__name__)
        body['code'] = 'server_error'
        return Response(body, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    detail = response.data
    code = getattr(exc, 'default_code', 'error')
    codes = exc.get_codes() if hasattr(exc, 'get_codes') else None
    if isinstance(codes, str):
        code = codes
    if isinstance(detail, dict) and set(detail) - {'detail'}:
        # Field errors: keep them, add the standard keys alongside.
        body = dict(detail)
        body.setdefault('error', _first_message(detail))
        body.setdefault('code', code)
    else:
        body = {'error': _first_message(detail.get('detail', detail) if isinstance(detail, dict)
                                        else detail),
                'code': code}
    response.data = body
    return response
