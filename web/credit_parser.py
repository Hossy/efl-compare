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
credit_parser.py — LLM-based bill credit and rate extraction from EFL text.

Uses an OpenAI-compatible Chat Completions endpoint (configured via .env — see
llm_backend.py and .env.example) with response_format=json_schema so every
response is constrained to the exact schema. Point it at any OpenAI-compatible
server (llama.cpp server, vLLM, Ollama, LM Studio, or the OpenAI API).
"""

import json
import re
import sys

import llm_backend

# JSON schema that response_format=json_schema enforces on every response
_CREDITS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "amount":              {"type": "number"},
            "threshold_kwh":       {"type": "integer"},
            "cumulative":          {"type": "boolean"},
            "requires_enrollment": {"type": "boolean"},
        },
        "required": ["amount", "threshold_kwh", "cumulative", "requires_enrollment"],
        "additionalProperties": False,
    },
}

# Extended schema — adds tiered rate fields.
# tier_boundary_kwh: 0 = no tiering (single rate applies to all kWh)
# energy_charge_cents_above_tier: the upper-tier rate in ¢/kWh (0 if no tiering)
_RATES_SCHEMA_EXT = {
    "type": "object",
    "properties": {
        "energy_charge_cents":            {"type": "number"},
        "base_charge_dollars":            {"type": "number"},
        "tdu_bundled":                    {"type": "boolean"},
        "energy_charge_threshold_kwh":    {"type": "integer"},
        "one_time_fee_dollars":           {"type": "number"},
        "tier_boundary_kwh":              {"type": "integer"},
        "energy_charge_cents_above_tier": {"type": "number"},
    },
    "required": [
        "energy_charge_cents", "base_charge_dollars", "tdu_bundled",
        "energy_charge_threshold_kwh", "one_time_fee_dollars",
        "tier_boundary_kwh", "energy_charge_cents_above_tier",
    ],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_llm           = None
_cache:        dict[str, dict] = {}
_two_line_mode = False   # set True by efl_compare to enable status-line cursor updates
_device_label  = "LLM"   # updated in _load_model; used by _update_model_status
_total_prompt_tokens     = 0
_total_completion_tokens = 0
_total_llm_calls         = 0


def _update_model_status(msg: str) -> None:
    if _two_line_mode:
        import sys as _sys
        # \0337 saves cursor, \033[1A moves to status line, \0338 restores cursor.
        # Avoids column drift from \n-based repositioning across multiple calls.
        _sys.stdout.write(f"\0337\033[1A\r\033[2K  {msg}\0338")
        _sys.stdout.flush()

# Truncation tracking — read by efl_compare_v2 to surface SEVERE WARNING flags
last_section_was_truncated  = False
last_section_char_count     = 0

# Token budget for section text (leaves room for system prompt + few-shot + completion)
_SECTION_MAX_CHARS = 12_000  # ~4000 tokens; typical section is 300–800 chars

_rates_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load_model():
    """Return the OpenAI-compatible chat backend, constructed once.

    Configuration comes entirely from the environment / .env (see llm_backend
    and .env.example). Raises llm_backend.LLMNotConfigured with actionable text
    if no endpoint is set.
    """
    global _llm, _device_label
    if _llm is not None:
        return _llm

    _device_label = llm_backend.label()

    def _status(msg):
        if _two_line_mode:
            import sys as _sys
            _sys.stdout.write(f"\0337\033[1A\r\033[2K  {msg}\0338")
            _sys.stdout.flush()
        else:
            print(f"  {msg}", flush=True)

    _status(f"Connecting to LLM ({_device_label})...")
    _llm = llm_backend.ChatBackend()
    _status(f"Model: {_device_label} ready")
    return _llm


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def parse_credits(fees_credits_text: str) -> list[dict]:
    """
    Parse bill credit(s) from a [Fees/Credits] CSV field.
    Unchanged from production credit_parser.py.
    """
    text = (fees_credits_text or "").strip()
    if not text:
        return []

    if text in _cache:
        return _cache[text]

    llm = _load_model()

    messages = [
        {
            "role": "system",
            "content": (
                "You are a precise structured data extractor for Texas electricity plans. "
                "Extract bill credits from plan description text. "
                "Respond ONLY with a valid JSON array — no explanation, no markdown. "
                "Rules: "
                "(1) Return objects with keys: amount (number), threshold_kwh (integer), cumulative (boolean), requires_enrollment (boolean). "
                "(2) cumulative=true only when described as 'additional' or stacking on a lower-tier credit. "
                "(3) requires_enrollment=true when the credit requires the customer to be enrolled in a program (auto-pay, paperless billing, smart thermostat, etc.). "
                "    A credit can have BOTH requires_enrollment=true AND threshold_kwh>0 if it requires enrollment AND a usage level (e.g. 'auto-pay customers get $5 when usage >= 1000 kWh'). "
                "    Plain usage-based credits with no enrollment requirement use requires_enrollment=false regardless of their kWh threshold. "
                "(4) Fees, minimum usage charges, and promotional text are NOT credits — return [] for those. "
                "(5) Extract credits even when buried inside promotional or plan description text. "
                "(6) Normalize off-by-one thresholds: 'exceeds 999 kWh' = threshold_kwh 1000."
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract: 'A 12-month fixed-rate plan with no base charges and 100% "
                "renewable energy. Get a $125 usage credit each month your usage is "
                "1,000 kWh or more.'"
            ),
        },
        {
            "role": "assistant",
            "content": '[{"amount": 125.0, "threshold_kwh": 1000, "cumulative": false, "requires_enrollment": false}]',
        },
        {
            "role": "user",
            "content": (
                "Extract: 'A bill credit of $30 will be applied for each billing cycle "
                "in which usage is 800 kWh or more'"
            ),
        },
        {
            "role": "assistant",
            "content": '[{"amount": 30.0, "threshold_kwh": 800, "cumulative": false, "requires_enrollment": false}]',
        },
        {
            "role": "user",
            "content": (
                "Extract: 'Pay the same amount every month whenever your usage is "
                "below 1,000 kWh.'"
            ),
        },
        {
            "role": "assistant",
            "content": "[]",
        },
        {
            "role": "user",
            "content": (
                "Extract: 'Constellation will automatically apply a $35 bill credit "
                "to your invoice for each billing cycle where usage is at least 1000 kWh. "
                "An additional $15 bill credit will be applied when usage reaches 2000 kWh'"
            ),
        },
        {
            "role": "assistant",
            "content": (
                '[{"amount": 35.0, "threshold_kwh": 1000, "cumulative": false, "requires_enrollment": false}, '
                '{"amount": 15.0, "threshold_kwh": 2000, "cumulative": true, "requires_enrollment": false}]'
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract: 'A $125 usage credit will be applied when usage is at least 1000 kWh. "
                "Auto Pay & Paperless Credit: $5.00 per month when enrolled in auto-pay.'"
            ),
        },
        {
            "role": "assistant",
            "content": (
                '[{"amount": 125.0, "threshold_kwh": 1000, "cumulative": false, "requires_enrollment": false}, '
                '{"amount": 5.0, "threshold_kwh": 0, "cumulative": false, "requires_enrollment": true}]'
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract: 'Auto Pay & Paperless Credit: $5.00 per month\n"
                "Usage Credit for 1,000 kWh or more: $125.00 per month\n"
                "A Usage Credit of $125.00 will only be included for each billing cycle "
                "if your usage on this plan is equal to or greater than 1,000 kWh.'"
            ),
        },
        {
            "role": "assistant",
            "content": (
                '[{"amount": 5.0, "threshold_kwh": 0, "cumulative": false, "requires_enrollment": true}, '
                '{"amount": 125.0, "threshold_kwh": 1000, "cumulative": false, "requires_enrollment": false}]'
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract: 'Auto-pay customers receive a $5 credit each billing "
                "cycle where usage is 1,000 kWh or more.'"
            ),
        },
        {
            "role": "assistant",
            "content": '[{"amount": 5.0, "threshold_kwh": 1000, "cumulative": false, "requires_enrollment": true}]',
        },
        {
            "role": "user",
            "content": f"Extract: '{text}'",
        },
    ]

    _update_model_status(f"Model: {_device_label} busy")
    response = llm.create_chat_completion(
        messages=messages,
        temperature=0.0,
        max_tokens=256,
        response_schema=_CREDITS_SCHEMA,
        schema_name="bill_credits",
    )
    _update_model_status(f"Model: {_device_label} ready")

    global _total_prompt_tokens, _total_completion_tokens, _total_llm_calls
    if "usage" in response:
        _total_prompt_tokens     += response["usage"].get("prompt_tokens", 0)
        _total_completion_tokens += response["usage"].get("completion_tokens", 0)
    _total_llm_calls += 1

    raw = response["choices"][0]["message"]["content"].strip()

    try:
        parsed = json.loads(raw)
        result = [
            {
                "amount":              float(c["amount"]),
                "threshold_kwh":       int(c["threshold_kwh"]),
                "cumulative":          bool(c.get("cumulative", False)),
                "requires_enrollment": bool(c.get("requires_enrollment", False)),
            }
            for c in parsed
            if isinstance(c, dict)
            and "amount"        in c
            and "threshold_kwh" in c
        ]
    except (json.JSONDecodeError, ValueError, TypeError):
        result = []

    _cache[text] = result
    return result


def parse_credits_from_efl_text(efl_text: str) -> list[dict]:
    """
    Parse bill credits from raw EFL PDF text or an extracted Electricity Price section.

    Called only when EFL regex and CSV LLM disagree — applies the same LLM
    parsing to the legal document so we honour the EFL as the authoritative source.
    In efl_compare_v2, pass efl["electricity_price_section"] for higher accuracy.
    """
    if not efl_text or not efl_text.strip():
        return []

    relevant_lines = [
        line for line in efl_text.splitlines()
        if re.search(r"credit|usage\s+charge", line, re.I)
        and not re.search(
            r"disconnect|insufficient|late\s+payment|non.recurring|deposit",
            line, re.I,
        )
    ]

    if not relevant_lines:
        return []

    excerpt = "\n".join(relevant_lines[:20])
    return parse_credits(excerpt)


# ---------------------------------------------------------------------------
# EFL structural rate extraction
# ---------------------------------------------------------------------------

def _extract_price_section(text: str) -> str:
    """
    Extract the Electricity Price section from raw EFL text.

    Starts at the Average Monthly Use/Price header and ends at the Other Key
    Terms / Disclosure Chart boundary.  No hard character cap — the caller
    handles truncation after a token pre-check so the LLM sees the full section
    (typical: 300–800 chars; pathological maximum: ~10 k chars).
    """
    start_m = re.search(r'average\s+(?:monthly|price)', text, re.I)
    pos = start_m.start() if start_m else 0
    tail = text[pos:]
    end_m = re.search(r'other\s+key\s+(?:terms|info)|type\s+of\s+product|contract\s+term', tail, re.I)
    section = tail[:end_m.start()] if end_m else tail
    return section.strip()


def parse_rates_from_efl_text(section_or_text: str) -> dict | None:
    """
    Extract full pricing structure from an EFL Electricity Price section.

    section_or_text: Either a pre-extracted Electricity Price section (preferred —
        pass efl["electricity_price_section"]) OR full EFL text. If "Other Key Terms"
        is detected in the input, the section is re-extracted automatically; otherwise
        the input is used directly as the section.

    Returns a dict with keys matching _RATES_SCHEMA_EXT, or None on failure.
    Tiered rate fields: tier_boundary_kwh (0 = no tiering) and
    energy_charge_cents_above_tier (0.0 = no upper-tier rate).
    """
    global last_section_was_truncated, last_section_char_count

    if not section_or_text or not section_or_text.strip():
        last_section_was_truncated = False
        last_section_char_count    = 0
        return None

    # Detect whether caller passed full EFL text or already-extracted section
    if re.search(r'other\s+key\s+terms', section_or_text, re.I):
        section = _extract_price_section(section_or_text)
    else:
        section = section_or_text

    last_section_char_count = len(section)

    # Token pre-check: apply principled truncation if section exceeds budget
    if len(section) > _SECTION_MAX_CHARS:
        keep_start = _SECTION_MAX_CHARS * 2 // 3  # 2/3 for pricing table at top
        keep_end   = _SECTION_MAX_CHARS - keep_start  # 1/3 for structural clauses at bottom
        section = (
            section[:keep_start] +
            "\n[... TRUNCATED: EFL section exceeded token budget ...]\n" +
            section[-keep_end:]
        )
        last_section_was_truncated = True
        print(
            f"  [SEVERE WARNING] EFL Electricity Price section truncated: "
            f"{last_section_char_count} chars → {len(section)} chars "
            f"(est. {last_section_char_count // 3} tokens > {_SECTION_MAX_CHARS // 3} budget). "
            f"Rates extracted under truncation have low confidence.",
            file=sys.stderr,
        )
    else:
        last_section_was_truncated = False

    est_toks  = len(section) // 3
    cache_key = section
    if cache_key in _rates_cache:
        return _rates_cache[cache_key]

    llm = _load_model()

    messages = [
        {
            "role": "system",
            "content": (
                "You extract electricity pricing structure from Texas EFL (Electricity Facts Label) text. "
                "Respond ONLY with valid JSON — no explanation, no markdown.\n\n"
                "Fields:\n"
                "  energy_charge_cents — REP energy charge in ¢/kWh. For tiered plans, use the LOWER "
                "(first) tier rate. For time-of-use plans use the standard (non-free) rate. Exclude TDU/delivery charges.\n"
                "  base_charge_dollars — REP fixed monthly charge in $ (0 if none).\n"
                "  tdu_bundled — true if TDU delivery is already included in the stated rates "
                "(TDU listed as $0, or EFL says delivery is bundled into base/energy rate).\n"
                "  energy_charge_threshold_kwh — kWh threshold above which the ENERGY CHARGE applies "
                "(0 if the energy charge applies from the first kWh). "
                "IMPORTANT: this is NOT a bill credit threshold. If the EFL describes a bill credit "
                "that applies above X kWh, that X belongs in the bill_credits array, not here. "
                "Set energy_charge_threshold_kwh=0 whenever bill credits are present unless the "
                "EFL explicitly states the energy charge itself is zero or waived below a threshold.\n"
                "  one_time_fee_dollars — the TOTAL of any one-time or setup fees the EFL states are "
                "amortised into average prices. Return the RAW TOTAL (e.g. '$49.99 setup fee, 1/12 "
                "included in average prices' → 49.99). Do NOT divide by 12. 0.0 if none.\n"
                "  tier_boundary_kwh — if the plan has TWO different energy charge rates, the kWh level "
                "where the rate changes (e.g. 'first 1,200 kWh at rate A, above at rate B' → 1200). "
                "0 if only one rate applies to all usage. "
                "Tabular range format recognition: '0 - 1,200 kWh: 12¢' paired with '> 1,200 kWh: 19.6¢' "
                "always means tier_boundary_kwh=1200, energy_charge_threshold_kwh=0. "
                "IMPORTANT: tier_boundary_kwh and energy_charge_threshold_kwh are mutually exclusive — "
                "NEVER set both to a non-zero value. If tier_boundary_kwh > 0, always set "
                "energy_charge_threshold_kwh = 0. A tiered plan is not a threshold plan.\n"
                "  energy_charge_cents_above_tier — the energy charge in ¢/kWh for usage ABOVE "
                "tier_boundary_kwh (0 if no tiering). "
                "IMPORTANT: if tier_boundary_kwh > 0, this field MUST also be > 0. A commercial "
                "electricity plan will never give energy away for free above a usage threshold — "
                "that is not a real pricing structure. If you cannot find an explicit above-tier "
                "rate in the EFL, set tier_boundary_kwh = 0 instead."
            ),
        },
        # Example 1: standard unbundled plan (no tiering)
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use: 500 kWh 1000 kWh 2000 kWh\n"
                "Average price per kWh: 13.1¢ 13.1¢ 13.2¢\n"
                "Energy Charge: 7.124¢ per kWh\n"
                "Base Charge: $0 per month\n"
                "Oncor Delivery Charge: 6.1196¢ per kWh and $4.06 per month'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":7.124,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 2: TDU bundled + threshold energy charge (Texans Choice Texas Instant)
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use: 500 kWh 1000 kWh 2000 kWh\n"
                "Average Price per kWh: 27.0¢ 13.5¢ 13.8¢\n"
                "*Energy Charge 14.00¢ per kWh\n"
                "Base Charge $135.00 per bill month\n"
                "TDU Delivery Charge 0.00000¢ per kWh\n"
                "TDU Delivery Charge $0.00 per bill month\n"
                "*The Energy Charge is only applicable to usage above 1000 kWh in a billing cycle.\n"
                "All delivery charges from your TDU are bundled into your Monthly Base Charge and per kWh rate.'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":14.0,"base_charge_dollars":135.0,"tdu_bundled":true,"energy_charge_threshold_kwh":1000,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 3: amortised one-time setup fee (Tara GoodBundle)
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use: 500 kWh 1000 kWh 2000 kWh\n"
                "Average price per kWh: 13.4¢ 12.5¢ 12.1¢\n"
                "Energy Charge: 5.6¢/kWh\n"
                "Pass-Through TDSP Distribution Charge: 6.1196¢/kWh\n"
                "Pass-Through TDSP Customer Charge: $4.06 per month\n"
                "One-time GoodBundle set up and carbon offset purchase: $49.99. "
                "For purposes of this EFL, 1/12 of this set up cost is included in the average prices above.'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":5.6,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":49.99,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 4: time-of-use free-hour plan (SoFed Free Energy Lunch)
        {
            "role": "user",
            "content": (
                "Extract: 'Free Energy Lunch Hour Rate: $0.0000 per kilo-Watt hour (12PM-1PM daily)\n"
                "Fixed Energy Rate: $0.070225 per kilo-Watt hour (all other hours)\n"
                "Monthly Base Charge: $0.00 per monthly bill cycle\n"
                "TDU charges are passed through to the customer without markup.'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":7.0225,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 5: TDU bundled without threshold (TriEagle)
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
                "Average Price per kWh 16.3¢ 15.8¢ 15.5¢\n"
                "Base Charge: Per Month ($) $4.95\n"
                "Energy Charge: Per kWh (¢) All kWh 15.3000¢\n"
                "Average prices per kWh listed above do not include facility relocation fees.\n"
                "The price applied in the first billing cycle may be different from the price "
                "in this EFL if there are changes in TDU charges.'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":15.3,"base_charge_dollars":4.95,"tdu_bundled":true,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 6: tiered energy rate — clean prose format
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
                "Average Price per kWh 13.7¢ 12.0¢ 16.0¢\n"
                "Energy Charge: 12.0¢ per kWh for the first 1,200 kWh per billing cycle\n"
                "Energy Charge: 19.6¢ per kWh for usage above 1,200 kWh per billing cycle\n"
                "Base Charge: $0.00 per month\n"
                "Oncor Delivery Charge: 6.1196¢ per kWh + $4.06 per month'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":12.0,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":1200,"energy_charge_cents_above_tier":19.6}',
        },
        # Example 9: tiered energy rate — tabular range format with injected section label
        # and a bill credit. Real-world noisy layout. threshold=0 even with two kWh values present.
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
                "Average Price per kWh 20.9¢ 14.5¢ 19.4¢\n"
                "Base Charge: Per Month ($) $9.95\n"
                "Energy Charge: Per kWh (¢)\n"
                "   0 - 1200 kWh   12.0000¢\n"
                "Electricity\n"
                "   > 1200 kWh   19.6000¢Price\n"
                "TDU Delivery Charges: Per Month ($) **\n"
                "A bill credit of $50 will be applied for each billing cycle in which usage is 800 kWh or more.'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":12.0,"base_charge_dollars":9.95,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":1200,"energy_charge_cents_above_tier":19.6}',
        },
        # Example 7: bill-credit plan with labeled credit row — kWh in the credit line
        # is NOT an energy charge threshold and NOT a tier boundary
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
                "Average price per kWh: 23.9¢ 11.0¢ 17.1¢\n"
                "Energy Charge: 17.0¢ per kWh\n"
                "Base Charge: $0.00 per month\n"
                "Bill Credit: $125.00 per billing cycle if usage is 1,000 kWh or more\n"
                "Pass-Through TDSP Distribution Charge: 6.1196¢/kWh\n"
                "Pass-Through TDSP Customer Charge: $4.06 per month'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":17.0,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 8: bill-credit plan with prose usage-credit description — the
        # "above or equal to 1,000 kWh" clause belongs to the credit, not the energy charge
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use 500 kWh 1000 kWh 2000 kWh\n"
                "Average price per kWh: 21.3¢ 8.4¢ 14.4¢\n"
                "A Usage Credit of $125.00 will be included for each billing cycle when "
                "your usage on this plan is above or equal to 1,000 kWh. There is no "
                "Usage Credit for a billing cycle when usage is below 1,000 kWh.\n"
                "Base Charge: $0.00 per billing cycle\n"
                "Energy Charge: 14.3341¢ per kWh\n"
                "Oncor Delivery Charges are passed through without markup.'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":14.3341,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 10: "Usage Credit for N kWh or more" format (Energy Texas / RHYTHM style)
        # with an additional autopay enrollment credit. The V-shape averages come from
        # credits, NOT from tiered rates — tier_boundary_kwh must be 0.
        {
            "role": "user",
            "content": (
                "Extract: 'Average monthly use: 500 kWh 1000 kWh 2000 kWh\n"
                "Average price per kWh: 20.6¢ 8.2¢ 14.5¢\n"
                "Base Charge: $0 per month\n"
                "Energy Charge: 14.656¢ per kWh\n"
                "Auto Pay & Paperless Credit: $5.00 per month\n"
                "Usage Credit for 1,000 kWh or more: $125.00 per month\n"
                "Oncor Delivery Charge: 6.1196¢ per kWh and $4.06 per month'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":14.656,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 11: "exceeds 999 kWh" conditional-sentence bill credit (Budget Power style)
        # "999 kWh" normalises to threshold 1000. Conditional sentence structure, no tier.
        {
            "role": "user",
            "content": (
                "Extract: 'Average Monthly Use 500 kWh 1,000 kWh 2,000 kWh\n"
                "Average price per kWh: 20.2¢ 7.3¢ 13.4¢\n"
                "Base Charge: $0 per billing cycle\n"
                "Fixed Energy Charge: 13.31¢ per kWh\n"
                "Oncor Delivery Charges: 6.1196¢ per kWh\n"
                "Oncor Monthly Charges: $4.06 per billing cycle\n"
                "If your usage exceeds 999 kWh in a billing cycle you will receive a bill credit of $125'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":13.31,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Example 12: "Minimum Usage Credit where usage >= N kWh" two-row table format (Octopus style)
        # Credit displayed as a conditional two-row table. No tier boundary.
        {
            "role": "user",
            "content": (
                "Extract: 'Average monthly use: 500 kWh 1000 kWh 2000 kWh\n"
                "Average price per kWh: 22.7¢ 9.8¢ 15.8¢\n"
                "Octopus Energy Charge: 15.7679¢ per kWh\n"
                "ONC Charge per kWh: 6.1196¢ per kWh\n"
                "ONC per meter Fee: $4.06 per month\n"
                "Base Charge: $0.00 per month\n"
                "Minimum Usage Credit: $125.00 per billing cycle where usage >= 1000 kWh\n"
                "                       $0.00 per billing cycle where usage < 1000 kWh'"
            ),
        },
        {
            "role": "assistant",
            "content": '{"energy_charge_cents":15.7679,"base_charge_dollars":0.0,"tdu_bundled":false,"energy_charge_threshold_kwh":0,"one_time_fee_dollars":0.0,"tier_boundary_kwh":0,"energy_charge_cents_above_tier":0.0}',
        },
        # Actual query
        {
            "role": "user",
            "content": f"Extract: '{section}'  [approx {est_toks} tokens]",
        },
    ]

    try:
        _update_model_status(f"Model: {_device_label} busy")
        response = llm.create_chat_completion(
            messages=messages,
            temperature=0.0,
            max_tokens=128,
            response_schema=_RATES_SCHEMA_EXT,
            schema_name="efl_rates",
        )
        _update_model_status(f"Model: {_device_label} ready")

        global _total_prompt_tokens, _total_completion_tokens, _total_llm_calls
        if "usage" in response:
            _total_prompt_tokens     += response["usage"].get("prompt_tokens", 0)
            _total_completion_tokens += response["usage"].get("completion_tokens", 0)
        _total_llm_calls += 1

        raw    = response["choices"][0]["message"]["content"].strip()
        parsed = json.loads(raw)
        ec = float(parsed["energy_charge_cents"])
        bc = float(parsed["base_charge_dollars"])
        if not (1.0 <= ec <= 50.0):          # sanity: real Texas REP rates are 1–50¢/kWh
            _rates_cache[cache_key] = None
            return None
        result = {
            "energy_charge_cents":            ec,
            "base_charge_dollars":            bc,
            "tdu_bundled":                    bool(parsed.get("tdu_bundled", False)),
            "energy_charge_threshold_kwh":    int(parsed.get("energy_charge_threshold_kwh", 0)),
            "one_time_fee_dollars":           float(parsed.get("one_time_fee_dollars", 0.0)),
            "tier_boundary_kwh":              int(parsed.get("tier_boundary_kwh", 0)),
            "energy_charge_cents_above_tier": float(parsed.get("energy_charge_cents_above_tier", 0.0)),
        }
        _rates_cache[cache_key] = result
        return result
    except Exception:
        _rates_cache[cache_key] = None
        return None
