import json
import logging
import os
import re

import db

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.1-flash-lite"

log = logging.getLogger("rcb")


def _client():
    """Return (client, model) using Gemini (OpenAI-compatible) or OpenAI fallback."""
    try:
        from openai import OpenAI
    except ImportError:
        return None, None
    gem_key = os.environ.get("GEMINI_API_KEY")
    if gem_key:
        return OpenAI(base_url=GEMINI_BASE, api_key=gem_key), GEMINI_MODEL
    oai_key = os.environ.get("OPENAI_API_KEY")
    if oai_key:
        return OpenAI(api_key=oai_key), "gpt-4o-mini"
    return None, None


def _build_prompt(canonical, product, store, size_tolerant=False):
    cid = canonical.get("canonical_id") or canonical.get("canonical_name")
    cname = canonical.get("canonical_name") or cid
    size = ""
    if canonical.get("size_value") is not None and canonical.get("size_unit"):
        size = f", size={canonical['size_value']} {canonical['size_unit']}"
    size_rule = (
        "- The store product is a DIFFERENT SIZE of the canonical product (we are matching the "
        "closest available weight). A different size (e.g. 125g vs 170g) is STILL THE SAME "
        "PRODUCT and must NEVER be a reason to answer NO. Judge only on brand, product type, "
        "variant, and STATE (e.g. Fresh vs Frozen IS a different product => NO).\n"
        if size_tolerant
        else "- Match on size too (e.g. '2L' vs '1L' are DIFFERENT).\n"
    )
    return (
        "You are a grocery product matcher for a price comparison service. "
        "Decide whether the STORE PRODUCT is the same physical product as the CANONICAL PRODUCT.\n\n"
        f"CANONICAL: name=\"{cname}\", brand=\"{canonical.get('brand') or '?'}\", "
        f"product_type=\"{canonical.get('product_name') or '?'}\"{size}\n"
        f"STORE PRODUCT: name=\"{product.get('name')}\", store=\"{store}\", "
        f"price={product.get('price')}\n\n"
        "Rules:\n"
        "- Match on brand (e.g. 'a2' vs 'Black & Gold' are DIFFERENT), product type, and "
        "variant (e.g. 'Full Cream' vs 'Light' are DIFFERENT products).\n"
        "- A product state such as Fresh vs Frozen is a DIFFERENT product => answer NO.\n"
        f"{size_rule}"
        "- When the canonical HAS a brand, the store product must be that same brand. A store's "
        "own private-label / store-brand equivalent (e.g. ALDI 'Tasty Cheese' for canonical "
        "'Bega Tasty Cheese', Woolworths/Coles home-brand) is a DIFFERENT brand => answer NO.\n"
        "- A store may omit the brand even for the correct product (e.g. Coles 'Command Clear Mini "
        "Hooks' is the 3M product). Use your knowledge of brands to decide.\n"
        "- Respond with JSON only: {\"match\": \"yes\"|\"no\", \"reason\": \"short reason\"}"
    )


def _parse(text):
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return data.get("match"), data.get("reason")
        except Exception:
            pass
    return "no", "unparseable LLM response"


def confirm_match(canonical, product, store, country="AU", size_tolerant=False):
    """Returns (decision, reason) where decision is True/False (same product or not).

    size_tolerant=True uses a prompt that ignores size (closest-weight matching) and
    caches under mode='size' so it never collides with the strict 'confirm' cache.
    """
    cid = canonical.get("canonical_id")
    purl = product.get("url") or f"__{product.get('name')}"
    mode = "size" if size_tolerant else "confirm"
    if cid and purl and not purl.startswith("__"):
        cached = db.get_match_decision(cid, store, purl, country, mode)
        if cached is not None:
            return cached == "yes", f"cached({mode}:{cached})"

    client, model = _client()
    if client is None:
        return False, "no AI configured"

    prompt = _build_prompt(canonical, product, store, size_tolerant)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        text = resp.choices[0].message.content
        decision, reason = _parse(text)
        if decision not in ("yes", "no"):
            decision = "no"
    except Exception as e:
        log.warning("ai confirm error for %s @ %s: %s", cid, store, e)
        return False, f"ai error: {type(e).__name__}"

    if cid and purl and not purl.startswith("__"):
        db.save_match_decision(cid, store, purl, product.get("name"), decision, reason, country, mode)
    return decision == "yes", reason
