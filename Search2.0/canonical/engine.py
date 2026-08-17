"""Core engine for the receipt-line canonicalisation prompt (``Canonical.md``).

Flow:
  1. Load the system prompt from ``Canonical.md``.
   2. For each raw receipt line, call Gemini (hard-coded model
      ``gemini-3.1-flash-lite``, key from ``GOOGLE_API_KEY`` in the repo-root
      ``.env``) with strict JSON output. No grounding tools are used — the
      prompt explicitly must not search the internet. The model is never
      overridden by arguments or environment variables.
  3. Parse + validate the returned JSON against the schema in the prompt.
  4. Cache results per line for 24h so repeat runs are instant.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_PROMPT_FILE = ROOT / "Canonical.md"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
CACHE_DIR = ROOT / "data" / "cache"
CACHE_TTL_SECONDS = 24 * 60 * 60

_lock = threading.Lock()

_ALLOWED_SIZE_UNITS = {"g", "kg", "mL", "L", "each"}
_UNSUPPORTED_UNITS = {
    "m", "cm", "mm", "m2", "cm2",
    "roll", "rolls", "sheet", "sheets",
    "tablet", "tablets", "capsule", "capsules", "sachet", "sachets",
    "wipe", "wipes",
}
_KNOWN_NUMERIC_BRANDS = ("3m", "7-eleven", "5 seeds", "4 pines")
_STORE_NAMES = {
    "woolworths", "ww", "w/w",
    "coles", "col",
    "aldi",
    "iga",
    "costco",
    "foodland",
    "harris farm", "harris farm markets",
}
_SIZE_RE = re.compile(
    r"(?i)(?:\d+(?:\.\d+)?\s*(?:x|pk)\s*)?\d+(?:\.\d+)?\s*(?:g|kg|ml|l|m|cm|mm)\b"
)
_RAW_SIZE_UNIT_RE = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(m2|cm2|mm|cm|g|kg|ml|l|m)\b"
)
_EXPLICIT_PER_UNIT_RE = re.compile(
    r"(?i)(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(g|kg|ml|l)\b"
)
_UNIT_ALIASES = {
    "g": "g", "gm": "g", "gr": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilo": "kg", "kilos": "kg", "kilograms": "kg",
    "ml": "mL", "mls": "mL", "millilitre": "mL", "millilitres": "mL",
    "milliliter": "mL", "milliliters": "mL",
    "l": "L", "lt": "L", "ltr": "L", "litre": "L", "litres": "L",
    "liter": "L", "liters": "L",
    "each": "each", "ea": "each", "unit": "each", "units": "each",
}
_ATTR_FIELDS = ("brand", "product_name", "variant", "size", "pack_count", "category")


def load_prompt(path: str | Path | None = None) -> str:
    """Read the canonicalisation system prompt (defaults to ``Canonical.md``)."""
    p = Path(path) if path else DEFAULT_PROMPT_FILE
    if not p.exists():
        raise FileNotFoundError(f"Prompt file not found: {p}")
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Make the repo-root ``.env`` authoritative.

    Overrides any pre-existing machine/user env values (e.g. a stale
    GEMINI_MODEL or GEMINI_API_KEY on the host) so the project's keys win.
    GEMINI_API_KEY is removed when the .env does not define it — the engine
    intentionally uses GOOGLE_API_KEY so the SDK never falls back elsewhere.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path = HERE.parent.parent / ".env"  # repo-root .env
    values: dict[str, str] = {}
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
    os.environ.update(values)
    if "GEMINI_API_KEY" not in values:
        os.environ.pop("GEMINI_API_KEY", None)


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


# ---------------------------------------------------------------------------
# JSON parsing / validation
# ---------------------------------------------------------------------------

def _clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _as_number(value: Any, lo: float = 0.0, hi: float | None = None) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if num <= lo or (hi is not None and num > hi):
        return None
    return int(num) if float(num).is_integer() else num


def _as_unit(value: Any) -> str | None:
    raw = _clean_str(value)
    if not raw:
        return None
    return _UNIT_ALIASES.get(raw.casefold())


def _extract_raw_size(text: str | None) -> str | None:
    """Best-effort recovery of the original size expression from the raw line."""
    text = _scan_text(text)
    if not text:
        return None
    matches = [m.group(0).strip() for m in _SIZE_RE.finditer(text)]
    return " ".join(matches) if matches else None


def _scan_text(text: str | None) -> str | None:
    """Drop a leading known numeric brand (e.g. '3M', '7-Eleven') so it is
    never read as a size measurement (rule: brands are not measurements)."""
    if not text:
        return None
    low = text.casefold()
    for phrase in _KNOWN_NUMERIC_BRANDS:
        if low.startswith(phrase):
            rest = text[len(phrase):].strip(" ,;:")
            return rest or None
    return text


def _starts_with_numeric_brand(text: str | None) -> bool:
    low = (text or "").casefold()
    return any(low.startswith(p) for p in _KNOWN_NUMERIC_BRANDS)


def _has_real_size_expr(text: str | None) -> bool:
    """True when the raw line contains a genuine size measurement token."""
    text = _scan_text(text)
    return bool(text and _SIZE_RE.search(text))


def _last_raw_unit(text: str | None) -> str | None:
    """Unit of the final numeric+unit token in the raw line (supported or not).

    Used to enforce rule 4: when the receipt itself states an unsupported
    measurement (e.g. "10m", "2 roll"), no normalized size may be produced —
    regardless of what the model inferred.
    """
    text = _scan_text(text)
    if not text:
        return None
    ms = list(_RAW_SIZE_UNIT_RE.finditer(text))
    return ms[-1].group(1).casefold() if ms else None


def _explicit_per_unit(raw_name: str) -> dict[str, Any] | None:
    """"6 x 60mL"-style receipt text explicitly establishes per-item size.

    Returns {pack_count, size_value, size_unit}. The receipt's second operand
    is authoritative — a model value derived by dividing (360/6=60) is a
    forbidden transformation and must be overridden back to the explicit size.
    """
    m = _EXPLICIT_PER_UNIT_RE.search(_scan_text(raw_name) or "")
    if not m:
        return None
    unit = _UNIT_ALIASES.get(m.group(3).casefold())
    if unit is None:
        return None
    value = float(m.group(2))
    return {
        "pack_count": int(m.group(1)),
        "size_value": int(value) if value.is_integer() else value,
        "size_unit": unit,
    }


def _guard_brand(raw_name: str, brand: str | None) -> str | None:
    """Apply documented compressed-brand interpretations (rule 3).

    "McrOrg..." must become "Macro Organic", never a character-similar guess
    like "McCormick". Only high-confidence abbreviations are mapped here.
    """
    low = raw_name.casefold()
    if "mcrorg" in low.replace(" ", "") or "mcr organic" in low or "mcr-org" in low:
        return "Macro Organic"
    return brand


def _as_confidence(value: Any) -> float:
    num = _as_number(value, lo=-0.001, hi=1.0)
    if num is None:
        return 0.0
    return max(0.0, min(1.0, float(num)))


def _phrase_contains(container: str, phrase: str) -> bool:
    """True when ``phrase`` is semantically contained within ``container``."""
    c = container.casefold().strip()
    p = phrase.casefold().strip()
    if not c or not p:
        return False
    return p in c


_POSTPOSITIVE_PREFIXES = ("in ", "with ", "on ", "& ")


def _is_postpositive_variant(variant: str | None) -> bool:
    """Postpositive descriptors ("In Oil", "With Pulp") read better AFTER the
    product, e.g. "John West Tuna In Oil" not "John West In Oil Tuna"."""
    v = (variant or "").casefold().strip()
    return any(v.startswith(p) for p in _POSTPOSITIVE_PREFIXES)


def _strip_brand_prefix(product_name: str, brand: str) -> str | None:
    """Remove a brand repeated at the start of product_name (word-boundary safe).

    "Coca-Cola Zero Sugar" + brand "Coca-Cola" → "Zero Sugar".
    "Coleslaw" + brand "Coles" is left untouched (no word boundary).
    """
    pn = product_name.strip()
    br = brand.strip()
    if not pn:
        return None
    if not br:
        return pn
    if pn.casefold() == br.casefold():
        return None
    if pn.casefold().startswith(br.casefold() + " "):
        rest = pn[len(br):].strip()
        return rest or None
    return pn


def _strip_leading_store(text: str | None) -> str | None:
    """Remove a leading store name / abbreviation from product_name.

    A store token ("WW", "Woolworths", "COL", "Coles", "ALDI", ...) at the
    start of product_name identifies the retailer, never part of the product
    identity. Runs regardless of what brand the model reported.
    """
    if not text:
        return None
    low = text.casefold()
    for name in sorted(_STORE_NAMES, key=len, reverse=True):
        if low == name:
            return None
        if low.startswith(name + " "):
            rest = text[len(name):].strip()
            return rest or None
    return text


def _build_canonical_name(d: dict[str, Any]) -> str | None:
    """Fallback canonical name from the extracted attributes.

    Preferred order: Brand + Variant/Descriptor + Product + Size/Pack.
    Semantic deduplication: the variant is dropped when product_name OR brand
    already contains it, and a brand repeated at the start of product_name is
    stripped. Pack format respects size_basis: "N x size" only when per_unit,
    otherwise "N pack size".
    """
    brand = d.get("brand")
    product_name = d.get("product_name")
    variant = d.get("variant")

    if not product_name:
        return None  # no product identity, no canonical name

    if brand and product_name:
        product_name = _strip_brand_prefix(product_name, brand)

    variant_tokens: list[str] = []
    if variant and not (
        (product_name and _phrase_contains(product_name, variant))
        or (brand and _phrase_contains(brand, variant))
    ):
        variant_tokens = [variant]
    pre = [v for v in variant_tokens if not _is_postpositive_variant(v)]
    post = [v for v in variant_tokens if _is_postpositive_variant(v)]

    parts: list[str] = []
    if brand:
        parts.append(brand)
    parts.extend(pre)
    if product_name:
        parts.append(product_name)
    parts.extend(post)

    size = ""
    if d.get("size_value") is not None and d.get("size_unit"):
        if d["size_unit"] == "each":
            if float(d["size_value"]) == 1:
                size = ""  # "1 each" adds no search value (Sweet Corn, not Sweet Corn 1each)
            else:
                size = f"{d['size_value']:g} each"
        else:
            size = f"{d['size_value']:g}{d['size_unit']}"
    if d.get("pack_count"):
        if size and d.get("size_basis") == "per_unit":
            size = f"{d['pack_count']:g} x {size}"
        elif size:
            size = f"{d['pack_count']:g} pack {size}"
        else:
            size = f"{d['pack_count']:g} pack"
    if size:
        parts.append(size)

    return " ".join(parts) if parts else None


def normalize_result(raw: Any, raw_name: str) -> dict[str, Any]:
    """Validate/repair the Gemini output into the schema defined in the prompt."""
    if not isinstance(raw, dict):
        raise ValueError("Model did not return a JSON object.")

    brand = _clean_str(raw.get("brand"))
    guarded_brand = _guard_brand(raw_name, brand)
    brand_interpreted = guarded_brand != brand
    if brand_interpreted:
        brand = guarded_brand
    product_name = _clean_str(raw.get("product_name"))
    store_name: str | None = None
    if brand and brand.casefold() in _STORE_NAMES:
        # a store prefix (WW, COL, Woolworths, ...) is the retailer, not a brand
        store_name = brand
        if product_name:
            stripped = _strip_brand_prefix(product_name, store_name)
            product_name = None if stripped is None else (stripped or product_name)
        brand = None
    if product_name:
        product_name = _strip_leading_store(product_name)
    variant = _clean_str(raw.get("variant"))
    category = _clean_str(raw.get("category"))
    subcategory = _clean_str(raw.get("subcategory"))
    size_value = _as_number(raw.get("size_value"), lo=0.0)
    raw_unit = _clean_str(raw.get("size_unit"))
    size_unit = _as_unit(raw.get("size_unit"))
    raw_size = _clean_str(raw.get("raw_size")) or _extract_raw_size(raw_name)
    unsupported_unit = raw_unit.casefold() if raw_unit and raw_unit.casefold() in _UNSUPPORTED_UNITS else None
    last_unit = _last_raw_unit(raw_name)
    if last_unit and last_unit in _UNSUPPORTED_UNITS:
        unsupported_unit = last_unit  # receipt states an unsupported measurement
        size_value = None
        size_unit = None
    if size_value is not None and size_unit is None:
        size_value = None
    pack_count = _as_number(raw.get("pack_count"), lo=0.0)
    if pack_count is not None and pack_count <= 1:
        pack_count = None  # pack count only meaningful for multipacks

    explicit = _explicit_per_unit(raw_name)
    if explicit:
        # "N x SIZE" on the receipt is authoritative — fixes any model
        # division (e.g. 6 x 360mL must stay 360, never 60) and mis-parses.
        pack_count = explicit["pack_count"]
        size_value = explicit["size_value"]
        size_unit = explicit["size_unit"]
        size_basis = "per_unit"
        unsupported_unit = None
    elif (
        pack_count is not None and size_value is not None
        and float(pack_count) == float(size_value)
        and not _has_real_size_expr(raw_name)
    ):
        # pack count was copied into size_value (e.g. "80PK" → 80 each)
        size_value = None
        size_unit = None
        size_basis = None
    else:
        basis_raw = _clean_str(raw.get("size_basis"))
        if basis_raw in {"per_unit", "total", "unknown"}:
            size_basis = basis_raw
        elif size_value is not None:
            size_basis = "unknown"
        else:
            size_basis = None
    leading_brand = _starts_with_numeric_brand(raw_name)
    if leading_brand and not _has_real_size_expr(raw_name):
        # any "size" the model extracted is really the leading brand token
        unsupported_unit = None
        size_value = None
        size_unit = None
        size_basis = None
    barcode = _clean_str(raw.get("barcode"))
    canonical_name = _build_canonical_name({
        "brand": brand, "product_name": product_name, "variant": variant,
        "size_value": size_value, "size_unit": size_unit, "pack_count": pack_count,
        "size_basis": size_basis,
    })
    if not canonical_name and product_name:
        canonical_name = _clean_str(raw.get("canonical_name"))

    extra_ambiguities: list[str] = []
    if brand_interpreted:
        extra_ambiguities.append(
            f"Brand interpreted from compressed abbreviation as '{guarded_brand}'."
        )
    if store_name:
        extra_ambiguities.append(
            f"'{store_name}' is a retailer/store, not a product brand; brand set to null."
        )
    confidence = _as_confidence(raw.get("confidence"))
    attr_raw = raw.get("attribute_confidence")
    attribute_confidence: dict[str, float] = {}
    if isinstance(attr_raw, dict):
        for field in _ATTR_FIELDS:
            attribute_confidence[field] = _as_confidence(attr_raw.get(field))
    else:
        attribute_confidence = {field: 0.0 for field in _ATTR_FIELDS}

    ambiguities_raw = raw.get("ambiguities")
    ambiguities: list[str] = []
    if isinstance(ambiguities_raw, str):
        ambiguities = [ambiguities_raw.strip()] if ambiguities_raw.strip() else []
    elif isinstance(ambiguities_raw, list):
        ambiguities = [str(a).strip() for a in ambiguities_raw if isinstance(a, str) and a.strip()]

    if unsupported_unit:
        msg = (
            f"Unsupported size unit '{unsupported_unit}' (e.g. length/roll/sheet/count); "
            "normalized size left null, original preserved in raw_size."
        )
        if not any(
            unsupported_unit in a.casefold()
            or "unsupported" in a.casefold()
            or "length" in a.casefold()
            for a in ambiguities
        ):
            ambiguities.append(msg)
    ambiguities.extend(extra_ambiguities)

    seen: set[str] = set()
    deduped: list[str] = []
    for a in ambiguities:
        key = a.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    ambiguities = deduped

    return {
        "raw_name": raw_name,
        "brand": brand,
        "product_name": product_name,
        "variant": variant,
        "category": category,
        "subcategory": subcategory,
        "size_value": size_value,
        "size_unit": size_unit,
        "size_basis": size_basis,
        "raw_size": raw_size,
        "pack_count": pack_count,
        "barcode": barcode,
        "canonical_name": canonical_name,
        "confidence": confidence,
        "attribute_confidence": attribute_confidence,
        "ambiguities": ambiguities,
    }


# ---------------------------------------------------------------------------
# Gemini transport
# ---------------------------------------------------------------------------

def _gemini_client():
    from google import genai

    api_key = _env("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing from .env")
    return genai.Client(api_key=api_key)


def _extract_usage(response: Any, model: str, elapsed_sec: float) -> dict[str, Any]:
    um = getattr(response, "usage_metadata", None)
    prompt = output = thoughts = total = 0
    if um is not None:
        prompt = int(getattr(um, "prompt_token_count", None) or 0)
        output = int(getattr(um, "candidates_token_count", None) or 0)
        thoughts = int(getattr(um, "thoughts_token_count", None) or 0)
        total = int(getattr(um, "total_token_count", None) or (prompt + output + thoughts))
    return {
        "model": model,
        "prompt_tokens": prompt,
        "thoughts_tokens": thoughts,
        "output_tokens": output,
        "total_tokens": total,
        "elapsed_sec": round(elapsed_sec, 1),
        "cached": False,
    }


def canonicalize(
    line: str,
    model: str | None = None,
    use_cache: bool = True,
    prompt_file: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Canonicalise one raw receipt line. Returns (result, usage).

    ``model`` is ignored: ``gemini-3.1-flash-lite`` is always used and can
    never be overridden.
    """
    load_env()
    line = line.strip()
    if not line:
        raise ValueError("Receipt line must not be empty.")
    model = DEFAULT_MODEL

    path = _cache_path(line, model)
    if use_cache:
        with _lock:
            cached = _cache_load(path)
        if cached is not None:
            cached = normalize_result(cached, cached.get("raw_name") or line)
            return cached, _cached_usage(model)

    from google.genai import types

    prompt = load_prompt(prompt_file)
    client = _gemini_client()
    start = time.monotonic()
    response = client.models.generate_content(
        model=model,
        contents=line,
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    elapsed = time.monotonic() - start
    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    result = normalize_result(json.loads(_clean_json(response.text)), line)
    usage = _extract_usage(response, model, elapsed)

    if use_cache:
        with _lock:
            _cache_save(path, result)
    return result, usage


def canonicalize_many(
    lines: list[str],
    model: str | None = None,
    use_cache: bool = True,
    workers: int = 1,
    prompt_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Canonicalise several receipt lines. A per-line failure becomes a
    schema-shaped result with confidence 0 rather than aborting the run."""
    results: list[dict[str, Any]] = []
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        def run(line: str) -> dict[str, Any]:
            try:
                res, _ = canonicalize(line, model, use_cache, prompt_file)
                return res
            except Exception as exc:  # noqa: BLE001
                return _error_result(line, exc)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run, lines))
    else:
        for line in lines:
            try:
                res, _ = canonicalize(line, model, use_cache, prompt_file)
            except Exception as exc:  # noqa: BLE001
                res = _error_result(line, exc)
            results.append(res)
    return results


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(line: str, model: str) -> Path:
    digest = hashlib.sha1(f"{line.strip().casefold()}|{model}".encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"canonical_{digest}.json"


def _cache_load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_save(path: Path, result: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _cached_usage(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "prompt_tokens": 0, "thoughts_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "elapsed_sec": 0.0, "cached": True,
    }


# ---------------------------------------------------------------------------
# Error result / Mock mode
# ---------------------------------------------------------------------------

def _error_result(line: str, exc: Exception) -> dict[str, Any]:
    return {
        "raw_name": line,
        "brand": None, "product_name": None, "variant": None,
        "category": None, "subcategory": None,
        "size_value": None, "size_unit": None, "size_basis": None,
        "raw_size": None, "pack_count": None,
        "barcode": None, "canonical_name": None,
        "confidence": 0.0,
        "attribute_confidence": {field: 0.0 for field in _ATTR_FIELDS},
        "ambiguities": [f"Failed to canonicalise: {exc}"],
    }


def canonicalize_mock(line: str, model: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic demo results for a handful of well-known receipt lines.
    Unknown lines fall back to a schema-shaped result with confidence 0.
    ``model`` is ignored — the hard-coded ``gemini-3.1-flash-lite`` is used."""
    line = line.strip()
    model = DEFAULT_MODEL
    mock = {
        "COCA-COLA Z/SUG 2L": {
            "raw_name": line, "brand": "Coca-Cola", "product_name": "Coca-Cola",
            "variant": "Zero Sugar", "category": "Beverages", "subcategory": "Soft Drinks",
            "size_value": 2, "size_unit": "L", "size_basis": None, "raw_size": "2L",
            "pack_count": None, "barcode": None,
            "canonical_name": "Coca-Cola Zero Sugar 2L", "confidence": 0.97,
            "attribute_confidence": {"brand": 0.98, "product_name": 0.95, "variant": 0.9,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": [],
        },
        "LURPAK S/S 250g": {
            "raw_name": line, "brand": "Lurpak", "product_name": "Butter",
            "variant": "Slightly Salted", "category": "Dairy", "subcategory": "Butter",
            "size_value": 250, "size_unit": "g", "size_basis": None, "raw_size": "250g",
            "pack_count": None, "barcode": None,
            "canonical_name": "Lurpak Slightly Salted Butter 250g", "confidence": 0.96,
            "attribute_confidence": {"brand": 0.99, "product_name": 0.95, "variant": 0.92,
                                     "size": 0.99, "pack_count": 1.0, "category": 0.98},
            "ambiguities": [],
        },
        "COLES MILK 2L": {
            "raw_name": line, "brand": None, "product_name": "Milk",
            "variant": None, "category": "Dairy", "subcategory": "Milk",
            "size_value": 2, "size_unit": "L", "size_basis": None, "raw_size": "2L",
            "pack_count": None, "barcode": None,
            "canonical_name": "Milk 2L", "confidence": 0.95,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.98},
            "ambiguities": ["'Coles' is a retailer/store, not a product brand; brand set to null."],
        },
        "COKE 6 X 375ML": {
            "raw_name": line, "brand": "Coca-Cola", "product_name": "Coca-Cola",
            "variant": None, "category": "Beverages", "subcategory": "Soft Drinks",
            "size_value": 375, "size_unit": "mL", "size_basis": "per_unit", "raw_size": "6 X 375ML",
            "pack_count": 6, "barcode": None,
            "canonical_name": "Coca-Cola 6 x 375mL", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.95, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.97, "pack_count": 0.99, "category": 0.95},
            "ambiguities": [],
        },
        "SMITH'S ORIGINAL POTATO CHIPS 170G": {
            "raw_name": line, "brand": "Smith's", "product_name": "Potato Chips",
            "variant": "Original", "category": "Snacks", "subcategory": "Crisps",
            "size_value": 170, "size_unit": "g", "size_basis": None, "raw_size": "170G",
            "pack_count": None, "barcode": None,
            "canonical_name": "Smith's Original Potato Chips 170g", "confidence": 0.97,
            "attribute_confidence": {"brand": 0.99, "product_name": 0.95, "variant": 0.93,
                                     "size": 0.99, "pack_count": 1.0, "category": 0.96},
            "ambiguities": [],
        },
        "A2 FULL CREAM MLK 2L": {
            "raw_name": line, "brand": "a2", "product_name": "Full Cream Milk",
            "variant": "Full Cream", "category": "Dairy", "subcategory": "Milk",
            "size_value": 2, "size_unit": "L", "size_basis": None, "raw_size": "2L",
            "pack_count": None, "barcode": None,
            "canonical_name": "a2 Full Cream Milk 2L", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.9, "product_name": 0.95, "variant": 0.7,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.98},
            "ambiguities": ["Variant 'Full Cream' already contained in product name."],
        },
        "MACRO ORGANIC COCONUT OIL 300G": {
            "raw_name": line, "brand": "Macro Organic", "product_name": "Coconut Oil",
            "variant": "Organic", "category": "Pantry", "subcategory": "Cooking Oil",
            "size_value": 300, "size_unit": "g", "size_basis": None, "raw_size": "300G",
            "pack_count": None, "barcode": None,
            "canonical_name": "Macro Organic Coconut Oil 300g", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.95, "product_name": 0.95, "variant": 0.8,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": ["Variant 'Organic' already contained in brand."],
        },
        "LITTLE ONES BABY WIPES 80PK": {
            "raw_name": line, "brand": "Little Ones", "product_name": "Baby Wipes",
            "variant": None, "category": "Baby", "subcategory": "Wipes",
            "size_value": None, "size_unit": None, "size_basis": None, "raw_size": None,
            "pack_count": 80, "barcode": None,
            "canonical_name": "Little Ones Baby Wipes 80 pack", "confidence": 0.92,
            "attribute_confidence": {"brand": 0.95, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.0, "pack_count": 0.98, "category": 0.9},
            "ambiguities": [],
        },
        "3M COMMAND HOOKS MINI 6PK": {
            "raw_name": line, "brand": "3M", "product_name": "Command Hooks Mini",
            "variant": None, "category": "Home", "subcategory": "Hooks & Fixings",
            "size_value": None, "size_unit": None, "size_basis": None, "raw_size": None,
            "pack_count": 6, "barcode": None,
            "canonical_name": "3M Command Hooks Mini 6 pack", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.95, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.0, "pack_count": 0.98, "category": 0.9},
            "ambiguities": [],
        },
        "WW PASTA SPIRALS 500G": {
            "raw_name": line, "brand": None, "product_name": "Pasta Spirals",
            "variant": None, "category": "Pantry", "subcategory": "Pasta",
            "size_value": 500, "size_unit": "g", "size_basis": None, "raw_size": "500G",
            "pack_count": None, "barcode": None,
            "canonical_name": "Pasta Spirals 500g", "confidence": 0.95,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": ["'Woolworths' is a retailer/store, not a product brand; brand set to null."],
        },
        "WW FULL CREAM MILK 2L": {
            "raw_name": line, "brand": None, "product_name": "Full Cream Milk",
            "variant": None, "category": "Dairy", "subcategory": "Milk",
            "size_value": 2, "size_unit": "L", "size_basis": None, "raw_size": "2L",
            "pack_count": None, "barcode": None,
            "canonical_name": "Full Cream Milk 2L", "confidence": 0.95,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": ["'Woolworths' is a retailer/store, not a product brand; brand set to null."],
        },
        "W/W BUTTERED POPCORN 100G": {
            "raw_name": line, "brand": None, "product_name": "Buttered Popcorn",
            "variant": None, "category": "Snacks", "subcategory": "Crisps",
            "size_value": 100, "size_unit": "g", "size_basis": None, "raw_size": "100G",
            "pack_count": None, "barcode": None,
            "canonical_name": "Buttered Popcorn 100g", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": ["'W/W' is a retailer/store, not a product brand; brand set to null."],
        },
        "COL FROZEN PEAS 500G": {
            "raw_name": line, "brand": None, "product_name": "Frozen Peas",
            "variant": None, "category": "Frozen", "subcategory": "Frozen Vegetables",
            "size_value": 500, "size_unit": "g", "size_basis": None, "raw_size": "500G",
            "pack_count": None, "barcode": None,
            "canonical_name": "Frozen Peas 500g", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": ["'COL' is a retailer/store, not a product brand; brand set to null."],
        },
        "ALDI BUTTER 250G": {
            "raw_name": line, "brand": None, "product_name": "Butter",
            "variant": None, "category": "Dairy", "subcategory": "Butter",
            "size_value": 250, "size_unit": "g", "size_basis": None, "raw_size": "250G",
            "pack_count": None, "barcode": None,
            "canonical_name": "Butter 250g", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.98, "pack_count": 1.0, "category": 0.95},
            "ambiguities": ["'ALDI' is a retailer/store, not a product brand; brand set to null."],
        },
        "IGA FREE RANGE EGGS 12": {
            "raw_name": line, "brand": None, "product_name": "Free Range Eggs",
            "variant": None, "category": "Dairy & Eggs", "subcategory": "Eggs",
            "size_value": None, "size_unit": None, "size_basis": None, "raw_size": None,
            "pack_count": 12, "barcode": None,
            "canonical_name": "Free Range Eggs 12 pack", "confidence": 0.9,
            "attribute_confidence": {"brand": 0.0, "product_name": 0.95, "variant": 0.0,
                                     "size": 0.0, "pack_count": 0.98, "category": 0.95},
            "ambiguities": ["'IGA' is a retailer/store, not a product brand; brand set to null."],
        },
    }
    entry = mock.get(line.upper())
    if entry is None:
        entry = _error_result(line, RuntimeError("Mock mode has no entry for this line."))
    usage = _cached_usage(model)
    usage["cached"] = False
    usage["model"] = model
    return dict(entry), usage
