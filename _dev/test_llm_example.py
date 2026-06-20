"""
test_llm_example.py — Empirically test whether adding a bill-credit few-shot
example to parse_rates_from_efl_text fixes the three [LLM BUG] warnings.

Runs each failing EFL section through:
  A. Current prompt (no bill-credit example)
  B. Prompt + new bill-credit example

Prints the diff for the two fields we care about:
  energy_charge_threshold_kwh  (should be 0)
  tier_boundary_kwh            (should be 0 for these plans)
"""

import fitz, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).parent.parent
CACHE = ROOT / "efls_cache"

# ── Section extraction (matches _extract_electricity_price_section in v2) ──
def extract_section(text):
    m1 = re.search(r'average\s+(?:monthly|price)', text, re.I)
    m2 = re.search(r'electricity\s+price', text, re.I)
    candidates = [m for m in (m1, m2) if m]
    if not candidates:
        return text
    start_m = min(candidates, key=lambda m: m.start())
    end_m = re.search(
        r'other\s+key\s+terms|disclosure\s+chart|key\s+terms\s+&',
        text[start_m.start():], re.I,
    )
    end = start_m.start() + end_m.start() if end_m else len(text)
    return text[start_m.start():end]

PLANS = [
    ("TXU Value Edge 12", "CHARIOT_ENERGY_Chariot_Choice_36_21502.pdf"),  # placeholder — replaced below
]

import pathlib
# Override with correct TXU file
PLANS = [("TXU Value Edge 12", "TXU_ENERGY_Value_Edge_12_33722.pdf")]

# ── New few-shot examples to test ──
# Example A: labeled bill-credit row (Tara / Amigo style)
EXAMPLE_A_USER = (
    "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
    "Average price per kWh: 23.9¢ 11.0¢ 17.1¢\n"
    "Energy Charge: 17.0¢ per kWh\n"
    "Base Charge: $0.00 per month\n"
    "Bill Credit: $125.00 per billing cycle if usage is 1,000 kWh or more\n"
    "Pass-Through TDSP Distribution Charge: 6.1196¢/kWh\n"
    "Pass-Through TDSP Customer Charge: $4.06 per month'"
)
EXAMPLE_A_ASST = (
    '{"energy_charge_cents":17.0,"base_charge_dollars":0.0,"tdu_bundled":false,'
    '"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,'
    '"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}'
)

# Example B: prose usage-credit description (Discount Power style)
EXAMPLE_B_USER = (
    "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
    "Average price per kWh: 21.3¢ 8.4¢ 14.4¢\n"
    "A Usage Credit of $125.00 will be included for each billing cycle when "
    "your usage on this plan is above or equal to 1,000 kWh. There is no "
    "Usage Credit for a billing cycle when usage is below 1,000 kWh.\n"
    "Base Charge: $0.00 per billing cycle\n"
    "Energy Charge: 14.3341¢ per kWh\n"
    "Oncor Delivery Charges are passed through without markup.'"
)
EXAMPLE_B_ASST = (
    '{"energy_charge_cents":14.3341,"base_charge_dollars":0.0,"tdu_bundled":false,'
    '"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,'
    '"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}'
)

def run_test():
    import credit_parser_v2 as cp

    # Clear cache so both runs actually call the LLM
    cp._rates_cache.clear()

    # Warm up: force model load before we try to patch _llm
    cp._load_model()

    sections = []
    for label, fname in PLANS:
        pdf = CACHE / fname
        doc = fitz.open(str(pdf))
        text = doc[0].get_text("text", sort=True)
        doc.close()
        sections.append((label, extract_section(text)))

    print("=" * 70)
    print("  EMPIRICAL TEST: bill-credit few-shot example")
    print("=" * 70)

    original_create = cp._llm.create_chat_completion

    def make_patched(extra_pairs):
        def patched(messages, **kwargs):
            inserts = []
            for u, a in extra_pairs:
                inserts += [{"role": "user", "content": u},
                             {"role": "assistant", "content": a}]
            new_msgs = messages[:-1] + inserts + [messages[-1]]
            return original_create(new_msgs, **kwargs)
        return patched

    for label, section in sections:
        print(f"\n-- {label} --")

        cp._rates_cache.clear()
        result_base = cp.parse_rates_from_efl_text(section)

        cp._rates_cache.clear()
        cp._llm.create_chat_completion = make_patched([(EXAMPLE_A_USER, EXAMPLE_A_ASST)])
        result_a = cp.parse_rates_from_efl_text(section)

        cp._rates_cache.clear()
        cp._llm.create_chat_completion = make_patched([
            (EXAMPLE_A_USER, EXAMPLE_A_ASST),
            (EXAMPLE_B_USER, EXAMPLE_B_ASST),
        ])
        result_ab = cp.parse_rates_from_efl_text(section)
        cp._llm.create_chat_completion = original_create

        for field in ("energy_charge_threshold_kwh", "tier_boundary_kwh"):
            vbase = result_base.get(field, "?") if result_base else "NONE"
            va    = result_a.get(field, "?")    if result_a    else "NONE"
            vab   = result_ab.get(field, "?")   if result_ab   else "NONE"
            def tag(v): return "OK" if v == 0 else "WRONG"
            print(f"  {field:40s}  base={vbase:>5}({tag(vbase)})  +A={va:>5}({tag(va)})  +A+B={vab:>5}({tag(vab)})")

    print()

if __name__ == "__main__":
    run_test()
