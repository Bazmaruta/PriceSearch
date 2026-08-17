"""Canonical — grocery receipt line canonicalisation engine.

Turns a raw supermarket receipt line into a structured, canonical product
representation using the system prompt in ``Canonical.md``.
"""

from .engine import (
    DEFAULT_MODEL,
    canonicalize,
    canonicalize_many,
    canonicalize_mock,
    load_prompt,
)

__all__ = [
    "DEFAULT_MODEL",
    "load_prompt",
    "canonicalize",
    "canonicalize_many",
    "canonicalize_mock",
]
