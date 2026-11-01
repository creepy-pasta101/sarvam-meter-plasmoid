# SPDX-License-Identifier: MIT
"""Rate card and cost arithmetic (SPEC section 5).

All money is INR.  Quantities use the units ``tokens``, ``chars``, ``seconds``,
``pages`` and ``requests``.  Costs are rounded to 6 decimal places.

``None`` in the rate card means *price not published*: :func:`cost_for` returns
``0.0`` and :func:`rate_known` returns ``False``.

Chat pricing note: output tokens cost four times input tokens on the Sarvam
models (Rs 60 vs Rs 15 per million), so the caller must pass the output count
separately via ``out_quantity``.  When ``out_quantity`` is ``None`` the legacy
behaviour applies -- every non-cached token is priced at ``in_per_million`` --
which understates a chat call by up to 4x; the recording proxy therefore always
passes ``out_quantity``.  ``quantity`` is the *non-cached input* token count when
``out_quantity`` is given, and ``cached_quantity`` is priced at
``cached_in_per_million``.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, Optional

#: Units accepted throughout the project.
UNITS = ("tokens", "chars", "seconds", "pages", "requests")

#: Pay-as-you-go list prices, INR.  The only numbers hardcoded anywhere.
DEFAULT_RATE_CARD = {
    "chat": {
        "default": {
            "in_per_million": 15.0,
            "cached_in_per_million": 5.0,
            "out_per_million": 60.0,
        },
        "sarvam-105b": {
            "in_per_million": 15.0,
            "cached_in_per_million": 5.0,
            "out_per_million": 60.0,
        },
        "sarvam-105b-conversations": {
            "in_per_million": 15.0,
            "cached_in_per_million": 5.0,
            "out_per_million": 60.0,
        },
        "glm5.3": {
            "in_per_million": 126.0,
            "cached_in_per_million": 20.0,
            "out_per_million": 390.0,
        },
        "glm5.3-flash": {
            "in_per_million": 13.5,
            "cached_in_per_million": 2.5,
            "out_per_million": 45.0,
        },
        "gemma4": {
            "in_per_million": 35.0,
            "cached_in_per_million": 10.0,
            "out_per_million": 90.0,
        },
        "deepseekv4.1-flash": {
            "in_per_million": 25.0,
            "cached_in_per_million": 0.5,
            "out_per_million": 105.0,
        },
    },
    "stt": {"default": {"per_minute": 0.49}},
    "tts": {"default": {"per_1k_chars": 3.00}},
    "translate": {"default": {"per_1k_chars": None}},
    "transliterate": {"default": {"per_1k_chars": None}},
    "lid": {"default": {"per_request": None}},
    "other": {"default": {}},
}

#: Price keys that mean "this rate is published".
_PRICE_KEYS = ("in_per_million", "per_minute", "per_1k_chars", "per_request")


def _merge_card(base: dict, overrides: dict) -> dict:
    for api, models in overrides.items():
        if not isinstance(models, dict):
            continue
        section = base.get(api)
        if not isinstance(section, dict):
            section = {}
            base[api] = section
        for model, prices in models.items():
            if not isinstance(prices, dict):
                continue
            entry = section.get(model)
            if not isinstance(entry, dict):
                entry = {}
            merged = dict(entry)
            merged.update(prices)
            section[model] = merged
    return base


def rate_card(overrides: Optional[dict] = None) -> dict:
    """Return a fresh rate card: :data:`DEFAULT_RATE_CARD` deep-merged with
    ``overrides`` (per api, per model, per price key)."""
    card = copy.deepcopy(DEFAULT_RATE_CARD)
    if not overrides:
        return card
    if not isinstance(overrides, dict):
        raise ValueError("rate overrides must be a dict")
    return _merge_card(card, overrides)


def _lookup(api: str, model: Optional[str], card: dict) -> Optional[dict]:
    if not isinstance(card, dict):
        return None
    section = card.get(api)
    if not isinstance(section, dict):
        return None
    if isinstance(model, str) and model in section:
        entry = section[model]
        if isinstance(entry, dict):
            return entry
    entry = section.get("default")
    if isinstance(entry, dict):
        return entry
    return None


def _check_quantity(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number, got %r" % (name, value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("%s must be finite, got %r" % (name, value))
    if number < 0:
        raise ValueError("%s must be >= 0, got %r" % (name, value))
    return number


def _finite(value: float) -> float:
    """Round to 6 dp, refusing to hand back an infinite or NaN price."""
    if not math.isfinite(value):
        raise ValueError("cost overflows for this quantity")
    return round(value, 6)


def cost_for(
    api: str,
    model: Optional[str],
    quantity: float,
    unit: str,
    card: Optional[dict] = None,
    cached_quantity: float = 0.0,
    out_quantity: Optional[float] = None,
) -> float:
    """Cost in INR for ``quantity`` of ``unit`` on ``api``/``model``.

    ``out_quantity`` applies to ``chat`` only: when given, ``quantity`` is the
    non-cached input token count, ``cached_quantity`` the cached input count and
    ``out_quantity`` the generated tokens, each priced at its own rate.  When it
    is ``None`` the legacy blended path is used (see the module docstring).

    Returns ``0.0`` when the api or the model price is unknown/unpublished.
    Raises :class:`ValueError` for a negative or non-finite quantity.
    """
    if not isinstance(api, str):
        raise ValueError("api must be a string, got %r" % (api,))
    amount = _check_quantity(quantity, "quantity")
    cached = _check_quantity(cached_quantity, "cached_quantity")
    out_tokens = None
    if out_quantity is not None:
        out_tokens = _check_quantity(out_quantity, "out_quantity")

    active_card = DEFAULT_RATE_CARD if card is None else card
    entry = _lookup(api, model, active_card)
    if not entry:
        return 0.0

    if api == "chat":
        in_price = entry.get("in_per_million")
        if in_price is None:
            return 0.0
        cached_price = entry.get("cached_in_per_million")
        if cached_price is None:
            cached_price = in_price
        if out_tokens is not None:
            out_price = entry.get("out_per_million")
            if out_price is None:
                out_price = in_price
            cost = (
                amount * float(in_price)
                + cached * float(cached_price)
                + out_tokens * float(out_price)
            ) / 1000000.0
            return _finite(cost)
        cached_used = min(cached, amount)
        cost = (
            (amount - cached_used) * float(in_price)
            + cached_used * float(cached_price)
        ) / 1000000.0
        return _finite(cost)

    per_minute = entry.get("per_minute")
    per_1k_chars = entry.get("per_1k_chars")
    per_request = entry.get("per_request")

    if per_minute is not None:
        return _finite(amount / 60.0 * float(per_minute))
    if per_1k_chars is not None:
        return _finite(amount / 1000.0 * float(per_1k_chars))
    if per_request is not None:
        return _finite(amount * float(per_request))
    return 0.0


def rate_known(api: str, model: Optional[str], card: Optional[dict] = None) -> bool:
    """True when a published price exists for ``api``/``model``."""
    active_card = DEFAULT_RATE_CARD if card is None else card
    entry = _lookup(api, model, active_card)
    if not entry:
        return False
    for key in _PRICE_KEYS:
        if entry.get(key) is not None:
            return True
    return False


__all__ = [
    "DEFAULT_RATE_CARD",
    "UNITS",
    "cost_for",
    "rate_card",
    "rate_known",
]
