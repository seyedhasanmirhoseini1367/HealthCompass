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
