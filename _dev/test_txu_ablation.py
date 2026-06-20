"""
test_txu_ablation.py — Ablation test for TXU Value Edge 12.

Tests four variants of the input text passed to the structural LLM:
  1. sort=True text   (current approach)
  2. sort=True text   with bill credit line REMOVED
  3. blocks-filtered  (x0 >= 100)
  4. blocks-filtered  with bill credit line REMOVED

For each: reports energy_charge_threshold_kwh and tier_boundary_kwh.
If removing the bill credit line drives threshold=0 consistently,
that confirms it is the cause of the LLM confusion.
"""

import fitz, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent))

ROOT  = Path(__file__).parent.parent
CACHE = ROOT / "efls_cache"
PLANS_TO_TEST = [
    ("TXU Value Edge 12",          CACHE / "TXU_ENERGY_Value_Edge_12_33722.pdf"),
    ("Energy Texas Lone Saver 12", CACHE / "Energy_Texas_The_Lone_Saver_Plus_12_36365.pdf"),
    ("RHYTHM Rhythm Max Saver 12", CACHE / "RHYTHM_Rhythm_Max_Saver_12_36355.pdf"),
]
PDF   = CACHE / "TXU_ENERGY_Value_Edge_12_33722.pdf"  # kept for compat
X0    = 100

CREDIT_LINE_RE = re.compile(
    r".*bill\s+credit.*\$50.*800\s*kwh.*\n?",
    re.I
)

def text_sort(path):
    doc = fitz.open(str(path))
    t = "\n".join(p.get_text("text", sort=True) for p in doc)
    doc.close()
    return t

def text_blocks(path, threshold=X0):
    doc = fitz.open(str(path))
    lines = []
    for page in doc:
        kept = [(b[1], b[0], b[4]) for b in page.get_text("blocks")
                if b[6] == 0 and b[0] >= threshold]
        kept.sort()
        lines.extend(t.strip() for _, _, t in kept)
    doc.close()
    return "\n".join(lines)

def strip_credit(text):
    return CREDIT_LINE_RE.sub("", text)

def run():
    import credit_parser_v2 as cp
    print("Loading model...", flush=True)
    cp._load_model()
    print("Model ready.\n")

    ts   = text_sort(PDF)
    tb   = text_blocks(PDF)
    ts_n = strip_credit(ts)
    tb_n = strip_credit(tb)

    print("Bill credit line present:")
    print("  sort=True:       ", "YES" if "bill credit" in ts.lower() else "NO")
    print("  blocks:          ", "YES" if "bill credit" in tb.lower() else "NO")
    print("  sort (stripped): ", "YES" if "bill credit" in ts_n.lower() else "NO")
    print("  blocks (stripped):", "YES" if "bill credit" in tb_n.lower() else "NO")
    print()

    N = 5  # runs per variant

    variants = [
        ("sort=True",          ts),
        ("sort=True  -credit", ts_n),
        ("blocks",             tb),
        ("blocks    -credit",  tb_n),
    ]

    print(f"Running each variant {N} times...\n")
    print(f"{'Variant':<22}  {'thresh=0':>10}  {'tier=1200':>10}  {'both OK':>8}")
    print("-" * 60)

    for label, text in variants:
        thresh_ok = 0
        tier_ok   = 0
        both_ok   = 0
        results   = []
        for _ in range(N):
            cp._rates_cache.clear()
            r = cp.parse_rates_from_efl_text(text)
            if r:
                t = r.get("energy_charge_threshold_kwh", -1)
                b = r.get("tier_boundary_kwh", -1)
                if t == 0:   thresh_ok += 1
                if b == 1200: tier_ok  += 1
                if t == 0 and b == 1200: both_ok += 1
                results.append(f"thresh={t} tier={b}")
            else:
                results.append("None")
        print(f"{label:<22}  {thresh_ok}/{N} OK    {tier_ok}/{N} OK   {both_ok}/{N} OK")
        for i, res in enumerate(results, 1):
            print(f"  run {i}: {res}")

    print()

if __name__ == "__main__":
    run()
