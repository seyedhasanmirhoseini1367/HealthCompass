# ai_insights/inference/registry.py
"""
Handler registry — maps handler slugs to InferenceHandler subclasses.

Usage
-----
    from ai_insights.inference.registry import register, get_handler

    @register("my_model")
    class MyModelHandler(InferenceHandler):
        ...

    # In views.py:
    handler = get_handler(ai_model)
    result  = handler.run(uploaded_file=file)
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_insights.models import AIModel

_REGISTRY: dict = {}


def register(slug: str):
    """Class decorator — registers an InferenceHandler subclass under *slug*."""
    def decorator(cls):
        _REGISTRY[slug] = cls
        return cls
    return decorator


def get_handler(ai_model: "AIModel"):
    """
    Return an instantiated handler for *ai_model*.

    Slug resolution order:
      1. ai_model.handler_slug  (the CharField on the model)
      2. ai_model.handler_config['handler']  (legacy / override key in JSON)

    Raises ValueError (caught by the view and shown as an error) when no slug
    is configured or the slug is not registered.
    """
    slug = (ai_model.handler_slug or '').strip()
    if not slug:
        cfg  = ai_model.handler_config or {}
        slug = cfg.get('handler', '').strip()

    if not slug:
        raise ValueError(
            f'No handler configured for model "{ai_model.name}". '
            'Set handler_slug in the admin (e.g. "seizure_eeg", "image_classifier").'
        )

    if slug not in _REGISTRY:
        available = ', '.join(sorted(_REGISTRY.keys())) or '(none registered)'
        raise ValueError(
            f'Unknown handler slug "{slug}" for model "{ai_model.name}". '
            f'Available: {available}. '
            'Check ai_insights/inference/ for registered handlers.'
        )

    return _REGISTRY[slug](ai_model)


def list_handlers() -> list[str]:
    """Return sorted list of registered handler slugs (useful in admin/debug)."""
    return sorted(_REGISTRY.keys())
