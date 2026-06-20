#!/usr/bin/env python3
"""
golden_compare.py — Compare plans_latest.json (script output) against
golden_plans.json (ground truth) and report discrepancies.

GOLDEN FILE BACKUP LOCATIONS (three-copy strategy)
===================================================
  1. Live:           _golden\\golden_plans.json  (this directory)
  2. In-project:     _golden\\golden_plans.backup_YYYYMMDD.json  (this directory)
  3. Offsite:        C:\\Users\\<username>\\Documents\\EFL_backups\\golden_plans.backup_YYYYMMDD.json

Before making significant changes to golden_plans.json, refresh copies 2 and 3.
The offsite copy survives any catastrophic command run against the project directory.

Human review support:
    If a plan record in golden_plans.json contains a "human_review" object,
    those fields are merged over the agent-generated "golden" fields before
    comparison (sparse override — only fields present in human_review differ).
    Reserved metadata keys (reviewer, reviewed_at, notes) are never used as
    comparison values.

Exit 0 always; findings are printed to stdout.
"""

import argparse
import json
import os
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE   = pathlib.Path(__file__).parent   # _golden/ directory
ROOT   = BASE.parent                     # project root
GOLDEN = BASE / "golden_plans.json"

_EFL_TIERS = [500, 1000, 2000]

def _parse_args():
    p = argparse.ArgumentParser(
        description="Compare script JSON output against the golden file."
    )
    p.add_argument("script", nargs="?",
                   default=str(ROOT / "plans_latest.json"),
                   help="Path to plans_latest.json (default: plans_latest.json in project root)")
    p.add_argument("--tiers", default=os.environ.get("EFL_TIERS"), metavar="A,B,...",
                   help="Personal usage tiers to include in rate comparison, comma-separated. "
                        "Defaults to EFL_TIERS env var; omit for standard EFL tiers only.")
    p.add_argument("--compare-tier", type=int, default=None, metavar="N",
                   help="kWh tier used for sort/delta display. "
                        "Defaults to EFL_COMPARE_TIER env var, then median of --tiers, then 1000.")
    return p.parse_args()

args        = _parse_args()
SCRIPT      = pathlib.Path(args.script)
_extra      = [int(t.strip()) for t in args.tiers.split(",") if t.strip()] if args.tiers else []
TIERS       = sorted(set(_EFL_TIERS + _extra))
COMPARE_TIER = (args.compare_tier
                or int(os.environ.get("EFL_COMPARE_TIER", 0))
                or (_extra[len(_extra)//2] if _extra else 1000))

RATE_TOL = 0.15   # ¢/kWh tolerance for rate comparison
EC_TOL   = 0.05   # ¢/kWh tolerance for energy charge comparison

# Fields in human_review that are metadata only — never used as comparison values
_HR_META = {"reviewer", "reviewed_at", "notes"}


def effective_golden(gp: dict) -> dict:
    """
    Return the effective ground-truth record for a plan: agent golden data
    with any human_review fields merged on top (sparse override).
    Metadata keys are stripped before returning.
    """
    g  = dict(gp.get("golden", {}))
    hr = {k: v for k, v in gp.get("human_review", {}).items() if k not in _HR_META}
    g.update(hr)
    return g


def load():
    script_raw = json.loads(SCRIPT.read_text(encoding="utf-8"))
    golden_raw = json.loads(GOLDEN.read_text(encoding="utf-8"))

    script_plans = {r["pid"]: r for r in script_raw.get("plans", [])}
    golden_plans = {r["pid"]: r for r in golden_raw.get("plans", [])}

    return script_plans, golden_plans


def credit_sig(credits):
    """Canonical string for a credits list, for equality comparison."""
    if not credits:
        return "[]"
    key = lambda c: (c["threshold_kwh"], c.get("requires_enrollment", False))
    return json.dumps(sorted(
        [{"amount": float(round(c["amount"], 2)), "threshold_kwh": c["threshold_kwh"],
          "cumulative": c.get("cumulative", False),
          "requires_enrollment": c.get("requires_enrollment", False)}
         for c in credits],
        key=key
    ), sort_keys=True)


def compare():
    script_plans, golden_plans = load()

    issues = []
    matched = 0
    only_in_script   = []
    only_in_golden   = []
    low_confidence   = []
    human_reviewed   = []

    all_pids = sorted(set(script_plans) | set(golden_plans))

    for pid in all_pids:
        sp = script_plans.get(pid)
        gp = golden_plans.get(pid)

        if sp is None:
            only_in_golden.append(f"  {pid}: {gp['provider'][:28]} / {gp['plan'][:30]}")
            continue
        if gp is None:
            only_in_script.append(f"  {pid}: {sp['provider'][:28]} / {sp['plan'][:30]}")
            continue

        # Merge human_review (sparse override) over agent golden data
        g    = effective_golden(gp)
        hr   = gp.get("human_review", {})
        has_hr = bool({k for k in hr if k not in _HR_META})
        if has_hr:
            human_reviewed.append(pid)

        conf = g.get("confidence", "low")
        if conf == "low":
            low_confidence.append(pid)
            continue   # don't compare low-confidence records

        hr_tag = " [HR]" if has_hr else ""
        label  = f"{sp['provider'][:26]:<26} / {sp['plan'][:28]:<28}{hr_tag}"
        plan_issues = []

        # 1. Source tag
        script_src = sp.get("src", "?")
        golden_src = g.get("parse_source", "?")
        # Only flag if script is "api" and golden isn't (downgrade)
        if script_src == "api" and golden_src != "puct":
            plan_issues.append(
                f"    src: script={script_src}  golden_source={golden_src}  "
                f"(script probably has wrong rate)"
            )

        # 2. TDU bundled flag
        script_bundled = sp.get("tdu_bundled", False)
        golden_bundled = g.get("tdu_bundled", False)
        if script_bundled != golden_bundled:
            plan_issues.append(f"    tdu_bundled: script={script_bundled}  golden={golden_bundled}")

        # 3. Energy threshold
        script_thresh = sp.get("energy_threshold_kwh", 0)
        golden_thresh = g.get("energy_charge_threshold_kwh", 0)
        if script_thresh != golden_thresh:
            plan_issues.append(
                f"    energy_threshold_kwh: script={script_thresh}  golden={golden_thresh}"
            )

        # 4. Energy charge (only when both have non-zero ec)
        script_ec = sp.get("ec_cents", 0)
        golden_ec = g.get("energy_charge_cents", 0)
        if script_ec > 0 and golden_ec > 0 and abs(script_ec - golden_ec) > EC_TOL:
            plan_issues.append(
                f"    energy_charge_cents: script={script_ec:.4f}  golden={golden_ec:.4f}  "
                f"delta={script_ec - golden_ec:+.4f}¢"
            )

        # 5. Base charge  (JSON key is "base_charge_dollars", not "bc")
        script_bc = sp.get("base_charge_dollars", 0)
        golden_bc = g.get("base_charge_dollars", 0)
        if abs(script_bc - golden_bc) > 0.10:
            plan_issues.append(
                f"    base_charge_dollars: script=${script_bc:.2f}  golden=${golden_bc:.2f}  "
                f"delta=${script_bc - golden_bc:+.2f}"
            )

        # 6. Bill credits
        script_cred = credit_sig(sp.get("bill_credits", []))
        golden_cred = credit_sig(g.get("bill_credits", []))
        if script_cred != golden_cred:
            plan_issues.append(
                f"    bill_credits differ:\n"
                f"      script: {sp.get('bill_credits', [])}\n"
                f"      golden: {g.get('bill_credits', [])}"
            )

        # 7. Tier boundary
        script_tier = sp.get("tier_boundary_kwh", 0)
        golden_tier = g.get("tier_boundary_kwh", 0)
        if script_tier != golden_tier:
            plan_issues.append(
                f"    tier_boundary_kwh: script={script_tier}  golden={golden_tier}"
            )

        # 7b. EC above tier
        script_ec_above = sp.get("ec_cents_above_tier", 0.0)
        golden_ec_above = g.get("energy_charge_cents_above_tier", 0.0)
        if abs(script_ec_above - golden_ec_above) > EC_TOL:
            plan_issues.append(
                f"    ec_cents_above_tier: script={script_ec_above:.4f}  golden={golden_ec_above:.4f}  "
                f"delta={script_ec_above - golden_ec_above:+.4f}¢"
            )

        # 8. Effective rates at all tiers
        script_rates = sp.get("rates_cents_per_kwh", {})
        golden_rates = g.get("rates_cents_per_kwh", {})
        rate_diffs = []
        for t in TIERS:
            sr = script_rates.get(str(t)) or script_rates.get(t)
            gr = golden_rates.get(str(t)) or golden_rates.get(t)
            if sr is not None and gr is not None and abs(sr - gr) > RATE_TOL:
                rate_diffs.append(f"{t:,}kWh: script={sr:.2f}  golden={gr:.2f}  Δ={sr-gr:+.2f}¢")
        if rate_diffs:
            plan_issues.append("    rate discrepancies (¢/kWh):\n" +
                               "\n".join(f"      {d}" for d in rate_diffs))

        if plan_issues:
            issues.append((abs(
                (script_rates.get(str(COMPARE_TIER)) or 0) -
                (golden_rates.get(str(COMPARE_TIER)) or 0)
            ), label, plan_issues, conf))
        else:
            matched += 1

    # Sort by severity (largest delta at compare tier first)
    issues.sort(reverse=True, key=lambda x: x[0])

    # ── Print report ─────────────────────────────────────────────────────────────
    print("=" * 80)
    print("  GOLDEN FILE COMPARISON REPORT")
    print(f"  Script plans:      {len(script_plans)}")
    print(f"  Golden plans:      {len(golden_plans)}")
    print(f"  Human reviewed:    {len(human_reviewed)} (overrides applied)")
    print(f"  Low-confidence:    {len(low_confidence)} (skipped)")
    print(f"  Matched (no diff): {matched}")
    print(f"  Issues found:      {len(issues)}")
    print("=" * 80)

    if only_in_script:
        print(f"\nIn script only ({len(only_in_script)}):")
        for s in only_in_script: print(s)

    if only_in_golden:
        print(f"\nIn golden only ({len(only_in_golden)}):")
        for s in only_in_golden: print(s)

    if issues:
        print(f"\n{'─'*80}")
        print(f"  DISCREPANCIES (sorted by Δ at {COMPARE_TIER:,} kWh, largest first)")
        print(f"{'─'*80}")
        for delta, label, plan_issues, conf in issues:
            print(f"\n[Δ{delta:.2f}¢ @{COMPARE_TIER}kWh | conf={conf}]  {label}")
            for msg in plan_issues:
                print(msg)

    print(f"\n{'─'*80}")
    print("  SUMMARY BY CATEGORY")
    print(f"{'─'*80}")

    rate_issues    = [x for x in issues if any("rate discrepanc" in m for pi in x[2] for m in [pi])]
    credit_issues  = [x for x in issues if any("bill_credit" in m for pi in x[2] for m in [pi])]
    ec_issues      = [x for x in issues if any("energy_charge_cents" in m for pi in x[2] for m in [pi])]
    thresh_issues  = [x for x in issues if any("energy_threshold" in m for pi in x[2] for m in [pi])]
    bundled_issues = [x for x in issues if any("tdu_bundled" in m for pi in x[2] for m in [pi])]
    src_issues     = [x for x in issues if any("src:" in m for pi in x[2] for m in [pi])]
    tier_issues    = [x for x in issues if any("tier_boundary_kwh" in m for pi in x[2] for m in [pi])]
    ec_above_issues= [x for x in issues if any("ec_cents_above_tier" in m for pi in x[2] for m in [pi])]

    print(f"  Rate differences   (>{RATE_TOL}¢ at any tier): {len(rate_issues)}")
    print(f"  Energy charge diff (>{EC_TOL}¢):               {len(ec_issues)}")
    print(f"  Bill credit diff:                            {len(credit_issues)}")
    print(f"  Threshold mismatch:                          {len(thresh_issues)}")
    print(f"  Tier boundary mismatch:                      {len(tier_issues)}")
    print(f"  EC above tier mismatch:                      {len(ec_above_issues)}")
    print(f"  TDU bundled mismatch:                        {len(bundled_issues)}")
    print(f"  Source degraded to API:                      {len(src_issues)}")

    return issues, matched, low_confidence, human_reviewed


if __name__ == "__main__":
    compare()
    sys.exit(0)
