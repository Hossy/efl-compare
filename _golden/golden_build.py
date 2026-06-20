#!/usr/bin/env python3
"""
golden_build.py — Construct a ground-truth golden JSON for comparing against
the automated efl_compare.py output.

For each English Oncor plan:
  1. Load the cached EFL PDF via pdfplumber.
  2. Run the expanded LLM parse (parse_rates_from_efl_text) to get the full
     pricing structure: ec, bc, tdu_bundled, threshold, additional_fee.
  3. Run parse_credits / parse_credits_from_efl_text for bill credits.
  4. Compute golden effective rates at 6 tiers using the same formula as the
     script but with the LLM-derived values (which catch structural features
     the regex misses).
  5. For plans with no PDF, fall back to PUCT back-calculation (marked low
     confidence).

Output: golden_plans.json
"""

import csv
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).parent.parent   # project root (one level up from _golden/)
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import efl_compare
import credit_parser as cp

BASE    = ROOT
CSV     = BASE / "plans_latest.csv"
CACHE   = BASE / "efls_cache"
OUT     = pathlib.Path(__file__).parent / "golden_plans.json"   # writes to _golden/

TDU_KWH  = 6.1196 / 100   # $/kWh
TDU_FIX  = 4.06            # $/mo
_EFL_TIERS = [500, 1000, 2000]
_extra   = [int(t.strip()) for t in os.environ["EFL_TIERS"].split(",") if t.strip()] \
           if os.environ.get("EFL_TIERS") else []
TIERS    = sorted(set(_EFL_TIERS + _extra))


def effective_c(ec, bc, kwh, credits, tdu_bundled, threshold, inc_enrollment=True):
    """Compute all-in effective rate in ¢/kWh at a given usage level."""
    credit = sum(
        c["amount"] for c in credits
        if c["threshold_kwh"] <= kwh
        and (inc_enrollment or not c.get("requires_enrollment", False))
    )
    billable = max(0, kwh - threshold) if threshold > 0 else kwh
    if tdu_bundled:
        total = ec * billable + bc - credit
    else:
        total = ec * billable + bc + TDU_FIX + TDU_KWH * kwh - credit
    return (total / kwh) * 100


def golden_build_for_plan(plan_row, pdf_path):
    """
    Returns a golden dict for one plan.
    pdf_path is None if no PDF is cached.
    """
    pid      = plan_row["[idKey]"]
    provider = (plan_row.get("[RepCompany]") or "").strip()
    name     = (plan_row.get("[Product]")    or "").strip()
    term     = int(plan_row.get("[TermValue]") or 0)
    has_crd  = plan_row.get("[MinUsageFeesCredits]", "") == "TRUE"

    puct = {
        500:  float(plan_row.get("[kwh500]")  or 0) * 100,
        1000: float(plan_row.get("[kwh1000]") or 0) * 100,
        2000: float(plan_row.get("[kwh2000]") or 0) * 100,
    }

    golden = {
        "pid":             pid,
        "provider":        provider,
        "plan":            name,
        "term_months":     term,
        "has_bill_credit": has_crd,
        "puct_kwh500c":    puct[500],
        "puct_kwh1000c":   puct[1000],
        "puct_kwh2000c":   puct[2000],
        "facts_url":       (plan_row.get("[FactsURL]")     or "").strip(),
        "fees_credits":    (plan_row.get("[Fees/Credits]") or "").strip(),
        "special_terms":   (plan_row.get("[SpecialTerms]") or "").strip(),
    }

    if pdf_path is None:
        # No PDF — PUCT back-calc only
        ec_api = puct[1000] / 100 - TDU_KWH - TDU_FIX / 1000
        golden["golden"] = {
            "energy_charge_cents":           round(ec_api * 100, 4),
            "base_charge_dollars":           0.0,
            "tdu_bundled":                   False,
            "energy_charge_threshold_kwh":   0,
            "additional_monthly_fee_dollars": 0.0,
            "bill_credits":                  [],
            "rates_cents_per_kwh":           {},
            "confidence":                    "low",
            "notes":                         "No PDF — PUCT back-calculation only",
        }
        rates = {}
        for k in TIERS:
            rates[str(k)] = round(effective_c(ec_api, 0.0, k, [], False, 0), 4)
        golden["golden"]["rates_cents_per_kwh"] = rates
        return golden

    # -- Parse EFL via regex first -----------------------------------------------
    try:
        efl = efl_compare.parse_efl(pdf_path)
    except Exception as e:
        efl = {"energy_charge": None, "base_charge": 0.0,
               "bill_credits": [], "raw_text": "", "tdu_bundled": False}

    raw_text = efl.get("raw_text", "")

    # -- LLM structural parse (full Electricity Price section) --------------------
    struct = None
    if raw_text:
        try:
            struct = cp.parse_rates_from_efl_text(raw_text)
        except Exception:
            pass

    # -- Determine golden ec, bc, tdu_bundled, threshold, additional_fee ----------
    if struct and struct.get("energy_charge_cents"):
        ec_g       = struct["energy_charge_cents"] / 100
        bc_g       = struct["base_charge_dollars"]
        bundled_g  = struct.get("tdu_bundled", False)
        thresh_g   = struct.get("energy_charge_threshold_kwh", 0)
        extra_g    = struct.get("additional_monthly_fee_dollars", 0.0)
        source     = "llm"
    elif efl.get("energy_charge") is not None:
        ec_g       = efl["energy_charge"]
        bc_g       = efl["base_charge"]
        bundled_g  = efl.get("tdu_bundled", False)
        thresh_g   = efl.get("energy_charge_threshold", 0)
        extra_g    = 0.0
        source     = "regex"
    else:
        # regex failed, LLM failed — PUCT back-calc
        ec_g      = puct[1000] / 100 - TDU_KWH - TDU_FIX / 1000
        bc_g      = 0.0
        bundled_g = False
        thresh_g  = 0
        extra_g   = 0.0
        source    = "puct"

    # Adjust for extra fee
    bc_adj = bc_g + extra_g

    # If TDU bundled AND threshold: use original rates (don't unbundle for rate calc)
    if bundled_g and thresh_g > 0:
        ec_calc   = ec_g
        bc_calc   = bc_adj
        tdu_b_eff = True
    elif bundled_g:
        ec_calc   = ec_g - TDU_KWH
        bc_calc   = max(0.0, bc_adj - TDU_FIX)
        tdu_b_eff = False   # TDU re-added in effective_c
    else:
        ec_calc   = ec_g
        bc_calc   = bc_adj
        tdu_b_eff = False

    # -- Bill credits -------------------------------------------------------------
    credits_g = []
    if has_crd:
        # Try EFL regex credits first
        regex_credits = efl.get("bill_credits", [])
        # Then CSV LLM credits
        csv_text = plan_row.get("[Fees/Credits]", "")
        try:
            csv_credits = cp.parse_credits(csv_text) if csv_text else []
        except Exception:
            csv_credits = []

        # If they agree, use LLM (more consistent); if not, use EFL regex
        if regex_credits and csv_credits:
            credits_g = regex_credits  # legal doc takes precedence
        elif regex_credits:
            credits_g = regex_credits
        elif csv_credits:
            credits_g = csv_credits
        else:
            # Fall back to LLM re-parse of EFL text
            if raw_text:
                try:
                    credits_g = cp.parse_credits_from_efl_text(raw_text) or []
                except Exception:
                    credits_g = []

    # -- Compute golden rates at all 6 tiers --------------------------------------
    rates = {}
    for k in TIERS:
        rates[str(k)] = round(effective_c(ec_calc, bc_calc, k, credits_g, tdu_b_eff, thresh_g), 4)

    # -- Confidence ---------------------------------------------------------------
    confidence = "high" if source in ("llm", "regex") else "low"
    # Downgrade if LLM and puct don't reconcile at 1000 kWh
    if source == "llm" and puct[1000] > 0:
        our_1000 = rates.get("1000", 0)
        if abs(our_1000 - puct[1000]) > 1.5:
            confidence = "medium"

    notes_parts = []
    if bundled_g:        notes_parts.append("TDU bundled")
    if thresh_g > 0:     notes_parts.append(f"Energy charge above {thresh_g} kWh only")
    if extra_g > 0:      notes_parts.append(f"${extra_g:.2f}/mo amortised one-time fee")
    if credits_g:        notes_parts.append(f"{len(credits_g)} bill credit(s)")
    notes = "; ".join(notes_parts) if notes_parts else "Standard plan"

    golden["golden"] = {
        "energy_charge_cents":           round(ec_g * 100, 4),
        "base_charge_dollars":           round(bc_g, 4),
        "tdu_bundled":                   bundled_g,
        "energy_charge_threshold_kwh":   thresh_g,
        "additional_monthly_fee_dollars": round(extra_g, 4),
        "bill_credits":                  credits_g,
        "rates_cents_per_kwh":           rates,
        "confidence":                    confidence,
        "notes":                         notes,
        "parse_source":                  source,
    }
    return golden


def main():
    t0 = time.perf_counter()

    # Load active English Oncor fixed non-prepaid plans (matches efl_compare.py filter)
    plans = []
    with open(CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if (row.get("[Language]") or "").strip().lower() != "english":
                continue
            if "oncor" not in (row.get("[TduCompanyName]") or "").lower():
                continue
            if row.get("[Fixed]") != "1":
                continue
            if row.get("[PrePaid]") == "True":
                continue
            plans.append(row)

    print(f"  Building golden file for {len(plans)} plans...")

    records = []
    for i, row in enumerate(plans, 1):
        pid     = row["[idKey]"]
        matches = list(CACHE.glob(f"*_{pid}.pdf"))
        pdf     = matches[0] if matches else None

        provider = (row.get("[RepCompany]") or "").strip()[:28]
        name     = (row.get("[Product]")    or "").strip()[:28]
        sys.stdout.write(f"\r  [{i:>3}/{len(plans)}] {provider:<28}  {name:<28}")
        sys.stdout.flush()

        rec = golden_build_for_plan(row, pdf)
        records.append(rec)

    print()

    # Sort: term desc, provider, plan
    records.sort(key=lambda r: (-r["term_months"], r["provider"], r["plan"]))

    output = {
        "schema_version": "1.0",
        "description":    "Ground-truth golden records: LLM reads full EFL section directly",
        "tdu":            {"fixed_mo_dollars": TDU_FIX, "per_kwh_cents": TDU_KWH * 100},
        "plans":          records,
    }

    OUT.write_text(json.dumps(output, indent=2), encoding="utf-8")
    elapsed = time.perf_counter() - t0

    high = sum(1 for r in records if r["golden"]["confidence"] == "high")
    med  = sum(1 for r in records if r["golden"]["confidence"] == "medium")
    low  = sum(1 for r in records if r["golden"]["confidence"] == "low")
    llm_calls = cp._total_llm_calls

    print(f"\n  Written {len(records)} records to {OUT}")
    print(f"  Confidence: {high} high / {med} medium / {low} low")
    print(f"  LLM calls:  {llm_calls}")
    print(f"  Elapsed:    {elapsed:.1f}s")


if __name__ == "__main__":
    main()
