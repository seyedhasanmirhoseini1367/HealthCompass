"""
Error tracking (audit ARCH-09). Enabled only when SENTRY_DSN is set.

Health data must never reach Sentry: no default PII, no request bodies, query
strings dropped, and any key that looks like a credential or a question/answer
is removed from every event before it is sent.
"""
import re

_SENSITIVE = re.compile(r'password|token|authorization|cookie|query|response|secret|session',
                        re.IGNORECASE)


def _scrub(value):
    if isinstance(value, dict):
        return {k: ('[removed]' if _SENSITIVE.search(str(k)) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def before_send(event, hint):
    request = event.get('request')
    if isinstance(request, dict):
        request.pop('query_string', None)
        request.pop('data', None)
        request.pop('cookies', None)
        if isinstance(request.get('url'), str):
            request['url'] = request['url'].split('?', 1)[0]
        if isinstance(request.get('headers'), dict):
            request['headers'] = _scrub(request['headers'])
    for key in ('extra', 'contexts', 'tags'):
        if key in event:
            event[key] = _scrub(event[key])
    return event


def init(dsn: str, *, environment: str, traces_sample_rate: float) -> None:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.rq import RqIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        max_request_body_size='never',
        traces_sample_rate=traces_sample_rate,
        before_send=before_send,
        integrations=[
            DjangoIntegration(),
            RqIntegration(),
            # healthcompass.ops events at ERROR become Sentry events; the
            # structured fields are already free of health data.
            LoggingIntegration(level=None, event_level=None),
        ],
    )


import logging  # noqa: E402


class OpsToSentryHandler(logging.Handler):
    """healthcompass.ops events at ERROR become Sentry messages. The event
    fields are already free of health data (observability._safe_scalar)."""

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
        try:
            import sentry_sdk
            payload = getattr(record, 'ops_event', {}) or {}
            sentry_sdk.capture_message(str(payload.get('event', 'ops_event')), level='error',
                                       extras=_scrub(payload))
        except Exception:  # never let reporting break the caller
            self.handleError(record)
