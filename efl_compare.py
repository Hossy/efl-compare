#!/usr/bin/env python3
# Copyright (C) 2026 John Greg Hossbach
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
efl_compare.py -- Compare Oncor fixed electricity plans from powertochoose.org

Usage:
    py -3.12 efl_compare.py --zip ZIP [--tiers A,B,...] [--text-table]
                            [--json] [--timestamped] [--no-llm] [--clear-cache]

    ZIP code and personal tiers can also be set via environment variables:
        EFL_ZIP=YOUR_ZIP EFL_TIERS=LOW,MID,HIGH py -3.12 efl_compare.py

Requirements: pip install -r requirements.txt

================================================================================
HOW IT WORKS
================================================================================

PLAN DATA
    powertochoose.org is operated by the PUCT (Public Utility Commission of
    Texas) and is the only site where all certified REPs are required to list
    their plans. Plan data is fetched via a POST to /en-us/Plan/ExportToCsv,
    which returns a structured CSV with pricing, EFL URLs, and metadata.
    The full CSV is saved to plans_latest.csv each run for debugging.

TDU RATES
    Oncor delivery charges (fixed $/mo + per-kWh) are the same for every
    provider and are passed through without markup. The script fetches current
    rates from the PUCT website (puc.texas.gov/industry/electric/rates/tdr/).
    Fallback chain: PUCT HTML scrape -> most recently dated local EFL that
    includes TDU charges -> hardcoded constants (June 2026 values).

USAGE TIERS
    EFLs only disclose pricing at 500/1000/2000 kWh -- insufficient for
    households with usage well above or below those tiers. The script calculates
    effective rates at the standard EFL tiers plus any personal tiers configured
    via EFL_TIERS or --tiers. The compare tier (default: median of personal tiers)
    is the primary sort key and the basis for the "vs best longer" delta.

EFL PARSING
    For each plan, the script downloads the EFL PDF (cached in efls_cache/) and
    extracts the energy charge and base charge using regex against PyMuPDF text
    (sort=True for spatial ordering). The Electricity Price section is anchored
    at "Average Monthly Use" rather than the section label, capturing rate rows
    that precede the label in multi-column PDF layouts. TDU charges are applied
    from the PUCT source above so all plans share the same current delivery rates
    regardless of when their EFL was published.

    Downloads use a 2-attempt strategy (45s + 30s timeout), followed by a
    Playwright headless-browser fallback (also 2 attempts) for JavaScript-rendered
    pages (Octopus Energy, TriEagle, Chariot Energy). If all attempts fail, rates
    fall back to CSV back-calculation.

BILL CREDITS
    Bill-credit plans advertise a low ¢/kWh rate that only applies at a
    specific usage threshold (typically 1000 kWh exactly). At typical high-usage
    levels well above the threshold, these plans are significantly more expensive.
    The script uses the full credit structure to calculate accurate rates at all
    six tiers.

    Credit data comes from two sources that are cross-checked against each
    other, since [Fees/Credits] in the CSV is a PUCT-validated structured field
    while the EFL is the legally authoritative document:

    1. EFL PDF (regex)  -- fast, direct from the legal document
    2. CSV [Fees/Credits] (LLM) -- PUCT-validated, always present, machine-
                                    readable; parsed by Qwen2.5-7B on GPU

    Reconciliation logic (for plans where has_crd=True):
      a. EFL parsed OK + both sources agree -> use agreed credits, high confidence
      b. EFL parsed OK + sources disagree   -> LLM re-parses the EFL text
                                               (applying careful LLM reading to
                                               the legal document), uses that
                                               result, flags a warning to user
      c. EFL parse failed                   -> CSV [Fees/Credits] via LLM only,
                                               back-calculate energy charge from
                                               the API's tier price + credit amount

    For plans where EFL parse failed, base_charge is assumed $0. Bill-credit
    plans almost universally use $0 base charge (the credit is the pricing
    mechanism), so this assumption is safe for this plan type.

    The [Fees/Credits] LLM parser caches results in memory -- at most 22 unique
    strings exist across the full CSV, so the model is called at most 22 times
    per run regardless of plan count.

SORTING AND OUTPUT
    Plans are grouped by contract term, longest first. Within each group, sorted
    by effective rate at the compare tier (default: median of --tiers).
    The rightmost column shows the delta vs the best plan with a
    strictly longer contract -- color-coded green/amber/red with a hover tooltip
    showing which plan and rate is being compared against.

    Output files are always written as plans_latest.html / .json. Pass
    --timestamped to also save a dated copy (e.g. plans_20260619_143022.html)
    alongside the latest file.

    The HTML output (plans_latest.html) includes:
      - Dark/light theme toggle (defaults to dark)
      - Top 3 Picks summary cards above the table
      - Collapsible term groups (▼ all / ★ best / ▶ hidden)
      - ❤ per-row favorite toggle
      - Color-coded source badges: [EFL] / [LLM] / [API]
      - ¢ badge for bill-credit plans (rate only valid near credit threshold)
      - ℹ badge with fee/credit details on hover
      - ⚠ badge for plans with one-time setup fees

    LLM STRUCTURAL GUARDS
    The structural LLM can misidentify bill-credit kWh thresholds as energy
    charge tier boundaries. Three guards catch and correct impossible states,
    emitting [LLM BUG] warnings to stdout:
      - tier_boundary_kwh AND energy_charge_threshold_kwh both set -> zero threshold
      - energy_charge_threshold_kwh set on a bill-credit plan -> zero threshold
      - tier_boundary_kwh > 0 with ec_above_tier == 0 (free energy above boundary,
        commercially impossible) -> zero tier boundary
================================================================================
"""

import argparse
import csv
import json
import time
import io
import logging
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

import truststore
truststore.inject_into_ssl()

# Suppress urllib3's InsecureRequestWarning — some providers use self-signed or
# legacy TLS certificates that trigger it on every request. The warning is not
# actionable and interleaves with progress output. SSL errors still raise.
import warnings as _warnings
try:
    import urllib3 as _urllib3
    _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
except Exception:
    _warnings.filterwarnings("ignore", message="Unverified HTTPS request")

import fitz              # PyMuPDF — strictly better than pdfplumber for these EFLs
import pdfplumber          # kept only for _fetch_tdu_rates_from_efls
import requests
from tabulate import tabulate

# Suppress pdfminer's verbose stderr (corrupt-stream warnings, /Root errors, etc.)
# Failures are tracked via the fallback counters in the summary line.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# ZIP code and personal tiers can be set via environment variables so users
# don't have to pass them on every run:
#   EFL_ZIP   — zip code to search (e.g. export EFL_ZIP=YOUR_ZIP)
#   EFL_TIERS — comma-separated personal tiers in kWh (e.g. export EFL_TIERS=LOW,MID,HIGH)
# CLI arguments always override environment variables.
import os as _os
TDU_FILTER   = "ONCOR ELECTRIC DELIVERY COMPANY"
_EFL_TIERS  = [500, 1_000, 2_000]   # mandated EFL disclosure tiers (fixed)
USAGE_TIERS = _EFL_TIERS            # rebuilt in main() from args
COMPARE_TIER   = 3_500                         # tier used for sort/delta column; rebuilt in main()

# Hardcoded fallback -- Oncor rates as of June 2026
ONCOR_FALLBACK = {"fixed_mo": 4.06, "per_kwh": 0.061196}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

EFL_CACHE   = _ROOT / "efls_cache"
META_FILE   = EFL_CACHE / "cache_meta.json"
MAX_WORKERS = 10                               # parallel download/HEAD workers
_cache_meta: dict = {}          # pid → entry; loaded in main(), saved at end
_meta_lock  = threading.Lock()  # guards concurrent writes from download threads


# ---------------------------------------------------------------------------
# TDU rate fetching (fallback chain: PUCT -> local EFL -> hardcoded)
# ---------------------------------------------------------------------------

def _fetch_tdu_rates_puct():
    try:
        r = requests.get(
            "https://www.puc.texas.gov/industry/electric/rates/tdr/",
            headers=HEADERS, timeout=15,
        )
        if r.status_code != 200:
            return None
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        # Table columns: AEP Central, AEP North, CenterPoint, Oncor, TNMP
        # Oncor is the 4th value in each row.
        cust_m  = re.search(
            r"Customer Charge" + r".*?&dollar;[\d.]+" * 3 + r".*?&dollar;([\d.]+)", text)
        meter_m = re.search(
            r"Metering Charge" + r".*?&dollar;[\d.]+" * 3 + r".*?&dollar;([\d.]+)", text)
        vol_m   = re.search(
            r"Volumetric"      + r".*?[\d.]+&cent"   * 3 + r".*?([\d.]+)&cent",     text)
        if cust_m and meter_m and vol_m:
            return {
                "fixed_mo": round(float(cust_m.group(1)) + float(meter_m.group(1)), 4),
                "per_kwh":  round(float(vol_m.group(1)) / 100, 6),
            }
        return None
    except Exception as exc:
        print(f"  [PUCT scrape failed: {exc}]", file=sys.stderr)
        return None


def _fetch_tdu_rates_from_efls():
    """Use most recently modified local EFL PDF that contains Oncor delivery charges."""
    fixed_pat = re.compile(
        r"(?:Oncor|ONC)[^\n]*?Delivery[^\n]*?\$([\d.]+)\s*per\s*(?:billing|month)", re.I)
    kwh_pat   = re.compile(
        r"(?:Oncor|ONC)[^\n]*?Delivery[^\n]*?([\d.]+)[¢c]\s*per\s*kWh", re.I)
    pdfs = sorted(
        _ROOT.glob("**/*.pdf"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for pdf in pdfs[:20]:
        try:
            with pdfplumber.open(str(pdf)) as doc:
                text = "\n".join(pg.extract_text() or "" for pg in doc.pages)
            fm = fixed_pat.search(text)
            km = kwh_pat.search(text)
            if fm and km:
                return {
                    "fixed_mo": float(fm.group(1)),
                    "per_kwh":  float(km.group(1)) / 100,
                }
        except Exception:
            pass
    return None


def get_tdu_rates():
    print("Fetching TDU rates...")
    rates = _fetch_tdu_rates_puct()
    if rates:
        print(f"  Source: PUCT  -- ${rates['fixed_mo']:.2f}/mo + {rates['per_kwh']*100:.4f}¢/kWh")
        return rates
    print("  PUCT failed -- trying local EFLs...")
    rates = _fetch_tdu_rates_from_efls()
    if rates:
        print(f"  Source: local EFL -- ${rates['fixed_mo']:.2f}/mo + {rates['per_kwh']*100:.4f}¢/kWh")
        return rates
    print("  WARNING: using hardcoded fallback (Oncor June 2026)")
    return ONCOR_FALLBACK


# ---------------------------------------------------------------------------
# Plan fetching via powertochoose.org CSV export
# ---------------------------------------------------------------------------

def fetch_plans(zip_code):
    print(f"\nFetching plans for zip {zip_code}...")
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(
        f"https://powertochoose.org/en-us/Plan/Results?zip={zip_code}",
        timeout=15,
    )
    r = session.post(
        "https://powertochoose.org/en-us/Plan/ExportToCsv",
        data={
            "method":             "plans",
            "zip_code":           zip_code,
            "estimated_use":      "1000",
            "plan_type":          "1",
            "sort_by_field":      "price_kwh1000_sort",
            "company_type":       "0",
            "min_usage_plan":     "off",
            "prepaid_plan":       "",
            "timeofuse":          "",
            "rating_total":       "",
            "renewable_energy_id":"",
            "company_id":         "",
            "compared_planes":    "",
        },
        timeout=60,
    )
    r.raise_for_status()
    (_ROOT / "plans_latest.csv").write_text(r.text, encoding="utf-8")
    all_rows = list(csv.DictReader(io.StringIO(r.text)))
    plans = [
        row for row in all_rows
        if row.get("[TduCompanyName]") == TDU_FILTER
        and row.get("[Fixed]")    == "1"
        and row.get("[PrePaid]")  != "True"
        and row.get("[TimeOfUse]") != "True"
        and row.get("[Language]") == "English"
    ]
    print(f"  {len(plans)} Oncor fixed non-prepaid English plans")
    return plans


# ---------------------------------------------------------------------------
# EFL parsing
# ---------------------------------------------------------------------------

def _find_energy_charge(text):
    """
    Return energy charge in $/kWh, or None if not found.

    Handles format variations seen across providers:
    - "6.72¢ per kWh"           standard ¢ per kWh
    - "9.3¢/kWh"                slash instead of 'per'
    - "7.088 cents/kWh"         'cents' unit word
    - "7.59¢ ¢ per kWh"         duplicate ¢ (Spark, Texans Choice)
    - "6.6�/kWh"           ¢ rendered as Unicode replacement char
    - "$ 0.060207"              $/kWh with no explicit 'per kWh' suffix
    - "Energy Charge(¢/kWh)     SFE Energy: unit in label, value after ')'
       6.67 ¢"
    - label on one line,        Octopus browser text: multi-line table
      value on the next

    Restricted to the Electricity Price section to avoid false positives.
    """
    # Try the Electricity Price section first for precision; if nothing found
    # fall back to full text (handles EFLs where the energy charge line appears
    # before the section header — e.g. Ranchero, SFE Energy).
    section = _extract_electricity_price_section(text)
    search_texts = [section, text] if section != text else [text]
    lines = None  # set per iteration below

    SKIP = re.compile(r"minimum|credit|residential\s+usage", re.I)

    # Pattern: cents value [unit] [per] kWh
    # Covers ¢, c, � (replacement char for ¢), cents, and duplicates
    CENTS = re.compile(
        r"([\d.]+)\s*"
        r"(?:cents?\s*|(?:[¢c�]\s*){1,2})"
        r"(?:/\s*)?(?:per\s+)?"
        r"kwh",
        re.I,
    )
    # Pattern: $ VALUE [per kWh/kilowatt-hour]  (with optional suffix)
    DOLLAR = re.compile(
        r"\$\s*([\d.]+)\s*"
        r"(?:per\s+(?:kilowatt[.\s-]?hour|kwh))?",
        re.I,
    )

    for search_text in search_texts:
        lines = search_text.splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"energy\s+(?:charge|rate)", line, re.I):
                continue
            if SKIP.search(line):
                continue

            # Combine with the next two lines to handle multi-line table layouts.
            # One-line lookahead covers Octopus ("Octopus Energy Charge\n7.8615¢ per kWh").
            # Two-line lookahead covers TriEagle ("Energy Charge: Per kWh (¢)\nPrice\nAll kWh 15.7000¢").
            combined = line
            for _j in range(1, 3):
                if i + _j < len(lines):
                    combined += " " + lines[i + _j]

            # 1. Cents format (most common)
            m = CENTS.search(combined)
            if m:
                return float(m.group(1)) / 100

            # 2. "(¢/kWh) VALUE" label format (SFE Energy)
            if re.search(r"\([¢c�/kwh]+\)", combined, re.I):
                m = re.search(r"\)\s*([\d.]+)", combined)
                if m:
                    val = float(m.group(1))
                    if 1.0 <= val <= 50.0:   # sanity: 1–50 ¢/kWh
                        return val / 100

            # 3. Dollar format — require explicit suffix OR a plausible $/kWh range
            m = DOLLAR.search(combined)
            if m:
                val = float(m.group(1))
                suffix = m.group(0)
                has_suffix = bool(re.search(r"per\s+(?:kilowatt|kwh)", suffix, re.I))
                if has_suffix or (0.01 <= val <= 0.50):  # $0.01–$0.50/kWh
                    return val

            # 4. Trailing-unit format: VALUE¢ with no following kWh qualifier
            #    (e.g. TriEagle: "Energy Charge: Per kWh (¢)\nAll kWh 14.7000¢")
            #    Only applies when the label already contains a per-kWh indicator.
            if re.search(r"per\s+kwh|\(", combined, re.I):
                m = re.search(r"(\d+\.\d+)\s*[¢c�](?:\s|$)", combined)
                if m:
                    val = float(m.group(1))
                    if 1.0 <= val <= 50.0:   # sanity: 1–50 ¢/kWh
                        return val / 100

    return None


def _find_base_charge(text):
    """Return base/monthly charge in $/month (0.0 if N/A or not found)."""
    base_pats = [
        r"base\s+monthly\s+charge",
        r"monthly\s+base\s+charge",
        r"base\s+charge",
    ]
    for line in text.splitlines():
        for pat in base_pats:
            if not re.search(pat, line, re.I):
                continue
            # Skip lines that refer to TDU/Oncor delivery charges
            if re.search(r"oncor|tdu|delivery|energy\s+charge", line, re.I):
                continue
            if re.search(r"n\s*/?\s*a", line, re.I):
                return 0.0
            m = re.search(r"\$\s*([\d.]+)", line)
            if m:
                return float(m.group(1))
    return 0.0


def _extract_electricity_price_section(text):
    """
    Return only the 'Electricity Price' section of EFL text.
    PUCT mandates this section appears before 'Other Key Terms' / 'Disclosure
    Chart', so bill credits must be disclosed here. Restricting the search to
    this section prevents false positives from promotional text elsewhere.

    Anchors at 'Average Monthly Use/Price' when available — this header appears
    before the pricing table in PyMuPDF's spatially-sorted output, ensuring that
    rate/credit rows which precede the 'Electricity Price' section label are
    included (affected: Think Energy, Texans Choice, and similar multi-column PDFs).
    Falls back to 'electricity price' label if the average-price header is absent.
    """
    # Use whichever anchor appears first in the text: "Average Monthly Use/Price" or
    # "Electricity Price" label.  In most EFLs the average-price header is first and
    # correctly captures rate/credit rows that PyMuPDF places before the section label.
    # Taking the minimum by position handles both layouts safely.
    m1 = re.search(r"average\s+(?:monthly|price)", text, re.I)
    m2 = re.search(r"electricity\s+price", text, re.I)
    candidates = [m for m in (m1, m2) if m is not None]
    if not candidates:
        return text   # can't find section boundary, search full text
    start_m = min(candidates, key=lambda m: m.start())
    # Find the end: start of the next major section
    end_m = re.search(
        r"other\s+key\s+terms|disclosure\s+chart|key\s+terms\s+&",
        text[start_m.start():], re.I,
    )
    end = start_m.start() + end_m.start() if end_m else len(text)
    return text[start_m.start():end]


def _find_bill_credits(text):
    """
    Return list of {amount, threshold_kwh, cumulative} dicts for bill credits
    declared in the Electricity Price section of the EFL.

    Approach: line-by-line scan restricted to the mandated section, looking for
    any line containing 'credit' + a dollar amount + a kWh threshold.
    Filters: amount must be > 0 (zero entries are 'no credit' declarations).
    Post-processing: deduplicate by (amount, threshold_kwh), normalize
    'exceeds 999 kWh' -> threshold 1000.
    """
    section = _extract_electricity_price_section(text)
    EXCL = re.compile(
        r"disconnect|insufficient|late\s+payment|non.recurring|deposit", re.I
    )
    DOLLAR = re.compile(r'\$\s*([\d.]+)')
    KWH    = re.compile(r'(?<!\d)(?<!\.)([\d,]+)\s*kWh', re.I)

    seen    = set()
    credits = []
    lines   = section.splitlines()
    for idx, line in enumerate(lines):
        if not re.search(r"credit", line, re.I):
            continue
        if EXCL.search(line):
            continue
        # Combine with next line to handle split-line formats such as:
        #   "Minimum Usage Credit: $125.00 per billing cycle"
        #   "where usage ≥ 1000 kWh"
        # Skip only if the kWh condition is absent on BOTH the current and next line —
        # if the next line has a kWh threshold (Octopus Lite style), we let it through
        # to be captured by the combine logic below.
        next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        if re.search(r"\$\s*[\d.]+\s*per\s+(?:billing\s+)?(?:month|cycle)", line, re.I) \
                and not KWH.search(line) and not KWH.search(next_line):
            continue
        combined = line + (" " + lines[idx + 1] if idx + 1 < len(lines) else "")
        dollar = DOLLAR.search(combined)
        kwh_m  = KWH.search(combined)
        if not (dollar and kwh_m):
            continue
        amount    = float(dollar.group(1))
        threshold = int(kwh_m.group(1).replace(",", ""))
        if amount <= 0:
            continue   # $0 entries are 'no credit' declarations
        # Normalize "exceeds 999 kWh" -> threshold 1000
        if threshold == 999:
            threshold = 1000
        key = (amount, threshold)
        if key in seen:
            continue   # duplicate line stating the same credit
        seen.add(key)
        credits.append({
            "amount":        amount,
            "threshold_kwh": threshold,
            "cumulative":    bool(re.search(r"additional", combined, re.I)),
            "requires_enrollment": False,
        })

    # Conditional flat credits: per-month discounts with no kWh threshold
    # (e.g. "Auto Pay & Paperless Credit: $5.00 per month")
    # threshold_kwh=0 so they apply at every usage level.
    COND_EXCL = re.compile(r"disconnect|NSF|insufficient|late\s+payment|per\s+kWh", re.I)
    cond_seen: set = set()
    for line in section.splitlines():
        if not re.search(r"credit|discount", line, re.I):
            continue
        if COND_EXCL.search(line):
            continue
        if KWH.search(line):
            continue   # usage-based credit already captured above
        dollar = DOLLAR.search(line)
        per_mo = re.search(r"per\s+(?:billing\s+)?(?:month|cycle)", line, re.I)
        if not (dollar and per_mo):
            continue
        amount = float(dollar.group(1))
        if amount <= 0 or amount > 50:   # sanity: reasonable monthly credit
            continue
        if amount in cond_seen:
            continue
        cond_seen.add(amount)
        credits.append({
            "amount":        amount,
            "threshold_kwh": 0,
            "cumulative":    False,
            "requires_enrollment": True,
        })

    return credits


def _detect_tdu_bundled(text):
    """
    Return True if this EFL's stated Energy Charge already includes TDU delivery
    charges (i.e. TDU is NOT listed as a separate pass-through line item).

    Searches the full Electricity Price section (from the section header through
    Other Key Terms) for any TDU/Oncor/delivery/distribution/pass-through mention.
    Every standard EFL that passes TDU through separately has at least one such
    mention.  TriEagle is the only provider (in this market) whose pricing section
    is completely silent about TDU — because their rates already include it.

    We search from the Electricity Price section start (not just from "Base Charge")
    to handle multi-column PDFs (e.g. Champion Energy) where the Oncor column header
    appears spatially before the "Base Charge" row label after PyMuPDF's sort.
    """
    TDU_SIGNAL = re.compile(
        r'oncor|tdu|delivery\s+charg|distribution|transmission|pass.?through',
        re.I
    )
    # Use the broadest possible section: from "Electricity Price" header (or
    # "Energy Charge" table header) to "Other Key Terms".  Anchoring at the full
    # pricing section ensures we catch TDU column headers that precede "Base Charge".
    section_start = re.search(r'electricity\s+price|energy\s+charge', text, re.I)
    if not section_start:
        return False   # can't locate section — assume unbundled

    tail = text[section_start.start():]
    end  = re.search(r'other\s+key\s+terms|type\s+of\s+product|contract\s+term', tail, re.I)
    block = tail[:end.start()] if end else tail[:3000]
    return not bool(TDU_SIGNAL.search(block))


def _parse_efl_text(text):
    """
    Parse energy charge, base charge, and bill credits from raw EFL text.
    Shared by both the PDF path and the browser-rendered path.
    Stores the extracted Electricity Price section so callers can pass it
    directly to LLM calls without re-extracting from the full raw text.
    """
    return {
        "energy_charge":             _find_energy_charge(text),
        "base_charge":               _find_base_charge(text),
        "bill_credits":              _find_bill_credits(text),
        "tdu_bundled":               _detect_tdu_bundled(text),
        "raw_text":                  text,
        "electricity_price_section": _extract_electricity_price_section(text),
    }


def parse_efl(pdf_path):
    result = {"energy_charge": None, "base_charge": 0.0, "bill_credits": [], "raw_text": "",
              "electricity_price_section": ""}
    try:
        doc   = fitz.open(str(pdf_path))
        pages = [page.get_text("text", sort=True) for page in doc]
        doc.close()
        text   = "\n".join(pages)
        result = _parse_efl_text(text)
    except Exception:
        pass   # failure is counted via the fallback stats in the summary line
    return result


def _fetch_efl_with_browser(url, cache, pid=None):
    """
    Use Playwright/Chromium to fetch an EFL that requires JavaScript rendering.
    Handles two cases:
      - SPA pages (Octopus, TriEagle): page renders as HTML → extract body text
      - PDF downloads (Tara/Amigo): navigation triggers a file download → save PDF
    Playwright uses Chromium's SSL stack so it also handles legacy TLS that
    Python's requests rejects (e.g. Tara/Amigo unsafe legacy renegotiation).
    Returns a parsed EFL dict or None.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        from playwright_stealth import stealth_sync
    except ImportError:
        stealth_sync = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            if stealth_sync:
                stealth_sync(page)

            downloaded_path = None

            def _on_download(dl):
                nonlocal downloaded_path
                dl.save_as(str(cache))
                downloaded_path = cache

            page.on("download", _on_download)
            page.goto(url, timeout=30_000, wait_until="networkidle")

            if downloaded_path:
                # Navigation triggered a PDF download (e.g. Tara/Amigo)
                browser.close()
                return parse_efl(downloaded_path)

            # Page rendered as HTML (SPA provider).
            # Save PDF via Chromium's print engine — self-contained, reuses the
            # existing PDF parse pipeline on subsequent runs.
            # Also save raw HTML alongside for human debugging (open in browser
            # with internet access for full CSS/font rendering).
            html_path = cache.with_suffix(".html")
            try:
                page.pdf(path=str(cache))
            except Exception:
                cache = None   # PDF generation failed, fall through to text

            try:
                html_path.write_text(page.content(), encoding="utf-8")
            except Exception:
                pass   # HTML save failing is non-fatal

            browser.close()

            if cache and cache.exists() and cache.read_bytes()[:4] == b"%PDF":
                if pid:
                    _record_meta(pid, cache.name, url, "browser", cache.stat().st_size)
                return parse_efl(cache)

            # PDF generation failed — fall back to inner_text extraction
            text = page.inner_text("body") if page else ""
            return _parse_efl_text(text) if text.strip() else None

    except Exception:
        return None


def _safe_name(s, maxlen=40):
    """Sanitize a string for use in a filename."""
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:maxlen]


def download_and_parse_efl(plan):
    """
    Download and parse an EFL PDF.
    Fast path: plain requests.get() — works for most providers.
    Fallback: Playwright browser rendering — used when the fast path returns HTML
              (SPA providers like Octopus/TriEagle) or fails entirely (legacy SSL
              providers like Tara/Amigo). The browser handles both cases transparently.
    Cached HTML from previous failed downloads is detected and discarded so the
    browser path runs on the next attempt.
    """
    url      = (plan.get("[FactsURL]") or "").strip()
    pid      = plan["[idKey]"]
    provider = _safe_name(plan.get("[RepCompany]", "").strip())
    name     = _safe_name(plan.get("[Product]", "").strip())
    cache    = EFL_CACHE / f"{provider}_{name}_{pid}.pdf"

    if not url:
        return None

    # ── Fast path ────────────────────────────────────────────────────────────
    _HTTP_RETRIES  = 2   # total attempts (original + 1 retry)
    _RETRY_DELAY_S = 2   # seconds between attempts

    if not cache.exists():
        _last_exc = None
        for _attempt in range(_HTTP_RETRIES):
            _timeout = 45 if _attempt == 0 else 30
            try:
                r = requests.get(url, headers=HEADERS, timeout=_timeout)
                r.raise_for_status()
                cache.write_bytes(r.content)
                _record_meta(pid, cache.name, url, "download",
                             len(r.content), r.headers)
                _last_exc = None
                break
            except Exception as _exc:
                _last_exc = _exc
                # Legacy TLS error — bypass retry loop and use special adapter
                if "UNSAFE_LEGACY_RENEGOTIATION" in str(_exc):
                    try:
                        import ssl
                        _ctx = ssl.create_default_context()
                        _ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
                        import urllib3
                        _s = requests.Session()
                        class _LegacyAdapter(requests.adapters.HTTPAdapter):
                            def init_poolmanager(self, *a, **kw):
                                kw["ssl_context"] = _ctx
                                super().init_poolmanager(*a, **kw)
                        _s.mount("https://", _LegacyAdapter())
                        r = _s.get(url, headers=HEADERS, timeout=_timeout)
                        r.raise_for_status()
                        cache.write_bytes(r.content)
                        _record_meta(pid, cache.name, url, "download",
                                     len(r.content), r.headers)
                        _last_exc = None
                    except Exception:
                        pass  # fall through to Playwright retries below
                    break  # don't retry the legacy TLS path
                if _attempt < _HTTP_RETRIES - 1:
                    import time as _time
                    _time.sleep(_RETRY_DELAY_S)

        if _last_exc is not None:
            # All HTTP attempts failed — try Playwright (also with retries)
            _result = None
            for _attempt in range(_HTTP_RETRIES):
                _result = _fetch_efl_with_browser(url, cache, pid)
                if _result is not None:
                    break
                if _attempt < _HTTP_RETRIES - 1:
                    import time as _time
                    _time.sleep(_RETRY_DELAY_S)
            if _result is not None:
                return _result

    # If cached file is not a real PDF (HTML from a previous run), discard it
    # and use the browser to get the actual content.
    if cache.read_bytes()[:4] != b"%PDF":
        cache.unlink()
        return _fetch_efl_with_browser(url, cache, pid)

    # ── PDF parse path ───────────────────────────────────────────────────────
    # Backfill metadata for files that existed before the metadata system was
    # added (or were downloaded while metadata was unavailable).
    if str(pid) not in _cache_meta:
        _record_meta(pid, cache.name, url, "cached", cache.stat().st_size, None)
    return parse_efl(cache)


# ---------------------------------------------------------------------------
# Cache maintenance
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    """Load EFL cache metadata from JSON. Returns empty dict if missing or corrupt."""
    try:
        if META_FILE.exists():
            return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_meta() -> None:
    """Persist _cache_meta to disk."""
    try:
        META_FILE.write_text(json.dumps(_cache_meta, indent=2), encoding="utf-8")
    except Exception:
        pass


def _record_meta(pid: str, filename: str, url: str, source: str,
                 file_size: int, response_headers=None) -> None:
    """Thread-safe: record/update metadata for a freshly cached EFL file."""
    from datetime import datetime
    h = dict(response_headers or {})
    # Normalise header case
    h_lower = {k.lower(): v for k, v in h.items()}
    entry = {
        "filename":       filename,
        "url":            url,
        "etag":           h_lower.get("etag"),
        "last_modified":  h_lower.get("last-modified"),
        "content_length": int(h_lower["content-length"]) if "content-length" in h_lower else None,
        "file_size":      file_size,
        "source":         source,
        "cached_at":      datetime.now().isoformat(timespec="seconds"),
    }
    with _meta_lock:
        _cache_meta[str(pid)] = entry


_CACHE_MIN_TTL_HOURS = 12   # default: skip HEAD check for files verified within this window

def _check_cache_freshness(pid_to_url: dict) -> int:
    """
    Validate cached EFLs against their origin servers using HEAD requests.

    Files cached or verified within --cache-ttl-hours (default 12h) are trusted
    as-is without a HEAD request. This prevents re-downloads on successive runs
    when EFL servers return fresh ETags for dynamically generated but unchanged
    content. Set --cache-ttl-hours 0 to always HEAD-check every cached file.

    For download-sourced entries: HEAD request, compare ETag → Last-Modified →
    Content-Length in that priority order. Also checks if FactsURL changed.
    For browser-sourced entries: compare stored file_size to current disk size.

    Invalidates stale entries by deleting the cached file and meta entry.
    Returns count of invalidated entries.
    """
    if not _cache_meta:
        return 0

    invalidated = 0

    def _check_one(pid: str, current_url: str) -> bool:
        """Returns True if entry was invalidated."""
        entry = _cache_meta.get(str(pid))
        if not entry:
            return False

        cache_file = EFL_CACHE / entry["filename"]
        if not cache_file.exists():
            with _meta_lock:
                _cache_meta.pop(str(pid), None)
            return True

        # If FactsURL changed → always stale
        if entry.get("url") and entry["url"] != current_url:
            cache_file.unlink(missing_ok=True)
            html_companion = cache_file.with_suffix(".html")
            html_companion.unlink(missing_ok=True)
            with _meta_lock:
                _cache_meta.pop(str(pid), None)
            return True

        # MinTTL: skip HEAD check if file was recently cached/verified
        cached_at = entry.get("cached_at")
        if cached_at and _CACHE_MIN_TTL_HOURS > 0:
            try:
                from datetime import datetime as _dt, timedelta as _td
                age = _dt.now() - _dt.fromisoformat(cached_at)
                if age < _td(hours=_CACHE_MIN_TTL_HOURS):
                    return False   # fresh enough — trust the cache
            except Exception:
                pass

        source = entry.get("source", "download")

        if source == "browser":
            # No live URL to HEAD; compare stored vs current file size
            current_size = cache_file.stat().st_size
            if entry.get("file_size") and current_size != entry["file_size"]:
                cache_file.unlink(missing_ok=True)
                with _meta_lock:
                    _cache_meta.pop(str(pid), None)
                return True
            return False

        # source == "download" or "cached": do a HEAD request
        try:
            r = requests.head(current_url, headers=HEADERS, timeout=10,
                              allow_redirects=True)
            if r.status_code not in (200, 204):
                return False   # server error — leave cache as-is
            h = {k.lower(): v for k, v in r.headers.items()}

            # Compare in priority order: ETag → Last-Modified → Content-Length
            server_etag = h.get("etag")
            if server_etag and entry.get("etag"):
                if server_etag != entry["etag"]:
                    cache_file.unlink(missing_ok=True)
                    with _meta_lock:
                        _cache_meta.pop(str(pid), None)
                    return True
                return False   # ETags match — definitely fresh

            server_lm = h.get("last-modified")
            if server_lm and entry.get("last_modified"):
                if server_lm != entry["last_modified"]:
                    cache_file.unlink(missing_ok=True)
                    with _meta_lock:
                        _cache_meta.pop(str(pid), None)
                    return True
                return False   # Last-Modified matches — treat as fresh

            # Compare Content-Length from HEAD against what the original GET
            # returned (both are wire-level / compressed sizes). Do NOT compare
            # against file_size -- requests decompresses gzipped responses so
            # len(r.content) != Content-Length for gzip-encoded transfers.
            server_cl = int(h["content-length"]) if "content-length" in h else None
            stored_cl = entry.get("content_length")
            if server_cl is not None and stored_cl is not None:
                if server_cl != stored_cl:
                    cache_file.unlink(missing_ok=True)
                    with _meta_lock:
                        _cache_meta.pop(str(pid), None)
                    return True

        except Exception:
            pass   # network error — leave cache as-is

        return False

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_check_one, pid, url): pid
                   for pid, url in pid_to_url.items()
                   if EFL_CACHE.joinpath(_cache_meta.get(str(pid), {}).get("filename", "")).exists()
                   or str(pid) in _cache_meta}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    invalidated += 1
            except Exception:
                pass

    return invalidated


def _prune_efl_cache():
    """
    Delete cached EFL PDFs whose plan ID is no longer present in the current
    plans_latest.csv. Uses all IDs in the full CSV (not just the filtered Oncor
    subset) so a plan that shifts TDU or language isn't incorrectly evicted.
    Called once per run after the CSV has been written.
    """
    csv_path = _ROOT / "plans_latest.csv"
    if not csv_path.exists() or not EFL_CACHE.exists():
        return

    with open(csv_path, encoding="utf-8") as f:
        active_ids = {row["[idKey]"] for row in csv.DictReader(f) if row.get("[idKey]")}

    removed = 0
    for f in list(EFL_CACHE.glob("*.pdf")) + list(EFL_CACHE.glob("*.html")):
        pid = f.stem.rsplit("_", 1)[-1]
        if pid not in active_ids:
            f.unlink()
            removed += 1
            _cache_meta.pop(str(pid), None)   # also remove from metadata

    if removed:
        print(f"  Pruned {removed} stale EFL file(s) from cache.")


# ---------------------------------------------------------------------------
# Credit reconciliation
# ---------------------------------------------------------------------------

def _credits_agree(a, b):
    """
    True if two credit lists represent the same credits.
    Compares on (amount, threshold_kwh) pairs only -- ignores cumulative flag
    since that field may be interpreted differently by regex vs LLM.
    """
    normalize = lambda credits: frozenset(
        (c["amount"], c["threshold_kwh"]) for c in credits
    )
    return normalize(a) == normalize(b)


# ---------------------------------------------------------------------------
# Bill calculation
# ---------------------------------------------------------------------------

def _fix_enrollment_credit_thresholds(credits: list) -> list:
    """
    Guard against the LLM inheriting a kWh threshold from an adjacent usage credit
    onto a simple enrollment credit (e.g. $5 auto-pay credit getting threshold=1000
    from the nearby $125 usage credit context).

    Heuristic: enrollment credits with amount <= $15 and threshold_kwh > 0 are almost
    certainly a context bleed.  $15 is safely above real auto-pay/paperless credits
    ($5–$10) and safely below any legitimate usage-based credit ($30+).
    """
    result = []
    for c in credits:
        if c.get("requires_enrollment") and c["threshold_kwh"] > 0 and c["amount"] <= 15.0:
            result.append({**c, "threshold_kwh": 0})
        else:
            result.append(c)
    return result


def _efl_needs_structural_llm(text: str) -> bool:
    """
    Return True if this EFL contains structural pricing features that a simple
    two-field (ec, bc) regex parse cannot capture correctly.

    These are trigger patterns — specific enough that they don't fire on normal
    EFLs, but broad enough to catch the known structural variants.  The LLM then
    reads the full Electricity Price section and returns the correct structure;
    we never rely on regex to *extract* the values, only to *detect* that a
    second look is warranted.

    Known triggers:
      - Threshold energy charge  (Texans Choice Texas Instant)
      - Amortised one-time fee   (Tara / Just Energy / Amigo GoodBundle)
      - Explicitly bundled TDU   (Texans Choice states this in prose)
    """
    triggers = [
        r"energy\s+charge\s+is\s+only\s+applicable\s+to\s+usage\s+above",
        r"(?:1/\d+|one.twelfth)\s+of\s+this\s+(?:set.?up\s+)?cost\s+is\s+included",
        r"delivery\s+charges\s+(?:from\s+your\s+)?(?:tdu|tdsp).*?bundled\s+into",
    ]
    return any(re.search(p, text, re.I | re.S) for p in triggers)


def effective_cents_per_kwh(energy_charge, base_charge, tdu, kwh, bill_credits,
                            include_enrollment_credits=True,
                            energy_threshold_kwh=0, tdu_already_bundled=False,
                            tier_boundary_kwh=0, ec_above_tier=0.0):
    """
    Calculate all-in effective rate (¢/kWh) at a given usage level.

    energy_threshold_kwh — if > 0, the energy charge applies only to kWh above
        this threshold (e.g. Texans Choice: 14¢/kWh only above 1000 kWh).
        When combined with tdu_already_bundled=True, TDU is NOT added separately
        because it is already embedded in the stated energy_charge and base_charge.
    tdu_already_bundled — when True, skip the TDU addition (rates are all-in).
    tier_boundary_kwh — if > 0, the plan has two energy charge tiers: energy_charge
        applies to the first tier_boundary_kwh kWh, ec_above_tier applies above.
    ec_above_tier — upper-tier energy charge in $/kWh (0.0 if no tiering).
    """
    credit = sum(
        c["amount"] for c in bill_credits
        if c["threshold_kwh"] <= kwh
        and (include_enrollment_credits or not c.get("requires_enrollment", False))
    )

    if tier_boundary_kwh > 0 and kwh > tier_boundary_kwh:
        # Two-tier billing: lower rate up to boundary, upper rate above
        energy_cost = (energy_charge * tier_boundary_kwh
                       + ec_above_tier * (kwh - tier_boundary_kwh))
    else:
        billable_kwh = max(0, kwh - energy_threshold_kwh) if energy_threshold_kwh > 0 else kwh
        energy_cost  = energy_charge * billable_kwh

    if tdu_already_bundled:
        total = energy_cost + base_charge - credit
    else:
        total = (
            energy_cost
            + base_charge
            + tdu["fixed_mo"]
            + tdu["per_kwh"] * kwh
            - credit
        )
    return (total / kwh) * 100   # -> ¢/kWh


def _back_calc_from_credits(plan, credits, tdu):
    """
    Back-calculate energy charge for a bill-credit plan when EFL parsing failed.
    Uses the lowest credit threshold tier from the API's 3-tier prices.
    Returns (energy_charge $/kWh, base_charge $/mo).
    """
    if not credits:
        return None, None

    credits_sorted = sorted(credits, key=lambda c: c["threshold_kwh"])
    threshold = credits_sorted[0]["threshold_kwh"]

    # Map threshold to nearest available API tier (500 / 1000 / 2000 kWh)
    if threshold <= 750:
        tier_kwh, tier_key = 500,  "[kwh500]"
    elif threshold <= 1500:
        tier_kwh, tier_key = 1000, "[kwh1000]"
    else:
        tier_kwh, tier_key = 2000, "[kwh2000]"

    try:
        all_in_per_kwh = float(plan[tier_key])
    except (ValueError, TypeError, KeyError):
        return None, None

    # Sum credits that apply at this tier
    credit_at_tier = sum(
        c["amount"] for c in credits_sorted
        if c["threshold_kwh"] <= tier_kwh
    )

    # Solve: all_in_per_kwh * tier_kwh = ec * tier_kwh + 0 + tdu_fixed + tdu_kwh * tier_kwh - credit
    ec = (
        all_in_per_kwh * tier_kwh
        + credit_at_tier
        - tdu["fixed_mo"]
        - tdu["per_kwh"] * tier_kwh
    ) / tier_kwh

    return ec, 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



def parse_args():
    p = argparse.ArgumentParser(
        description="Compare Oncor electricity plans from powertochoose.org",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    grp = p.add_argument_group("search")
    grp.add_argument("--zip", default=_os.environ.get("EFL_ZIP") or None, metavar="ZIP",
                     help="Zip code to search. Defaults to EFL_ZIP env var; required if "
                          "neither is set.")

    grp = p.add_argument_group("usage tiers")
    _env_tiers = _os.environ.get("EFL_TIERS")
    grp.add_argument("--tiers", default=_env_tiers, metavar="A,B,...",
                     help="Comma-separated personal usage tiers in kWh, appended to the "
                          "standard 500/1,000/2,000 EFL tiers. Defaults to EFL_TIERS env "
                          "var; omit both to use standard EFL tiers only.")
    grp.add_argument("--no-tiers", action="store_true",
                     help="Use only the 3 standard EFL tiers (500/1,000/2,000 kWh); "
                          "ignore --tiers")
    grp.add_argument("--compare-tier", type=int, default=None, metavar="N",
                     help="kWh tier used for sort order and the 'vs best longer' delta "
                          "column (default: median of --tiers)")

    grp = p.add_argument_group("output")
    grp.add_argument("--html", default=str(_ROOT / "plans_latest.html"),
                     metavar="PATH",
                     help="HTML output path (default: plans_latest.html next to script)")
    grp.add_argument("--no-html", action="store_true",
                     help="Skip HTML generation")
    grp.add_argument("--text-table", action="store_true",
                     help="Print the comparison table to stdout "
                          "(default: off; HTML is the primary output)")
    grp.add_argument("--json", nargs="?",
                     const=str(_ROOT / "plans_latest.json"),
                     metavar="PATH",
                     help="Write full results to JSON (default path: plans_latest.json). "
                          "Includes parsed rates, credits, and structural flags for all plans.")
    grp.add_argument("--timestamped", action="store_true",
                     help="Also save timestamped copies of output files alongside the 'latest' "
                          "versions (e.g. plans_20260619_143022.html)")

    grp = p.add_argument_group("rate calculation")
    grp.add_argument("--no-enrollment-credits", action="store_true",
                     help="Exclude enrollment-based credits (auto-pay, paperless, etc.) "
                          "from rate calculations; shows the rate you'd pay without signing "
                          "up for those programs")

    grp = p.add_argument_group("cache / performance")
    grp.add_argument("--no-llm", action="store_true",
                     help="Skip all LLM calls — development/diagnostic mode, "
                          "significantly degraded accuracy, not for production use")
    grp.add_argument("--no-cache-check", action="store_true",
                     help="Skip HTTP HEAD freshness checks against origin servers; use "
                          "cached EFLs as-is (faster offline / CI runs, may use stale data)")
    grp.add_argument("--cache-ttl-hours", type=float, default=12, metavar="N",
                     help="Skip HEAD freshness check for EFLs cached within the last N hours "
                          "(default: 12). Set to 0 to always check. Ignored when --no-cache-check "
                          "or --clear-cache is set.")
    grp.add_argument("--clear-cache", action="store_true",
                     help="Delete all cached EFL files and re-download everything fresh")

    return p.parse_args()


def main():
    args = parse_args()

    import shutil as _shutil
    from datetime import datetime as _dt
    _run_stamp = _dt.now().strftime("%Y%m%d_%H%M%S")

    if args.clear_cache and EFL_CACHE.exists():
        for f in EFL_CACHE.glob("*.pdf"):
            f.unlink()
        print("EFL cache cleared.")

    EFL_CACHE.mkdir(exist_ok=True)
    _t_start = time.perf_counter()

    if not args.zip:
        print("ERROR: zip code is required. Pass --zip or set the EFL_ZIP environment variable.")
        raise SystemExit(1)

    # Rebuild USAGE_TIERS and COMPARE_TIER from args
    global USAGE_TIERS, COMPARE_TIER
    try:
        extra = [] if (args.no_tiers or not args.tiers) else [int(t.strip()) for t in args.tiers.split(",") if t.strip()]
        USAGE_TIERS = sorted(set(_EFL_TIERS + extra))
    except ValueError:
        print(f"WARNING: invalid --tiers value '{args.tiers}', ignoring personal tiers")
        extra = []
        USAGE_TIERS = _EFL_TIERS

    if args.compare_tier is not None:
        COMPARE_TIER = args.compare_tier
    else:
        # Default: median of the personal (extra) tiers.
        # If --tiers is empty, fall back to median of EFL tiers (1000).
        pool = sorted(extra) if extra else sorted(_EFL_TIERS)
        COMPARE_TIER = pool[len(pool) // 2]

    # Ensure compare tier is in USAGE_TIERS (add it if not)
    if COMPARE_TIER not in USAGE_TIERS:
        USAGE_TIERS = sorted(USAGE_TIERS + [COMPARE_TIER])

    print("=" * 80)
    print("  powertochoose.org EFL Comparator")
    print(f"  Zip: {args.zip}  |  TDU: Oncor  |  Fixed plans only  |  excl. taxes")
    if args.no_llm:
        print("  WARNING: --no-llm active -- bill-credit and some EFL-failed plan rates are less accurate")
    print("=" * 80)

    tdu   = get_tdu_rates()
    plans = fetch_plans(args.zip)
    _prune_efl_cache()

    # Load existing EFL cache metadata and optionally check freshness via HEAD
    global _cache_meta, _CACHE_MIN_TTL_HOURS
    _CACHE_MIN_TTL_HOURS = args.cache_ttl_hours
    _cache_meta = _load_meta()
    if not args.no_cache_check and _cache_meta:
        pid_to_url = {
            plan["[idKey]"]: (plan.get("[FactsURL]") or "").strip()
            for plan in plans
        }
        print("  Checking EFL cache freshness...", end="", flush=True)
        invalidated = _check_cache_freshness(pid_to_url)
        if invalidated:
            print(f" {invalidated} stale EFL(s) invalidated -- will re-download.")
        else:
            print(" all current.")

    # ── Phase 1: parallel EFL downloads and PDF parsing ─────────────────────
    # Network I/O and PDF text extraction are safe to parallelize -- each plan
    # writes to its own unique cache file. LLM calls stay in the serial phase.
    _any_missing = any(
        str(p["[idKey]"]) not in _cache_meta
        for p in plans
    )
    _phase1_verb = "Downloading & parsing" if _any_missing else "Parsing"
    print(f"\n{_phase1_verb} {len(plans)} EFLs"
          f" (up to {MAX_WORKERS} parallel, cached in efls_cache/)...")

    efl_results: dict[str, object] = {}   # idKey -> parse_efl result or None
    _progress_lock = threading.Lock()
    _done = [0]

    def _download_one(plan):
        result = download_and_parse_efl(plan)
        with _progress_lock:
            _done[0] += 1
            sys.stdout.write(f"\r  {_done[0]:>3}/{len(plans)} downloaded")
            sys.stdout.flush()
        return plan["[idKey]"], result

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_download_one, p): p for p in plans}
        for fut in as_completed(futures):
            try:
                pid, efl = fut.result()
                efl_results[pid] = efl
            except Exception:
                efl_results[futures[fut]["[idKey]"]] = None

    print()   # newline after progress

    # ── Phase 2: serial LLM + rate calculation ───────────────────────────────
    import credit_parser as _cp
    if not args.no_llm:
        # Load the model eagerly in plain-scroll mode so any C-level output
        # (llama_context warnings, etc.) appears in the scrolling region before
        # the two-line display starts. Two-line mode is enabled after load so
        # the "Model: GPU ready" line naturally becomes the status line.
        _cp._two_line_mode = False
        _cp._load_model()
    _cp._two_line_mode = not args.no_llm

    results   = []
    failed    = 0
    llm_used  = 0
    warnings  = []

    for i, plan in enumerate(plans, 1):
        provider = plan["[RepCompany]"].strip()
        name     = plan["[Product]"].strip()
        term     = int(plan.get("[TermValue]") or 0)
        etf      = plan["[CancelFee]"].strip()
        rnw      = plan["[Renewable]"].strip()
        has_crd  = plan["[MinUsageFeesCredits]"] == "TRUE"

        sys.stdout.write(f"\r  [{i:>3}/{len(plans)}] {provider[:28]:<28}  {name[:28]:<28}")
        sys.stdout.flush()

        efl = efl_results.get(plan["[idKey]"])

        # V-shape CSV signal: if kwh2000 price is significantly higher than kwh1000,
        # the plan almost certainly has tiered rates. Used to trigger structural LLM.
        # CSV stores prices in $/kWh, so 1.5¢/kWh threshold = 0.015 $/kWh.
        try:
            kwh1000_csv = float(plan.get("[kwh1000]") or 0)
            kwh2000_csv = float(plan.get("[kwh2000]") or 0)
            is_tiered   = (kwh2000_csv - kwh1000_csv) > 0.015  # $/kWh V-shape threshold (=1.5¢)
        except (ValueError, TypeError):
            is_tiered = False

        # Per-plan structural fields — all default to "not special"
        energy_threshold_kwh = 0
        tdu_already_bundled  = False
        tier_boundary_kwh    = 0
        ec_above_tier        = 0.0
        one_time_fee         = 0.0
        credits              = []

        if efl and efl["energy_charge"] is not None:
            # EFL parsed successfully — immutable originals + mutable calc vars.
            ec_efl  = efl["energy_charge"]
            bc_efl  = efl["base_charge"]
            ec_calc = ec_efl
            bc_calc = bc_efl
            src     = "efl"

            # Some providers (currently TriEagle) bundle TDU into their stated rates.
            # Subtract TDU now so the universal rate calculator can re-add it uniformly.
            # ec_calc/bc_calc are mutated; ec_efl/bc_efl remain as originals for restoration.
            if efl.get("tdu_bundled", False):
                ec_calc = ec_efl - tdu["per_kwh"]
                bc_calc = max(0.0, bc_efl - tdu["fixed_mo"])

            if has_crd and not args.no_llm:
                from credit_parser import parse_credits, parse_credits_from_efl_text
                efl_credits = efl["bill_credits"]         # regex parse of legal doc
                csv_credits = parse_credits(              # LLM parse of PUCT CSV field
                    plan.get("[Fees/Credits]", "")
                )

                if _credits_agree(efl_credits, csv_credits):
                    credits = csv_credits
                else:
                    # Disagreement: apply LLM to the EFL section (the legal document).
                    section = efl.get("electricity_price_section") or efl.get("raw_text", "")
                    lm_efl_credits = parse_credits_from_efl_text(section)
                    # If LLM re-parse found credit structures but all amounts are zero,
                    # the EFL likely lacks explicit dollar amounts — fall back to CSV LLM.
                    if lm_efl_credits and all(c.get("amount", 0) == 0 for c in lm_efl_credits):
                        credits = csv_credits
                    else:
                        credits = lm_efl_credits
                    warnings.append(
                        f"{provider} / {name}: EFL regex {efl_credits} vs "
                        f"CSV LLM {csv_credits} -- LLM re-parsed EFL: {lm_efl_credits}"
                    )
            else:
                credits = efl["bill_credits"]

            # Structural LLM check — triggered by text patterns OR CSV V-shape.
            # Detects threshold energy charges, amortised fees, TDU bundling prose,
            # and tiered energy rates that a two-field regex parse cannot capture.
            needs_struct = _efl_needs_structural_llm(efl.get("raw_text", "")) or is_tiered
            if not args.no_llm and needs_struct:
                from credit_parser import parse_rates_from_efl_text
                # Pass raw_text so parse_rates_from_efl_text re-extracts from "Average Monthly Use"
                # anchor — this captures the full pricing table even when PyMuPDF sort places the
                # "Electricity Price" section label after the table rows (e.g. Texans Choice).
                struct  = parse_rates_from_efl_text(efl.get("raw_text", ""))
                if struct:
                    energy_threshold_kwh = struct.get("energy_charge_threshold_kwh", 0)
                    llm_bundled          = struct.get("tdu_bundled", False)
                    one_time_fee         = struct.get("one_time_fee_dollars", 0.0)
                    tier_boundary_kwh    = struct.get("tier_boundary_kwh", 0)
                    ec_above_tier        = struct.get("energy_charge_cents_above_tier", 0.0) / 100  # ¢ → $/kWh

                    # Mutual-exclusion guards — the LLM prompt instructs the model to
                    # keep these fields mutually exclusive; if a guard fires it means
                    # the model disobeyed its instructions and should be investigated.
                    if tier_boundary_kwh > 0 and energy_threshold_kwh > 0:
                        warnings.append(
                            f"{provider} / {name}: [LLM BUG] structural LLM set both "
                            f"tier_boundary_kwh={tier_boundary_kwh} and "
                            f"energy_charge_threshold_kwh={energy_threshold_kwh} — "
                            f"zeroing threshold (mutually exclusive fields)"
                        )
                        energy_threshold_kwh = 0
                    if credits and energy_threshold_kwh > 0:
                        warnings.append(
                            f"{provider} / {name}: [LLM BUG] structural LLM set "
                            f"energy_charge_threshold_kwh={energy_threshold_kwh} on a "
                            f"bill-credit plan — credit threshold belongs in credits[], "
                            f"not energy charge structure; zeroing"
                        )
                        energy_threshold_kwh = 0
                    if tier_boundary_kwh > 0 and ec_above_tier == 0.0:
                        warnings.append(
                            f"{provider} / {name}: [LLM BUG] structural LLM set "
                            f"tier_boundary_kwh={tier_boundary_kwh} with ec_above_tier=0 — "
                            f"impossible for a commercial plan (would make energy free above "
                            f"boundary); zeroing tier boundary"
                        )
                        tier_boundary_kwh = 0

                    if energy_threshold_kwh > 0 and llm_bundled:
                        # Threshold + bundled TDU: TDU is embedded in the threshold rate.
                        # Restore originals and signal effective_cents_per_kwh to skip TDU addition.
                        ec_calc = ec_efl
                        bc_calc = bc_efl
                        tdu_already_bundled = True
                    # one_time_fee is NOT folded into bc_calc — it is a one-time charge
                    # displayed separately in the HTML, not included in the effective rate.
                    llm_used += 1

        elif has_crd:
            # EFL parse failed on a bill-credit plan.
            if not args.no_llm:
                from credit_parser import parse_credits
                credits = parse_credits(plan.get("[Fees/Credits]", ""))
                ec_calc, bc_calc = _back_calc_from_credits(plan, credits, tdu)
                if ec_calc is None:
                    failed += 1
                    continue
                src      = "llm"
                llm_used += 1
            else:
                # --no-llm: pure API back-calc, credits ignored.
                try:
                    all_in  = float(plan["[kwh1000]"])
                    ec_calc = all_in - tdu["per_kwh"] - tdu["fixed_mo"] / 1000
                except (ValueError, TypeError):
                    failed += 1
                    continue
                bc_calc, credits, src = 0.0, [], "api"
                failed += 1

        else:
            # Non-credit plan, EFL regex failed.
            try:
                all_in  = float(plan["[kwh1000]"])
                ec_api  = all_in - tdu["per_kwh"] - tdu["fixed_mo"] / 1000
            except (ValueError, TypeError):
                failed += 1
                continue

            credits = []

            if efl and efl.get("raw_text") and not args.no_llm:
                from credit_parser import parse_rates_from_efl_text
                llm_rates = parse_rates_from_efl_text(efl.get("raw_text", ""))
                if llm_rates:
                    ec_llm = llm_rates["energy_charge_cents"] / 100
                    bc_llm = llm_rates["base_charge_dollars"]
                    delta  = abs(ec_api - ec_llm) * 100  # ¢/kWh

                    if delta <= 0.5:
                        ec_calc, bc_calc = ec_llm, bc_llm
                    else:
                        warnings.append(
                            f"{provider} / {name}: API back-calc "
                            f"{ec_api*100:.4f}c vs LLM {ec_llm*100:.4f}c "
                            f"(delta={delta:.4f}c) -- using LLM"
                        )
                        ec_calc, bc_calc = ec_llm, bc_llm
                    src      = "llm"
                    llm_used += 1
                else:
                    ec_calc, bc_calc = ec_api, 0.0
                    src    = "api"
                    failed += 1
            else:
                ec_calc, bc_calc = ec_api, 0.0
                src    = "api"
                failed += 1

        # Post-process credits: enrollment credits should not have inherited kWh thresholds
        credits = _fix_enrollment_credit_thresholds(credits)

        # Display fields: for TDU-bundled EFLs show original bundled base charge and flag.
        # bc_calc was unbundled for rate calculation; bc_display restores the EFL-stated value.
        bc_display = (efl["base_charge"] if efl and efl.get("tdu_bundled", False) else bc_calc)
        tdu_bundled_display = (efl.get("tdu_bundled", False) if efl else False) or tdu_already_bundled

        inc_enrollment = not args.no_enrollment_credits
        tiers = {
            k: effective_cents_per_kwh(ec_calc, bc_calc, tdu, k, credits, inc_enrollment,
                                       energy_threshold_kwh=energy_threshold_kwh,
                                       tdu_already_bundled=tdu_already_bundled,
                                       tier_boundary_kwh=tier_boundary_kwh,
                                       ec_above_tier=ec_above_tier)
            for k in USAGE_TIERS
        }
        results.append({
            "pid":       plan["[idKey]"],
            "provider":  provider,
            "plan":      name,
            "term":      term,
            "etf":       etf,
            "rnw":       rnw,
            "has_crd":   has_crd,
            "ec_cents":  ec_calc * 100,
            "bc":        bc_display,
            "tdu_bundled":            tdu_bundled_display,
            "energy_threshold_kwh":   energy_threshold_kwh,
            "tier_boundary_kwh":      tier_boundary_kwh,
            "ec_cents_above_tier":    ec_above_tier * 100,
            "is_tiered":              is_tiered,
            "one_time_fee_dollars":   one_time_fee,
            "bill_credits":           [dict(c) for c in credits],
            "tiers":     tiers,
            "src":       src,
            "facts_url":     (plan.get("[FactsURL]")      or "").strip(),
            "fees_credits":  (plan.get("[Fees/Credits]")  or "").strip(),
            "special_terms": (plan.get("[SpecialTerms]")  or "").strip(),
        })

    print()  # newline after last \r progress line
    summary = f"{len(results)} plans processed"
    if failed:
        summary += f", {failed} API fallback"
    if llm_used:
        summary += f", {llm_used} LLM fallback"
    if warnings:
        summary += f", {len(warnings)} credit disagreement(s)"
    print(f"\r  Done. {summary}.")

    if warnings:
        print("\nCredit source disagreements (EFL regex vs CSV LLM -- LLM re-parsed EFL used):")
        for w in warnings:
            print(f"  [!] {w}")

    # Sort: longer term first, then cheapest at compare tier within each term
    results.sort(key=lambda r: (-r["term"], r["tiers"].get(COMPARE_TIER, 999)))

    # For each plan, find the best rate at compare tier among all plans with a strictly longer term
    def best_rate_longer_term(term):
        candidates = [r["tiers"][COMPARE_TIER] for r in results if r["term"] > term]
        return min(candidates) if candidates else None

    # Build output table
    tier_hdrs = [f"{k:,}" for k in USAGE_TIERS]
    ct_label  = f"{COMPARE_TIER/1000:.1f}k".rstrip('0').rstrip('.')
    headers   = ["Provider", "Plan", "Mo", "ETF", "Rnw%", "Flags"] + tier_hdrs + [f"vs best longer@{ct_label}"]

    rows      = []
    cur_term  = None
    _footnotes     = {}   # fees_credits text -> footnote number
    _footnote_ctr  = 0

    for r in results:
        if r["term"] != cur_term:
            if cur_term is not None:
                rows.append([""] * len(headers))
            rows.append([f"-- {r['term']}-Month Plans --"] + [""] * (len(headers) - 1))
            cur_term = r["term"]

        best_longer = best_rate_longer_term(r["term"])
        if best_longer is not None:
            diff  = r["tiers"][COMPARE_TIER] - best_longer
            delta = f"{diff:+.2f}¢"
        else:
            delta = ""

        fn_ref = ""
        if r.get("fees_credits"):
            fc = r["fees_credits"]
            if fc not in _footnotes:
                _footnote_ctr += 1
                _footnotes[fc] = _footnote_ctr
            fn_ref = f"[{_footnotes[fc]}]"

        flags_txt = ""
        if r["has_crd"]: flags_txt += "¢"
        if fn_ref:        flags_txt += f" {fn_ref}"
        rows.append([
            r["provider"][:28],
            r["plan"][:30],
            r["term"],
            r["etf"],
            f"{r['rnw']}%",
            f"{r['src'].upper()} {flags_txt}".strip(),
            *[f"{r['tiers'][k]:.2f}" for k in USAGE_TIERS],
            delta,
        ])

    if args.text_table:
        print("\n")
        print(tabulate(rows, headers=headers, tablefmt="simple", floatfmt=".2f"))
        print(f"""
TDU charges applied: ${tdu['fixed_mo']:.2f}/mo fixed + {tdu['per_kwh']*100:.4f}¢/kWh  (same for all providers)
Flags: [EFL]=exact legal doc | [LLM]=high-accuracy | [API]=estimated | ¢=bill-credit | [n]=fee/credit footnote
All rates in ¢/kWh effective (energy + base + TDU). State/local taxes excluded.
{f"Personal tiers: {', '.join(str(k) for k in USAGE_TIERS if k not in _EFL_TIERS)} kWh  (standard EFL tiers: 500 / 1,000 / 2,000)" if any(k not in _EFL_TIERS for k in USAGE_TIERS) else "Standard EFL tiers only: 500 / 1,000 / 2,000 kWh"}
""")
        if _footnotes:
            print("Fee/Credit Footnotes:")
            for _fc_text, _fc_num in sorted(_footnotes.items(), key=lambda x: x[1]):
                print(f"  [{_fc_num}] {_fc_text}")
            print()
        # Top picks
        from itertools import groupby as _gb
        _starred = [list(g)[0] for _, g in _gb(results, key=lambda r: r["term"])]
        _top3    = sorted(_starred, key=lambda r: r["tiers"].get(COMPARE_TIER, 999))[:3]
        _medals  = ["1st", "2nd", "3rd"]
        print("-" * 72)
        print(f"  TOP PICKS  (cheapest starred plan per term group @ {COMPARE_TIER:,} kWh)")
        print("-" * 72)
        for _i, _r in enumerate(_top3):
            _label = f"Best {_r['term']:>2}-mo"
            print(f"  {_medals[_i]}  {_label}  "
                  f"{_r['provider'][:28]:<28} / {_r['plan'][:28]:<28}  "
                  f"{_r['tiers'][COMPARE_TIER]:.2f}¢  ETF: {_r['etf'] or '$0'}")
        print()

    if not args.no_html:
        _html_latest = Path(args.html)
        _write_html(results, tdu, args.zip, out_path=_html_latest)
        if args.timestamped:
            _html_stamped = _html_latest.parent / f"plans_{_run_stamp}.html"
            _shutil.copy2(_html_latest, _html_stamped)
            print(f"HTML written to:  {_html_latest.name}  +  {_html_stamped.name}")
        else:
            print(f"HTML written to:  {_html_latest.name}")

    if args.json:
        json_out = {
            "generated":    _dt.now().isoformat(timespec="seconds"),
            "zip":          args.zip,
            "tdu":          {"fixed_mo_dollars": tdu["fixed_mo"],
                             "per_kwh_cents":    tdu["per_kwh"] * 100},
            "usage_tiers":  USAGE_TIERS,
            "compare_tier": COMPARE_TIER,
            "plans": [
                {
                    "pid":                   r["pid"],
                    "provider":              r["provider"],
                    "plan":                  r["plan"],
                    "term_months":           r["term"],
                    "cancellation_fee":      r["etf"],
                    "renewable_pct":         r["rnw"],
                    "has_bill_credit":       r["has_crd"],
                    "src":                   r["src"],
                    "energy_charge_cents":   r["ec_cents"],
                    "base_charge_dollars":   r["bc"],
                    "tdu_bundled":           r["tdu_bundled"],
                    "energy_threshold_kwh":  r["energy_threshold_kwh"],
                    "tier_boundary_kwh":     r.get("tier_boundary_kwh", 0),
                    "ec_cents_above_tier":   r.get("ec_cents_above_tier", 0.0),
                    "is_tiered":             r.get("is_tiered", False),
                    "one_time_fee_dollars":  r.get("one_time_fee_dollars", 0.0),
                    "bill_credits":          r["bill_credits"],
                    "rates_cents_per_kwh":   {str(k): round(v, 4) for k, v in r["tiers"].items()},
                    "facts_url":             r["facts_url"],
                    "fees_credits_text":     r["fees_credits"],
                    "special_terms":         r["special_terms"],
                }
                for r in results
            ],
        }
        json_path = Path(args.json)
        json_path.write_text(json.dumps(json_out, indent=2), encoding="utf-8")
        if args.timestamped:
            json_stamped = json_path.parent / f"plans_{_run_stamp}.json"
            _shutil.copy2(json_path, json_stamped)
            print(f"JSON written to:  {json_path.name}  +  {json_stamped.name}")
        else:
            print(f"JSON written to:  {json_path.name}")

    _save_meta()

    # ── Nerd stats ────────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - _t_start
    cache_pdfs  = sum(1 for f in EFL_CACHE.glob("*.pdf"))
    cache_htmls = sum(1 for f in EFL_CACHE.glob("*.html"))
    print(f"\n{'-'*60}")
    print(f"  Wall clock:    {elapsed:.1f}s")
    print(f"  Plans:         {len(results)} results  |  {failed} API fallback  |  {llm_used} LLM fallback")
    print(f"  EFL cache:     {cache_pdfs} PDF  +  {cache_htmls} HTML files")

    import credit_parser as _cp
    if _cp._llm is not None and not args.no_llm:
        print(f"  LLM calls:     {_cp._total_llm_calls}  |  "
              f"prompt {_cp._total_prompt_tokens:,} tok  |  "
              f"completion {_cp._total_completion_tokens:,} tok  |  "
              f"total {_cp._total_prompt_tokens + _cp._total_completion_tokens:,} tok")
        try:
            import llama_cpp
            perf = llama_cpp.llama_perf_context(_cp._llm.ctx)
            print(f"  Model load:    {perf.t_load_ms/1000:.1f}s")
            if perf.t_p_eval_ms > 0:
                print(f"  Prompt eval:   {perf.t_p_eval_ms/1000:.1f}s  "
                      f"({perf.n_p_eval} tok  /  "
                      f"{perf.n_p_eval/(perf.t_p_eval_ms/1000):.0f} tok/s)")
            if perf.t_eval_ms > 0:
                print(f"  Generation:    {perf.t_eval_ms/1000:.1f}s  "
                      f"({perf.n_eval} tok  /  "
                      f"{perf.n_eval/(perf.t_eval_ms/1000):.0f} tok/s)")
        except Exception:
            pass
    print(f"{'-'*60}\n")


def _write_html(results, tdu, zip_code, out_path=None):
    """Write an HTML comparison table. out_path defaults to plans_latest.html next to the script."""
    from datetime import date
    from itertools import groupby

    SRC_TITLE = {
        "efl": "Rates from legal EFL document (exact)",
        "llm": "Rates from LLM extraction (high accuracy)",
        "api": "Rates estimated from CSV price (less accurate)",
    }

    ct_label      = f"{COMPARE_TIER/1000:.1f}k".rstrip('0').rstrip('.')   # e.g. "1k"
    n_cols        = 7 + len(USAGE_TIERS) + 1   # fav + Provider + Plan + Mo + ETF + Rnw% + Flags + tiers + delta
    tier_ths      = "".join(f"<th>{k:,}</th>" for k in USAGE_TIERS)
    tier_ths_titled = "".join(
        f'<th title="Effective rate ¢/kWh at {k:,} kWh/month usage">{k:,}</th>'
        for k in USAGE_TIERS
    )

    SRC_TAG = {
        "efl": "EFL — exact rates from legal document",
        "llm": "LLM — high-accuracy extraction (not from PDF)",
        "api": "API — estimated from CSV price (least accurate)",
    }

    def plan_row(r, is_best=False):
        src      = r["src"]
        url      = r.get("facts_url", "")
        plan_cell = (
            f'<a href="{url}" target="_blank" title="Open EFL">{r["plan"]}</a>'
            if url else r["plan"]
        )

        # Build Flags cell: [EFL/LLM/API] tag + optional ¢ (bill credit) + optional ℹ (fees text)
        tag_title = SRC_TAG.get(src, src.upper())
        flags = (
            f'<span title="{tag_title}" '
            f'style="font-size:0.75em;cursor:default;'
            f'font-weight:bold;user-select:none">[{src.upper()}]</span>'
        )
        if r["has_crd"]:
            flags += (
                ' <span title="Bill-credit plan — advertised rate only applies near '
                'the credit threshold kWh; rates shown reflect actual cost at each tier" '
                'style="cursor:help;display:inline-block;background:#e6a817;color:#fff;'
                'font-size:0.85em;font-weight:700;padding:0px 4px;border-radius:4px;'
                'min-width:1.0em;text-align:center;'
                'vertical-align:middle;user-select:none;line-height:1.4">&#162;</span>'
            )
        fees = r.get("fees_credits", "")
        if fees:
            safe = fees.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            flags += (
                f' <span title="{safe}" '
                f'style="cursor:help;display:inline-block;background:#4a90d9;color:#fff;'
                f'font-size:0.85em;font-weight:700;padding:0px 4px;border-radius:4px;'
                f'min-width:1.0em;text-align:center;'
                f'vertical-align:middle;user-select:none;line-height:1.4">&#8505;</span>'
            )
        one_time = r.get("one_time_fee_dollars", 0.0)
        if one_time and one_time > 0:
            flags += (
                f' <span title="One-time setup fee: ${one_time:.2f} (not included in rates above)" '
                f'style="cursor:help;display:inline-block;background:#c0392b;color:#fff;'
                f'font-size:0.75em;font-weight:700;padding:0px 4px;border-radius:4px;'
                f'vertical-align:middle;user-select:none;line-height:1.4">'
                f'&#9888; ${one_time:.0f}</span>'
            )

        tier_tds = "".join(f'<td>{r["tiers"][k]:.2f}</td>' for k in USAGE_TIERS)

        best_candidates = [
            (x["tiers"][COMPARE_TIER], x["provider"], x["plan"], x["term"])
            for x in results if x["term"] > r["term"]
        ]
        if best_candidates:
            best_rate, best_prov, best_plan_name, best_term = min(
                best_candidates, key=lambda x: x[0]
            )
            delta_val = r["tiers"][COMPARE_TIER] - best_rate
            tip = (f"<span style='color:#f59e0b;font-size:1.5em;vertical-align:middle;margin-right:4px'>&#9878;</span>"
                   f"<b>{best_prov[:28]} / {best_plan_name[:28]}</b>"
                   f" &nbsp;({best_term}-mo)&nbsp;"
                   f"<span style='color:#4ade80;font-weight:700'>{best_rate:.2f}¢</span>")
            cls = "vs-low" if delta_val < 1.0 else ("vs-mid" if delta_val < 3.0 else "vs-high")
            delta_td = (
                f'<td class="vs-cell">'
                f'<span class="{cls}">{delta_val:+.2f}¢</span>'
                f'<span class="vs-tip">{tip}</span></td>'
            )
        else:
            delta_td = '<td></td>'

        # Top row gets a non-clickable star; others get a clickable heart toggle.
        if is_best:
            fav_td = '<td class="star-cell" title="Best in group"></td>'
        else:
            fav_td = '<td class="fav-btn" onclick="toggleHeart(this,event)" title="Favorite this plan"></td>'
        return (
            f'<tr class="src-{src}">'
            f'{fav_td}'
            f'<td>{r["provider"]}</td><td>{plan_cell}</td>'
            f'<td>{r["term"]}</td><td>{r["etf"]}</td>'
            f'<td>{r["rnw"]}%</td>'
            f'<td style="text-align:center;white-space:nowrap">{flags}</td>'
            f'{tier_tds}{delta_td}'
            f'</tr>\n'
        )

    # results are already sorted by (-term, rate); groupby preserves that order.
    # First row in each group is the cheapest (best) — marked with class "best".
    body = ""
    for term, grp in groupby(results, key=lambda r: r["term"]):
        gid        = f"g{term}"
        group_list = list(grp)
        rows       = ""
        for i, r in enumerate(group_list):
            row_class = ' class="best"' if i == 0 else ""
            rows += plan_row(r, is_best=(i == 0)).replace("<tr ", f"<tr{row_class} ", 1)
        body += (
            f'<tr id="{gid}-hdr" class="grp-hdr" data-state="all" '
            f'onclick="toggleGroup(\'{gid}\')">'
            f'<td colspan="{n_cols}" style="padding:6px 10px">'
            f'<span id="{gid}-arrow">&#9660;</span>&nbsp;{term}-Month Plans'
            f'</td></tr>\n'
            f'<tbody id="{gid}">\n{rows}</tbody>\n'
        )

    # ── Top 3 picks cards ──────────────────────────────────────────────────
    medals   = ["🥇", "🥈", "🥉"]
    starred  = []
    for _term, _grp in groupby(results, key=lambda r: r["term"]):
        _gl = list(_grp)
        starred.append(_gl[0])
    top3     = sorted(starred, key=lambda r: r["tiers"][COMPARE_TIER])[:3]
    cards    = ""
    for _i, _r in enumerate(top3):
        _rate  = _r["tiers"][COMPARE_TIER]
        _url   = _r.get("facts_url", "")
        _pname = f'<a href="{_url}" target="_blank" style="color:inherit;text-decoration:none">{_r["plan"]}</a>' if _url else _r["plan"]
        _rank_label = f"Best {_r['term']}-mo"
        cards += (
            f'<div class="top-card"><div class="top-card-inner">'
            f'<div class="top-card-rank">{medals[_i]} <span style="font-size:0.62em;font-family:sans-serif;color:var(--text-mid);font-weight:700;vertical-align:middle">{_rank_label}</span></div>'
            f'<div class="top-card-provider">{_r["provider"]}</div>'
            f'<div class="top-card-plan">{_pname}</div>'
            f'<div class="top-card-rate">{_rate:.2f}¢<span>@ {COMPARE_TIER:,} kWh</span></div>'
            f'<div class="top-card-meta">{_r["term"]}-month &nbsp;·&nbsp; ETF: {_r["etf"] or "$0"}</div>'
            f'</div></div>\n'
        )
    top3_html = f'<div class="top-picks-outer"><div class="top-picks">\n{cards}</div></div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EFL Comparison — {zip_code} — {date.today()}</title>
<style>
  /* ── Dark mode (default) ── */
  body {{
    --bg:        #13131a;
    --surface:   #1c1c26;
    --border:    #2c2c3a;
    --th-bg:     #23232f;
    --th-text:   #c8c8d8;
    --text:      #d0d0da;
    --text-dim:  #6a6a7a;
    --text-mid:  #9a9aaa;
    --hover:     rgba(255,255,255,0.05);
    --link:      #60a5fa;
    --row-efl:   #0c2015;
    --row-llm:   #1e1a07;
    --row-api:   #200d0d;
    --grp-bg:    #23232f;
    --grp-text:  #c8c8d8;
    --sw-border: #3a3a4a;
    font-family: monospace; font-size: 18px; padding: 20px; margin: 0;
    background: var(--bg); color: var(--text);
    position: relative;
  }}
  /* ── Light mode ── */
  body.light {{
    --bg:        #f8f9fa;
    --surface:   #ffffff;
    --border:    #dee2e6;
    --th-bg:     #495057;
    --th-text:   #ffffff;
    --text:      #212529;
    --text-dim:  #555555;
    --text-mid:  #777777;
    --hover:     rgba(0,0,0,0.06);
    --link:      #0066cc;
    --row-efl:   #d4edda;
    --row-llm:   #fff3cd;
    --row-api:   #f8d7da;
    --grp-bg:    #343a40;
    --grp-text:  #ffffff;
    --sw-border: #cccccc;
  }}
  h2 {{ font-family: sans-serif; color: var(--text); }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid var(--border); padding: 4px 8px; white-space: nowrap; }}
  th {{ background: var(--th-bg); color: var(--th-text); position: sticky; top: 0; z-index: 1; }}
  tr.src-efl {{ background: var(--row-efl); }}
  tr.src-llm {{ background: var(--row-llm); }}
  tr.src-api {{ background: var(--row-api); }}
  tr:hover td {{ background-color: var(--hover); }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .grp-hdr {{ background: var(--grp-bg) !important; color: var(--grp-text) !important;
              font-weight: bold; cursor: pointer; user-select: none; }}
  .grp-hdr:hover td {{ background-color: rgba(255,255,255,0.12) !important; }}
  tbody.show-best .hideable {{ display: none; }}
  td.fav-btn, td.star-cell, thead th:first-child {{ padding: 1px 3px; width: 30px; min-width: 30px; max-width: 30px; text-align: center; }}
  td.fav-btn {{ cursor: pointer; }}
  td.fav-btn::before  {{ content: '❤'; font-size: 1.1em; line-height: 1; color: #e8c8d0; }}
  td.fav-btn.filled::before {{ color: #cc0033; }}
  td.star-cell::before {{ content: '★'; font-size: 1.5em; line-height: 0.75; color: #f0c040; }}
  .legend {{ margin-top: 12px; font-family: sans-serif; font-size: 0.8em; color: var(--text-dim); }}
  .swatch {{ display:inline-block; width:14px; height:14px;
             vertical-align:middle; margin-right:4px; border:1px solid var(--sw-border); }}
  .swatch.src-efl {{ background: var(--row-efl); }}
  .swatch.src-llm {{ background: var(--row-llm); }}
  .swatch.src-api {{ background: var(--row-api); }}
  /* ── Top picks cards ── */
  .top-picks-outer {{ width: 75%; margin: 8px auto 12px; }}
  .top-picks {{ display: flex; justify-content: space-evenly; gap: 16px; }}
  .top-card {{
    flex: 0 0 auto; min-width: 260px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 12px; font-family: sans-serif; overflow: hidden;
    display: flex; flex-direction: column; align-items: center;
  }}
  .top-card-inner {{ text-align: left; }}
  .top-card-rank {{ font-size: 1.5em; line-height: 1; margin-bottom: 4px; }}
  .top-card-provider {{ font-size: 0.78em; color: var(--text-dim); margin-bottom: 2px; }}
  .top-card-plan {{ font-size: 0.95em; font-weight: 700; color: var(--text); margin-bottom: 6px; }}
  .top-card-rate {{ font-size: 1.6em; font-weight: 700; color: #4ade80; line-height: 1; }}
  body.light .top-card-rate {{ color: #16a34a; }}
  .top-card-rate span {{ font-size: 0.45em; color: var(--text-dim); font-weight: 400; margin-left: 4px; vertical-align: middle; }}
  .top-card-meta {{ font-size: 0.78em; color: var(--text-dim); margin-top: 4px; }}
  .top-card:nth-child(1) {{ border-left: 3px solid #f59e0b; }}
  .top-card:nth-child(2) {{ border-left: 3px solid #94a3b8; }}
  .top-card:nth-child(3) {{ border-left: 3px solid #b45309; }}
  /* ── VS column custom tooltip ── */
  .vs-cell {{ white-space: nowrap; cursor: default; }}
  .vs-tip {{
    display: none; position: fixed;
    background: var(--surface); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 12px; font-size: 0.82em; font-family: sans-serif;
    white-space: nowrap; z-index: 20; pointer-events: none;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }}
  .vs-low  {{ color: #4ade80; }}
  .vs-mid  {{ color: #f59e0b; }}
  .vs-high {{ color: #f87171; }}
  body.light .vs-low  {{ color: #16a34a; }}
  body.light .vs-mid  {{ color: #d97706; }}
  body.light .vs-high {{ color: #dc2626; }}
  /* ── Theme toggle button ── */
  #theme-btn {{
    position: absolute; top: 14px; right: 18px;
    background: var(--surface); color: var(--text);
    border: 1px solid var(--border); border-radius: 20px;
    padding: 4px 14px; font-size: 0.8em; font-family: sans-serif;
    cursor: pointer; transition: background 0.15s; white-space: nowrap;
  }}
  #theme-btn:hover {{ background: var(--th-bg); color: var(--th-text); }}
</style>
<script>
function toggleGroup(id) {{
  var tbody = document.getElementById(id);
  var arrow = document.getElementById(id + '-arrow');
  var hdr   = document.getElementById(id + '-hdr');
  var state = hdr.getAttribute('data-state');

  if (state === 'all') {{
    // One class toggle — CSS handles row filtering, no per-row DOM mutations
    tbody.classList.add('show-best');
    arrow.innerHTML = '&#9733;';
    hdr.setAttribute('data-state', 'best');
  }} else if (state === 'best') {{
    tbody.classList.remove('show-best');
    tbody.style.display = 'none';
    arrow.innerHTML = '&#9654;';
    hdr.setAttribute('data-state', 'hidden');
  }} else {{
    tbody.style.display = '';
    arrow.innerHTML = '&#9660;';
    hdr.setAttribute('data-state', 'all');
  }}
}}

function toggleAll() {{
  var th      = document.getElementById('global-toggle');
  var state   = th.getAttribute('data-state');
  var tbodies = Array.from(document.querySelectorAll('tbody[id]'));

  if (state === 'all') {{
    tbodies.forEach(function(tbody) {{
      tbody.classList.add('show-best');
      tbody.style.display = '';
      var hdr = document.getElementById(tbody.id + '-hdr');
      if (hdr) {{ hdr.setAttribute('data-state', 'best'); document.getElementById(tbody.id + '-arrow').innerHTML = '&#9733;'; }}
    }});
    th.innerHTML = '&#9733;';
    th.setAttribute('data-state', 'best');
  }} else if (state === 'best') {{
    tbodies.forEach(function(tbody) {{
      tbody.classList.remove('show-best');
      tbody.style.display = 'none';
      var hdr = document.getElementById(tbody.id + '-hdr');
      if (hdr) {{ hdr.setAttribute('data-state', 'hidden'); document.getElementById(tbody.id + '-arrow').innerHTML = '&#9654;'; }}
    }});
    th.innerHTML = '&#9654;';
    th.setAttribute('data-state', 'hidden');
  }} else {{
    tbodies.forEach(function(tbody) {{
      tbody.classList.remove('show-best');
      tbody.style.display = '';
      var hdr = document.getElementById(tbody.id + '-hdr');
      if (hdr) {{ hdr.setAttribute('data-state', 'all'); document.getElementById(tbody.id + '-arrow').innerHTML = '&#9660;'; }}
    }});
    th.innerHTML = '&#9660;';
    th.setAttribute('data-state', 'all');
  }}
}}

function toggleHeart(td, event) {{
  event.stopPropagation();
  var row   = td.parentElement;
  var tbody = row.parentElement;

  if (row.classList.contains('fav')) {{
    row.classList.remove('fav');
    row.classList.add('hideable');
    td.classList.remove('filled');
  }} else {{
    row.classList.add('fav');
    row.classList.remove('hideable');
    td.classList.add('filled');
  }}
}}

// Mark hideable rows once on load so toggles use fast class lookup
document.addEventListener('DOMContentLoaded', function() {{
  document.querySelectorAll('tbody tr:not(.best)').forEach(function(r) {{
    r.classList.add('hideable');
  }});
}});

// VS column tooltip — anchored above the cell
function _showVsTip(cell) {{
  var tip = cell.querySelector('.vs-tip');
  if (!tip) return;
  tip.style.display = 'block';
  var r = cell.getBoundingClientRect();
  var tw = tip.offsetWidth, th = tip.offsetHeight;
  var tx = r.left + (r.width - tw) / 2;
  if (tx < 6) tx = 6;
  if (tx + tw > window.innerWidth - 6) tx = window.innerWidth - tw - 6;
  var ty = r.top - th - 6;
  if (ty < 6) ty = r.bottom + 6;
  tip.style.left = tx + 'px';
  tip.style.top  = ty + 'px';
}}
function _hideAllVsTips() {{
  document.querySelectorAll('.vs-tip').forEach(function(t) {{ t.style.display = 'none'; }});
}}
document.addEventListener('mouseover', function(e) {{
  var cell = e.target.closest ? e.target.closest('.vs-cell') : null;
  _hideAllVsTips();
  if (cell) _showVsTip(cell);
}});
// Hide when mouse enters right 20% of cell (about to leave browser edge)
document.addEventListener('mousemove', function(e) {{
  var cell = e.target.closest ? e.target.closest('.vs-cell') : null;
  if (cell) {{
    var r = cell.getBoundingClientRect();
    if (e.clientX > r.left + r.width * 0.8) _hideAllVsTips();
  }}
}});
// Hide when mouse leaves the browser window entirely
document.addEventListener('mouseleave', function() {{ _hideAllVsTips(); }});

function toggleDark() {{
  var body = document.body;
  var btn  = document.getElementById('theme-btn');
  if (body.classList.contains('light')) {{
    body.classList.remove('light');
    btn.textContent = '☀ Light';
  }} else {{
    body.classList.add('light');
    btn.textContent = '🌙 Dark';
  }}
}}
</script>
</head>
<body>
<button id="theme-btn" onclick="toggleDark()" title="Toggle dark / light mode">☀ Light</button>
<h2>Oncor Fixed Plans — {zip_code} — {date.today()}</h2>
<p style="font-family:sans-serif;font-size:0.85em;color:var(--text-dim)">
  TDU: ${tdu['fixed_mo']:.2f}/mo + {tdu['per_kwh']*100:.4f}¢/kWh &nbsp;|&nbsp;
  All rates ¢/kWh effective (energy + base + TDU, excl. taxes) &nbsp;|&nbsp;
  Plan name links open the EFL in a new tab.
</p>
{top3_html}
<table aria-hidden="true">
<thead>
<tr>
  <th id="global-toggle" data-state="all" onclick="toggleAll()" style="cursor:pointer;user-select:none" title="Toggle all groups: ▼ all expanded / ★ best per group / ▶ all hidden">&#9660;</th>
  <th>Provider</th>
  <th>Plan</th>
  <th title="Contract term in months">Mo</th>
  <th title="Early termination fee">ETF</th>
  <th title="Renewable energy content percentage">Rnw%</th>
  <th title="[EFL] green=exact from legal document | [LLM] amber=high-accuracy extraction | [API] red=estimated from CSV price&#10;&#162; Bill-credit plan: low rate only valid near credit threshold kWh&#10;&#8505; Hover for fee/credit details">Flags</th>
  {tier_ths_titled}
  <th title="Rate difference vs best plan with longer contract, at {ct_label} kWh/month">vs best longer@{ct_label}</th>
</tr>
</thead>
{body}
</table>
<div class="legend">
  <span class="swatch src-efl"></span> EFL — exact (legal document) &nbsp;
  <span class="swatch src-llm"></span> LLM — high accuracy &nbsp;
  <span class="swatch src-api"></span> API — estimated (less accurate) &nbsp;
  <span style="display:inline-block;background:#e6a817;color:#fff;font-size:0.85em;font-weight:700;padding:0px 4px;border-radius:4px;vertical-align:middle;line-height:1.4">&#162;</span> Bill-credit plan — rate only valid near the credit threshold kWh
</div>
</body>
</html>
"""

    out = out_path if out_path is not None else _ROOT / "plans_latest.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"HTML table written to: {out}")


if __name__ == "__main__":
    main()
