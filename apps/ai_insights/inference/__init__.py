# ai_insights/inference/__init__.py
"""
Auto-import all handler modules so their @register() decorators run at startup.
Add a new handler by creating a .py file here and importing it below.
"""

from .base import InferenceHandler, InferenceError  # noqa: F401
from .registry import register, get_handler, list_handlers  # noqa: F401

# ── Built-in handlers ──────────────────────────────────────────────────────────
from . import tabular_passthrough  # noqa: F401
from . import image_classifier     # noqa: F401
from . import eeg_csv              # noqa: F401
from . import seizure_eeg          # noqa: F401


def run_model(ai_model, input_data: dict, input_file=None) -> dict:
    """
    The single inference entry point for both the web view and the mobile API.

    Resolves a handler (falling back to the extension/input-type map when
    handler_slug is unset), runs it, and returns a StandardPrediction result
    dict. Models with no uploaded file return the rule-based demonstration
    result, flagged `demo: True`.

    This exists because the mobile API imported `run_model` from
    apps.ai_insights.runner — a module that no longer exists after the split
    into inference/ and interpretation.py. Every call raised
    ModuleNotFoundError, so POST /api/v1/ai-models/<slug>/run/ returned 500 for
    the whole life of the endpoint, undetected because apps/api had no tests.

    Rather than restore a second copy of the dispatch logic, both callers now
    share this one: two divergent definitions of "how a model runs" is how the
    web and mobile paths drift apart.
    """
    from .interpretation import _rule_based_demo_result

    handler_slug = ai_model.handler_slug or ''

    if not handler_slug and ai_model.model_file:
        ext = ai_model.model_file.name.rsplit('.', 1)[-1].lower()
        ext_map = {
            'onnx': {'tabular': 'tabular_passthrough',
                     'image':   'image_classifier',
                     'eeg_csv': 'eeg_csv',
                     'parquet': 'tabular_passthrough',
                     'file':    'tabular_passthrough'},
            'pt':  {'eeg_csv': 'eeg_csv', 'parquet': 'eeg_csv'},
            'pth': {'eeg_csv': 'eeg_csv', 'parquet': 'eeg_csv'},
        }
        handler_slug = ext_map.get(ext, {}).get(ai_model.input_type, '')

    if handler_slug:
        ai_model.handler_slug = handler_slug
        handler = get_handler(ai_model)
        return handler.run(
            uploaded_file=input_file,
            input_data=input_data if not input_file else None,
        )

    if not ai_model.model_file:
        return _rule_based_demo_result(ai_model, input_data, input_file)

    ext = ai_model.model_file.name.rsplit('.', 1)[-1].upper()
    raise ValueError(
        f'No handler configured for model "{ai_model.name}" ({ext} file). '
        'Set handler_slug in Django Admin.'
    )
