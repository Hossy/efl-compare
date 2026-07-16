"""
Extract the wizard's inputs from a user's electricity bill PDF.

Two passes:

  1. READ — get the bill's words. Most bills are generated digitally and carry a
     text layer, which is exact and free; that is used when present. Only when
     there is no usable text (a scan, a photo, an image-only export) is the page
     rasterised and shown to the multimodal model.
  2. STRUCTURE — the text is handed to the endpoint with a JSON schema, pinning
     it to the exact fields the wizard needs.

Preferring the text layer is not just an optimisation. On a real TXU bill the
vision pass spent its budget narrating page 1's usage bar chart (transcribing
the Y-axis labels) and never reached "Energy Charge (3753 kWh x $0.114)" on page
2 -- it returned a bill total but no usage, which is the one field that matters.
The text layer has that line verbatim.

What a bill does NOT contain: the early-termination fee and the contract end
date. Those live in the contract / Electricity Facts Label, not the monthly
statement. The wizard asks for them separately rather than pretending to read
them here.

Every field comes back as a string ("" when unknown) rather than a nullable
type: llama.cpp compiles the schema into a sampling grammar, and plain strings
are the shape that survives every server. Coercion happens here, where a bad
parse degrades to "unknown" instead of crashing.
"""

from __future__ import annotations

import base64
import json
import re

import llm_backend

# Guard rails for a publicly reachable endpoint. Scanned bills are images and
# run large -- a 2-page 150dpi scan is ~12MB -- so the cap has to clear that or
# it rejects exactly the bills that need the vision path.
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 4
RENDER_DPI = 150
MAX_EDGE_PX = 1600
# Below this many characters across the document, assume there is no real text
# layer (a scan often yields a few stray characters) and fall back to vision.
MIN_TEXT_LAYER_CHARS = 250

# The comparison data is Oncor-only, so a bill from another utility would be
# priced against the wrong delivery charges.
SUPPORTED_TDU = "oncor"


class BillParseError(RuntimeError):
    """Raised with a user-facing message when a bill cannot be read."""


_SCHEMA = {
    "type": "object",
    "properties": {
        "usage_kwh":                   {"type": "string"},
        "days_in_period":              {"type": "string"},
        "total_bill_dollars":          {"type": "string"},
        "taxes_dollars":               {"type": "string"},
        "energy_rate_dollars_per_kwh": {"type": "string"},
        "avg_price_cents_per_kwh":     {"type": "string"},
        "service_zip":                 {"type": "string"},
        "tdu":                         {"type": "string"},
        "provider":                    {"type": "string"},
        "plan":                        {"type": "string"},
        "rate_type":                   {"type": "string"},
    },
    "required": list(),  # filled below
}
_SCHEMA["required"] = list(_SCHEMA["properties"])

_VISION_PROMPT = (
    "This is a residential electricity bill. Transcribe the billing facts as a "
    "plain list, copying numbers exactly as printed. Focus on: electricity used "
    "in kWh for the period, number of days in the billing period, total amount "
    "due, taxes, the energy charge rate per kWh, the average price per kWh, the "
    "service address ZIP code, the delivery utility (TDU) name, the retail "
    "provider, and the plan name. IGNORE the usage history bar chart and any "
    "marketing text -- do not transcribe chart axes. If something is not shown, "
    "do not guess it."
)

_STRUCTURE_SYSTEM = (
    "You turn an electricity bill into strict JSON. Use only what the text "
    "states. Never estimate, infer or invent: if a field is not clearly present, "
    "return an empty string.\n"
    "Rules:\n"
    "- usage_kwh: digits only, no separators (e.g. '3753'). The electricity used "
    "in THIS billing period. Prefer a 'Billed Usage' or 'Usage (kWh)' column; the "
    "quantity inside a line like 'Energy Charge (3753 kWh x $0.114)' is also it. "
    "Never take a number from a usage-history chart, and never a dollar amount.\n"
    "- days_in_period: digits only (e.g. '30'), the days in the billing cycle.\n"
    "- total_bill_dollars: digits/decimal point, no currency sign (e.g. '686.90'). "
    "The total amount due or current charges for this period.\n"
    "- taxes_dollars: sales tax charged, same format.\n"
    "- energy_rate_dollars_per_kwh: the per-kWh energy charge in DOLLARS "
    "(e.g. '0.114').\n"
    "- avg_price_cents_per_kwh: the stated average price in CENTS (e.g. '17.8').\n"
    "- service_zip: the 5-digit ZIP of the SERVICE address (not the payment "
    "address of the provider).\n"
    "- tdu: the transmission/distribution utility name (e.g. 'Oncor').\n"
    "- provider: the retail provider (e.g. 'TXU Energy'). plan: the plan/product "
    "name.\n"
    "- rate_type: 'variable' if the bill indicates a variable price, 'fixed' if "
    "it indicates a fixed price, else ''."
)


def _pdf_text(pdf_bytes: bytes) -> tuple[str, int]:
    """Return (text, page_count) from the PDF's text layer."""
    import fitz
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise BillParseError("That file could not be opened as a PDF.") from exc
    if doc.page_count == 0:
        raise BillParseError("That PDF has no pages.")
    parts = [p.get_text() for p in list(doc)[:MAX_PAGES]]
    return "\n".join(parts).strip(), doc.page_count


def _pdf_to_png_data_uris(pdf_bytes: bytes) -> list[str]:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    uris = []
    for page in list(doc)[:MAX_PAGES]:
        pix = page.get_pixmap(dpi=RENDER_DPI)
        if max(pix.width, pix.height) > MAX_EDGE_PX:
            scale = MAX_EDGE_PX / max(pix.width, pix.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale * RENDER_DPI / 72,
                                                     scale * RENDER_DPI / 72))
        uris.append("data:image/png;base64," +
                    base64.b64encode(pix.tobytes("png")).decode("ascii"))
    return uris


def _num(s) -> float | None:
    if s in (None, ""):
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _read_bill_text(pdf_bytes: bytes, backend, vision_prompt: str = None) -> tuple[str, str]:
    """Return (text, how) where how is 'text-layer' or 'vision'."""
    vision_prompt = vision_prompt or _VISION_PROMPT
    text, _pages = _pdf_text(pdf_bytes)
    if len(text) >= MIN_TEXT_LAYER_CHARS:
        return text, "text-layer"

    images = _pdf_to_png_data_uris(pdf_bytes)
    if not images:
        raise BillParseError("That PDF has no readable pages.")
    content = [{"type": "text", "text": vision_prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    out = backend.create_chat_completion(
        messages=[{"role": "user", "content": content}],
        temperature=0.0,
        max_tokens=1500,
    )["choices"][0]["message"]["content"].strip()
    if not out:
        raise BillParseError("The bill could not be read. Try a clearer scan.")
    return out, "vision"


def parse_bill(pdf_bytes: bytes) -> dict:
    """Return the wizard's prefill values extracted from a bill PDF.

    Unknown numbers come back as None and unknown text as "". Raises
    BillParseError with a user-facing message.
    """
    if not pdf_bytes:
        raise BillParseError("No file was received.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise BillParseError(
            f"That PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB. "
            "Please upload just your bill.")
    if not pdf_bytes.lstrip()[:5].startswith(b"%PDF"):
        raise BillParseError("That doesn't look like a PDF file.")

    backend = llm_backend.ChatBackend()
    text, how = _read_bill_text(pdf_bytes, backend)

    raw = backend.create_chat_completion(
        messages=[
            {"role": "system", "content": _STRUCTURE_SYSTEM},
            {"role": "user", "content": "Bill:\n\n" + text[:24000]},
        ],
        temperature=0.0,
        max_tokens=400,
        response_schema=_SCHEMA,
        schema_name="bill_fields",
    )["choices"][0]["message"]["content"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BillParseError("The bill was read but could not be understood.") from exc

    usage = _num(data.get("usage_kwh"))
    days = _num(data.get("days_in_period"))
    total = _num(data.get("total_bill_dollars"))
    taxes = _num(data.get("taxes_dollars"))
    rate = _num(data.get("energy_rate_dollars_per_kwh"))
    avg_c = _num(data.get("avg_price_cents_per_kwh"))
    zip_m = re.search(r"\b(\d{5})\b", str(data.get("service_zip") or ""))
    tdu = (data.get("tdu") or "").strip()
    rate_type = (data.get("rate_type") or "").strip().lower()

    # Sanity bounds. The classic failure is a dollar amount landing in usage, so
    # anything outside what a home could consume is treated as unread.
    if usage is not None and not (1 <= usage <= 100_000):
        usage = None
    if days is not None and not (1 <= days <= 200):
        days = None
    # A per-kWh energy charge is cents-scale in dollars; a stray total would not be.
    if rate is not None and not (0 < rate < 1):
        rate = None
    if avg_c is not None and not (0 < avg_c < 100):
        avg_c = None

    warnings = []
    if tdu and SUPPORTED_TDU not in tdu.lower():
        warnings.append(
            f"This bill's delivery utility looks like {tdu}, but these plans are "
            "priced for the Oncor area — the comparison may not apply to you.")
    if days is not None and not (25 <= days <= 35):
        warnings.append(
            f"This bill covers {days:g} days, not a typical month, so your usage "
            "here isn't a normal monthly figure.")

    return {
        "usage_kwh":            int(usage) if usage is not None else None,
        "days_in_period":       int(days) if days is not None else None,
        "total_bill_dollars":   round(total, 2) if total is not None else None,
        "taxes_dollars":        round(taxes, 2) if taxes is not None else None,
        "energy_rate_cents":    round(rate * 100, 4) if rate is not None else None,
        "avg_price_cents":      round(avg_c, 2) if avg_c is not None else None,
        "service_zip":          zip_m.group(1) if zip_m else "",
        "tdu":                  tdu,
        "provider":             (data.get("provider") or "").strip(),
        "plan":                 (data.get("plan") or "").strip(),
        "rate_type":            rate_type if rate_type in ("fixed", "variable") else "",
        "warnings":             warnings,
        "read_via":             how,
    }


# ---------------------------------------------------------------------------
# Contracts / Electricity Facts Labels
#
# The bill says what you used and paid. The contract says what leaving costs.
# Those are the two numbers the break-even verdict needs and a bill never has,
# so this reads them from the document that does.
# ---------------------------------------------------------------------------

_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "exit_fee_dollars":  {"type": "string"},
        "contract_end":      {"type": "string"},
        "contract_start":    {"type": "string"},
        "term_months":       {"type": "string"},
        "contract_type":     {"type": "string"},
        "energy_rate_cents": {"type": "string"},
        "provider":          {"type": "string"},
        "plan":              {"type": "string"},
    },
    "required": [],
}
_CONTRACT_SCHEMA["required"] = list(_CONTRACT_SCHEMA["properties"])

_CONTRACT_VISION_PROMPT = (
    "This is a residential electricity contract or Electricity Facts Label (EFL). "
    "Transcribe the contract terms as a plain list, copying numbers exactly as "
    "printed. Focus on: the early-termination / cancellation fee, the contract "
    "term length in months, the contract start and end dates, the energy charge "
    "per kWh, the retail provider, and the plan name. If something is not shown, "
    "do not guess it."
)

_CONTRACT_SYSTEM = (
    "You turn an electricity contract / Electricity Facts Label into strict JSON. "
    "Use only what the text states. Never estimate or invent: if a field is not "
    "clearly present, return an empty string.\n"
    "Rules:\n"
    "- exit_fee_dollars: the early-termination/cancellation fee in dollars, digits "
    "and optional decimal point only (e.g. '150'). If the document says there is "
    "no fee, return '0'.\n"
    "- term_months: the contract length in months, digits only (e.g. '24'). Empty "
    "if the plan is month-to-month.\n"
    "- contract_type: 'month-to-month' if the term is month-to-month/variable with "
    "no fixed end, 'fixed' if it runs for a set number of months, else ''.\n"
    "- contract_start / contract_end: dates as 'YYYY-MM' (e.g. '2027-02'). Empty "
    "if not stated.\n"
    "- energy_rate_cents: the energy charge in CENTS per kWh (e.g. '11.4').\n"
    "- provider / plan: as printed, else empty string."
)


def _months_until(yyyy_mm: str, today=None) -> float | None:
    """Whole months from today to a 'YYYY-MM'. None if missing/past/absurd."""
    from datetime import date
    m = re.match(r"\s*(\d{4})-(\d{1,2})\s*$", yyyy_mm or "")
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    today = today or date.today()
    months = (year - today.year) * 12 + (month - today.month)
    if months < 0 or months > 120:
        return None
    return float(months)


def _add_months(yyyy_mm: str, n: int) -> str:
    m = re.match(r"\s*(\d{4})-(\d{1,2})\s*$", yyyy_mm or "")
    if not m:
        return ""
    total = (int(m.group(1)) * 12 + int(m.group(2)) - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def parse_contract(pdf_bytes: bytes) -> dict:
    """Return exit fee / months remaining read from a contract or EFL PDF."""
    if not pdf_bytes:
        raise BillParseError("No file was received.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise BillParseError(
            f"That PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.")
    if not pdf_bytes.lstrip()[:5].startswith(b"%PDF"):
        raise BillParseError("That doesn't look like a PDF file.")

    backend = llm_backend.ChatBackend()
    text, how = _read_bill_text(pdf_bytes, backend, _CONTRACT_VISION_PROMPT)

    raw = backend.create_chat_completion(
        messages=[
            {"role": "system", "content": _CONTRACT_SYSTEM},
            {"role": "user", "content": "Contract:\n\n" + text[:24000]},
        ],
        temperature=0.0,
        max_tokens=300,
        response_schema=_CONTRACT_SCHEMA,
        schema_name="contract_fields",
    )["choices"][0]["message"]["content"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BillParseError("The contract was read but could not be understood.") from exc

    fee = _num(data.get("exit_fee_dollars"))
    term = _num(data.get("term_months"))
    rate = _num(data.get("energy_rate_cents"))
    end = (data.get("contract_end") or "").strip()
    start = (data.get("contract_start") or "").strip()
    ctype = (data.get("contract_type") or "").strip().lower()
    if "month-to-month" in ctype or "month to month" in ctype:
        ctype = "month-to-month"
    elif ctype != "fixed":
        ctype = ""

    if fee is not None and not (0 <= fee <= 5000):
        fee = None
    if term is not None and not (1 <= term <= 120):
        term = None
    if rate is not None and not (0 < rate < 100):
        rate = None

    # An end date is what we actually need; derive it from start + term if the
    # document only states those.
    if not _months_until(end) and start and term:
        end = _add_months(start, int(term))

    # A month-to-month plan has no term to run down: there is nothing to wait
    # out, so "months remaining" is not unknown -- it is zero.
    months = _months_until(end)
    if months is None and ctype == "month-to-month":
        months = 0.0

    return {
        "exit_fee_dollars": round(fee, 2) if fee is not None else None,
        "months_remaining": months,
        "contract_type":    ctype,
        "contract_end":     end,
        "term_months":      int(term) if term is not None else None,
        "energy_rate_cents": round(rate, 2) if rate is not None else None,
        "provider":         (data.get("provider") or "").strip(),
        "plan":             (data.get("plan") or "").strip(),
        "read_via":         how,
    }
