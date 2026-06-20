#!/usr/bin/env python3
"""
golden_review.py — Record a human review override on a golden file entry.

GOLDEN FILE BACKUP LOCATIONS (three-copy strategy)
===================================================
  1. Live:           _golden\\golden_plans.json  (this directory)
  2. In-project:     _golden\\golden_plans.backup_YYYYMMDD.json  (this directory)
  3. Offsite:        C:\\Users\\<username>\\Documents\\EFL_backups\\golden_plans.backup_YYYYMMDD.json

Before making significant changes to golden_plans.json, refresh copies 2 and 3.
The offsite copy survives any catastrophic command run against the project directory.

Usage:
    # Show current data for a plan (no changes made):
    py -3.12 _dev/golden_review.py <pid>

    # Write a sparse override (only supply fields that differ from agent data):
    py -3.12 _dev/golden_review.py <pid> [--field value ...] [--reviewer name] [--notes "text"]

Overridable fields (same names as in golden_plans.json golden object):
    --energy_charge_cents     float
    --base_charge_dollars     float
    --tdu_bundled             true/false
    --energy_charge_threshold_kwh  int
    --tier_boundary_kwh       int
    --energy_charge_cents_above_tier  float
    --additional_monthly_fee_dollars  float
    --bill_credits            JSON array string
    --confidence              high / medium / low

Metadata (always written):
    --reviewer   (default: human)
    --notes      (default: "")

Examples:
    py -3.12 _dev/golden_review.py 27831 --energy_charge_cents 7.3 --confidence high \
        --notes "EFL states 7.3c explicitly; energy charge is authoritative per PUCT 25.475"

    py -3.12 _dev/golden_review.py 36344 --confidence high \
        --notes "bill_credits=[] correct; $200 is one-time promotional, not in EFL Electricity Price section"
"""

import argparse
import json
import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE   = pathlib.Path(__file__).parent
GOLDEN = BASE / "golden_plans.json"

_FLOAT_FIELDS = {
    "energy_charge_cents", "base_charge_dollars",
    "energy_charge_cents_above_tier", "one_time_fee_dollars",
}
_INT_FIELDS = {
    "energy_charge_threshold_kwh", "tier_boundary_kwh",
}
_BOOL_FIELDS = {"tdu_bundled"}
_STR_FIELDS  = {"confidence"}
_JSON_FIELDS = {"bill_credits"}
_ALL_OVERRIDE_FIELDS = _FLOAT_FIELDS | _INT_FIELDS | _BOOL_FIELDS | _STR_FIELDS | _JSON_FIELDS


def parse_args():
    p = argparse.ArgumentParser(description="Record a human review override on a golden file entry.")
    p.add_argument("pid", help="Plan ID to review")
    p.add_argument("--energy_charge_cents",            type=float)
    p.add_argument("--base_charge_dollars",            type=float)
    p.add_argument("--tdu_bundled",                    type=lambda x: x.lower() == "true")
    p.add_argument("--energy_charge_threshold_kwh",    type=int)
    p.add_argument("--tier_boundary_kwh",              type=int)
    p.add_argument("--energy_charge_cents_above_tier", type=float)
    p.add_argument("--one_time_fee_dollars",           type=float)
    p.add_argument("--bill_credits",                   type=str, help="JSON array, e.g. '[]'")
    p.add_argument("--confidence",                     choices=["high", "medium", "low"])
    p.add_argument("--reviewer",                       default="human")
    p.add_argument("--notes",                          default="")
    return p.parse_args()


def show_plan(gp: dict) -> None:
    g  = gp.get("golden", {})
    hr = gp.get("human_review", {})
    print(f"\n{'='*60}")
    print(f"  {gp['provider']} / {gp['plan']}  (pid {gp['pid']}, {gp['term_months']} mo)")
    print(f"{'='*60}")
    print(f"  Agent golden:")
    for k, v in g.items():
        if k == "verification_shown":
            continue
        print(f"    {k}: {v}")
    if hr:
        print(f"\n  Human review ({hr.get('reviewer','?')} on {hr.get('reviewed_at','?')}):")
        for k, v in hr.items():
            print(f"    {k}: {v}")
        print()
        print("  Effective (merged):")
        eff = {**g, **{k: v for k, v in hr.items() if k not in {"reviewer","reviewed_at","notes"}}}
        for k in _ALL_OVERRIDE_FIELDS:
            if k in eff:
                marker = " [HR]" if k in hr else ""
                print(f"    {k}: {eff[k]}{marker}")
    else:
        print("\n  No human review on file.")
    print()


def main():
    args = parse_args()
    pid  = str(args.pid)

    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    plans = data.get("plans", [])

    idx = next((i for i, p in enumerate(plans) if str(p["pid"]) == pid), None)
    if idx is None:
        print(f"ERROR: pid {pid} not found in {GOLDEN.name}")
        sys.exit(1)

    gp = plans[idx]

    # Build the override dict from supplied args
    overrides = {}
    for field in _FLOAT_FIELDS:
        v = getattr(args, field, None)
        if v is not None:
            overrides[field] = v
    for field in _INT_FIELDS:
        v = getattr(args, field, None)
        if v is not None:
            overrides[field] = v
    for field in _BOOL_FIELDS:
        v = getattr(args, field, None)
        if v is not None:
            overrides[field] = v
    for field in _STR_FIELDS:
        v = getattr(args, field, None)
        if v is not None:
            overrides[field] = v
    if args.bill_credits is not None:
        overrides["bill_credits"] = json.loads(args.bill_credits)

    if not overrides and not args.notes:
        # Read-only mode — just show current state
        show_plan(gp)
        return

    # Write the human_review object (merge with any existing review)
    existing_hr = gp.get("human_review", {})
    new_hr = {**existing_hr, **overrides}
    new_hr["reviewer"]    = args.reviewer
    new_hr["reviewed_at"] = str(date.today())
    if args.notes:
        new_hr["notes"] = args.notes
    elif "notes" not in new_hr:
        new_hr["notes"] = ""

    plans[idx]["human_review"] = new_hr
    data["plans"] = plans

    GOLDEN.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(f"Updated human_review for pid {pid} ({gp['provider']} / {gp['plan']}):")
    for k, v in new_hr.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
