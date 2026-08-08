"""Model pricing + model list for the web UI.

Rates are approximate USD per 1M tokens (Google AI pricing tiers). The active
model's rate can be overridden in .env with GEMINI_MODEL_PRICE_IN /
GEMINI_MODEL_PRICE_OUT. Everything else is editable here.
"""

from __future__ import annotations

import os

# USD per 1M tokens: {"input_usd_m": X, "output_usd_m": Y}
PRICING: dict[str, dict[str, float]] = {
    "gemini-3.1-flash-lite": {"input_usd_m": 0.45, "output_usd_m": 2.70},
}

DEFAULT_MODEL = "gemini-3.1-flash-lite"


def model_options() -> list[str]:
    return list(PRICING.keys())


def get_pricing(model: str | None) -> dict[str, float]:
    model = model or DEFAULT_MODEL
    rate = PRICING.get(model, PRICING[DEFAULT_MODEL])
    in_rate = float(os.environ.get("GEMINI_MODEL_PRICE_IN", "") or rate["input_usd_m"])
    out_rate = float(os.environ.get("GEMINI_MODEL_PRICE_OUT", "") or rate["output_usd_m"])
    return {"input_usd_m": in_rate, "output_usd_m": out_rate}


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    rate = get_pricing(model)
    return (input_tokens / 1_000_000) * rate["input_usd_m"] + (output_tokens / 1_000_000) * rate["output_usd_m"]
