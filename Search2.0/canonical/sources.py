"""Receipt source readers: text files, PDF e-receipts, and photo OCR.

Each reader returns entries as dicts::

    {
        "path": Path,          # source file
        "line_no": int,        # 1-based line / entry index
        "raw_name": str,       # the receipt line fed to the canonical engine
        "source_text": str,    # full original text of the entry (provenance)
        "source_type": str,    # "txt" | "pdf" | "image"
    }
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

TEXT_EXT = {".txt"}
PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
ALL_EXTS = TEXT_EXT | PDF_EXT | IMAGE_EXT

# A leading marker (loyalty/marketing flags) before a product name.
_LEADING_MARKERS = re.compile(r"^[#^~*@\s]+")

# Right-aligned price column: 2+ spaces then a decimal amount.
_PRICE_SUFFIX = re.compile(r"\s{2,}\d+(?:\.\d{2})\s*$")

# PDF detail lines.
_QTY_DETAIL = re.compile(r"^qty\s+(\d+)\s*@", re.I)
_WEIGHT_DETAIL = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?)\s*(?:kg|g)\b.*?(?:NET\s*)?@\s*\$", re.I
)
_XS_DETAIL = re.compile(r"^(\d+)\s*x\s*\d+(?:\.\d+)?\s*(?:g|kg|ml|l)\b", re.I)

# OCR continuation / weight fragments.
_OCR_WEIGHT = re.compile(r"^\s*([\d.]+)\s*(kg|g|ml|mL|L)\b.*@", re.I)
_OCR_FRAGMENT = re.compile(
    r"^\s*[A-Za-z]\s*\d+(?:\.\d+)?\s*(?:g|kg|ml|l|mL|L)\s*$", re.I
)


def find_receipt_files(roots: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.casefold() in ALL_EXTS:
                files.add(p.resolve())
    return sorted(files, key=lambda p: p.name.casefold())


def read_txt(path: Path) -> list[dict]:
    entries: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            entries.append({
                "path": path.resolve(),
                "line_no": i,
                "raw_name": line,
                "source_text": line,
                "source_type": "txt",
            })
    return entries


def _pdf_lines(path: Path) -> list[str]:
    from pypdf import PdfReader

    text: list[str] = []
    reader = PdfReader(str(path))
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            page_text = ""
        text.extend(line for line in page_text.splitlines())
    return text


def read_pdf(path: Path) -> list[dict]:
    """Parse a Woolworths-style e-receipt PDF into product entries."""
    lines = _pdf_lines(path)
    entries: list[dict] = []
    current: dict | None = None
    started = False

    for line in lines:
        s = line.strip()
        if not started:
            if "description" in s.casefold():
                started = True
            continue
        low = s.casefold()
        if "subtotal" in low or "total" in low and "$" in s:
            break
        if not s or s.startswith("---") or low in ("promotional price", "taxable items"):
            continue

        # bare price line
        if re.fullmatch(r"\$?\d+(?:\.\d{2})?", s):
            continue

        # quantity / weight / "N x size" detail line for the previous entry
        if _QTY_DETAIL.match(s):
            m = _QTY_DETAIL.match(s)
            if current:
                current["detail"] = s
                current["qty"] = int(m.group(1))
            continue
        if _WEIGHT_DETAIL.match(s) or _XS_DETAIL.match(s):
            if current:
                current["detail"] = s
            continue

        # otherwise a product name line
        name = _LEADING_MARKERS.sub("", s)
        name = _PRICE_SUFFIX.sub("", name).strip()
        if not name:
            continue
        current = {
            "path": path.resolve(),
            "line_no": len(entries) + 1,
            "name": name,
            "detail": None,
            "qty": None,
        }
        entries.append(current)

    result: list[dict] = []
    for e in entries:
        raw_name = e["name"]
        if e.get("qty"):
            raw_name = f"{raw_name} Qty {e['qty']}"
        source_text = e["name"]
        if e.get("detail"):
            source_text = f"{source_text} / {e['detail']}"
        result.append({
            "path": e["path"],
            "line_no": e["line_no"],
            "raw_name": raw_name,
            "source_text": source_text,
            "source_type": "pdf",
        })
    return result


def _ocr_lines(path: Path) -> list[str]:
    from google import genai
    from google.genai import types

    from .engine import _env, load_env

    load_env()
    api_key = _env("GOOGLE_API_KEY")
    model = _env("GEMINI_VISION_MODEL") or "gemini-3.5-flash-lite"
    client = genai.Client(api_key=api_key)
    mime = "image/jpeg" if path.suffix.casefold() in (".jpg", ".jpeg") else "image/png"
    data = path.read_bytes()
    prompt = (
        "Extract the product lines from this grocery store receipt. "
        "Return one line per purchased product, in order, exactly as printed. "
        "Keep product description, brand, pack size (e.g. 2L, 500g), quantity (e.g. Qty 2) "
        "and weight (e.g. 0.860 kg) as printed. "
        "Do NOT include prices, subtotals, totals, store header, card or payment details. "
        "Do not number the lines. Return plain text only."
    )
    resp = client.models.generate_content(
        model=model,
        contents=types.Content(
            parts=[
                types.Part.from_bytes(data=data, mime_type=mime),
                types.Part.from_text(text=prompt),
            ]
        ),
    )
    return (resp.text or "").splitlines()


def read_image(path: Path) -> list[dict]:
    """OCR a receipt photo, merging weight lines back into their product."""
    lines = _ocr_lines(path)
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if not s:
            continue
        low = s.casefold()
        if low.startswith("pkg disc") or "[" in s and "for $" in low:
            continue  # discount / packaging-noise lines
        wm = _OCR_WEIGHT.match(s)
        if wm and entries:
            # "0.860kg @ $10.99/kg" → attach weight to the previous product
            prev = entries[-1]
            prev["raw_name"] = f"{prev['raw_name']} {wm.group(1)}{wm.group(2)}"
            prev["source_text"] = f"{prev['source_text']} / {s}"
            continue
        # continuation fragment like "S 500G" joins the previous name
        if _OCR_FRAGMENT.match(s) and entries and not entries[-1]["raw_name"].endswith(")"):
            prev = entries[-1]
            prev["raw_name"] = f"{prev['raw_name']} {s}"
            prev["source_text"] = f"{prev['source_text']} {s}"
            continue
        entries.append({
            "path": path.resolve(),
            "line_no": len(entries) + 1,
            "raw_name": s,
            "source_text": s,
            "source_type": "image",
        })
    return entries


def read_receipt(path: Path) -> list[dict]:
    ext = path.suffix.casefold()
    if ext in TEXT_EXT:
        return read_txt(path)
    if ext in PDF_EXT:
        return read_pdf(path)
    if ext in IMAGE_EXT:
        return read_image(path)
    return []
