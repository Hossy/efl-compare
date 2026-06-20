# EFL Electricity Plan Comparator

Fetches all fixed-rate Oncor electricity plans from [powertochoose.org](https://powertochoose.org), downloads and parses each provider's Electricity Facts Label (EFL), and produces an interactive HTML comparison table showing **actual bill costs at your real usage levels** — not just the standard 500/1,000/2,000 kWh tiers disclosed on the EFL.

Configured for the **Oncor service area**. Set your zip code via `--zip` or the `EFL_ZIP` environment variable.

---

## What It Does

1. **Fetches plans** from powertochoose.org via CSV export — all Oncor fixed-rate English plans (TimeOfUse and prepaid excluded)
2. **Downloads EFL PDFs** in parallel (10 at a time), cached locally with HTTP HEAD freshness checking
3. **Parses rate components** from each EFL using PyMuPDF with spatial text sorting:
   - Regex extraction for energy charge, base charge, and bill credits
   - Local LLM (Qwen2.5-7B) for structural analysis: threshold energy charges, tiered rates, bundled TDU, amortised fees
   - 2-attempt download retry (45s + 30s) before Playwright browser fallback
4. **Cross-checks bill credits** between the EFL and PUCT's CSV data using the LLM; escalates disagreements to EFL re-parse
5. **Calculates effective rates** at the standard EFL tiers (500/1,000/2,000 kWh) plus any personal profile tiers you configure via `EFL_TIERS` or `--tiers`
6. **Generates an interactive HTML table** (`plans_latest.html`) with:
   - **Dark/light theme toggle** (defaults to dark)
   - **Top 3 Picks cards** — best plan from each of the 3 cheapest term groups, shown above the table
   - Plans grouped by contract term (longest first), collapsible
   - ▼/★/▶ group header cycles: all plans → best only → hidden
   - ❤ favorite toggle per row
   - ★ marks cheapest plan in each group
   - Color-coded source badges: `[EFL]` green / `[LLM]` amber / `[API]` red
   - ¢ badge for bill-credit plans (advertised rate only valid near credit threshold)
   - ℹ badge with fee/credit details on hover
   - ⚠ badge for plans with one-time setup fees
   - **vs best longer column** — color-coded delta (green/amber/red) at your compare tier, with styled hover tooltip showing which plan is being compared and its rate
   - Plan name links open the EFL PDF in a new tab

---

## Requirements

- **Python 3.10+** (tested on 3.12)
- **Windows 10+** with NVIDIA GPU recommended (RTX 3060 12 GB or similar)
- **Internet access** for plan fetching and EFL downloads
- ~**5 GB disk space** for the LLM model

---

## Installation

### 1. Clone / copy the script files

Place `efl_compare.py`, `credit_parser.py`, and `requirements.txt` in the same directory.

### 2. Install Python dependencies

```
py -3.12 -m pip install -r requirements.txt
```

### 3. Install llama-cpp-python

**GPU (recommended — NVIDIA CUDA 12.x):**
```
py -3.12 -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

**CPU only:**
```
py -3.12 -m pip install llama-cpp-python
```

> The script auto-detects GPU presence at runtime via `nvidia-smi`. If you install the GPU build on a CPU-only machine it falls back gracefully.

### 4. Install Playwright browser (one-time, ~300 MB)

```
py -3.12 -m playwright install chromium
```

Enables fetching EFLs from providers that use JavaScript-rendered pages (Octopus Energy, TriEagle).

### 5. First run — model download (~4.7 GB, one-time)

On the first run that triggers LLM parsing, the script automatically downloads `Qwen2.5-7B-Instruct-Q4_K_M.gguf` into a `models/` subdirectory. No action needed — it shows a progress bar.

---

## Usage

```
py -3.12 efl_compare.py --zip ZIP [options]
```

ZIP code and personal tiers can be set via environment variables so you don't have to pass them on every run:

```
# Windows (PowerShell)
$env:EFL_ZIP = "YOUR_ZIP"
$env:EFL_TIERS = "LOW,MID,HIGH"
py -3.12 efl_compare.py

# macOS / Linux
EFL_ZIP=YOUR_ZIP EFL_TIERS=LOW,MID,HIGH py3 efl_compare.py
```

### Options

**Search**

| Flag | Default | Description |
|---|---|---|
| `--zip ZIP` | `EFL_ZIP` | Zip code to search. Required if `EFL_ZIP` is not set. |

**Usage tiers**

| Flag | Default | Description |
|---|---|---|
| `--tiers A,B,...` | `EFL_TIERS` | Personal usage tiers (kWh) appended to the standard 500/1,000/2,000 EFL tiers. Omit to use standard tiers only. |
| `--no-tiers` | off | Use only the 3 standard EFL tiers (ignores `--tiers` and `EFL_TIERS`) |
| `--compare-tier N` | median of `--tiers` | kWh tier used for sort order and "vs best longer" delta column |

**Output**

| Flag | Default | Description |
|---|---|---|
| `--html PATH` | `plans_latest.html` | HTML output path |
| `--no-html` | off | Skip HTML generation |
| `--text-table` | off | Print comparison table to stdout; includes Top Picks summary at the bottom |
| `--json [PATH]` | off | Write full results to JSON |
| `--timestamped` | off | Also save a timestamped copy alongside each output file (e.g. `plans_20260619_143022.html`) |

**Rate calculation**

| Flag | Default | Description |
|---|---|---|
| `--no-enrollment-credits` | off | Exclude enrollment-based credits (auto-pay, paperless, etc.) from rates |

**Cache / performance**

| Flag | Default | Description |
|---|---|---|
| `--no-llm` | off | Skip all LLM calls — development/diagnostic mode, significantly degraded accuracy |
| `--cache-ttl-hours N` | `12` | Trust cached EFLs verified within the last N hours without a HEAD check; set to `0` to always check |
| `--no-cache-check` | off | Skip all HTTP HEAD freshness checks; use cached EFLs as-is |
| `--clear-cache` | off | Delete all cached EFL files and re-download everything fresh |

### Examples

**Standard run (with env vars set):**
```
py -3.12 efl_compare.py
```

**Standard run (without env vars):**
```
py -3.12 efl_compare.py --zip YOUR_ZIP --tiers LOW,MID,HIGH
```

**Save JSON output alongside HTML:**
```
py -3.12 efl_compare.py --json plans_latest.json
```

**Custom usage profile:**
```
py -3.12 efl_compare.py --tiers 3000,4500,6000
```

**Quick diagnostic run (no model load, ~20 seconds, less accurate):**
```
py -3.12 efl_compare.py --no-llm
```

**Rates without enrollment credits:**
```
py -3.12 efl_compare.py --no-enrollment-credits
```

---

## Understanding the Output

### HTML Table

Open `plans_latest.html` in any browser. Fully self-contained — no external dependencies.

**Theme:** Defaults to dark mode. Click the **☀ Light** button (top right, scrolls with page) to toggle.

**Top Picks cards:** Three cards above the table show the cheapest plan from each of the top 3 term groups at your compare tier.

**Group header controls:**
- Click a group header to cycle: ▼ (all plans) → ★ (best + favorites) → ▶ (hidden) → ▼
- Click the ▼ in the top-left header cell to apply the same state to all groups

**Row badges (Flags column, hover for details):**
- `[EFL]` — rates from the legal EFL document (most accurate)
- `[LLM]` — rates extracted by local AI from the EFL text (high accuracy)
- `[API]` — rates estimated from PUCT CSV price data (least accurate)
- `¢` — bill-credit plan: advertised rate only valid near the credit threshold kWh
- `ℹ` — hover to see the fee/credit description from PUCT's data
- `⚠ $X` — one-time setup fee (not included in the displayed rates)

**vs best longer column:** Color-coded rate delta vs the best plan with a strictly longer contract, at your compare tier. Hover anywhere in the cell for a styled tooltip showing which plan is being compared and its rate. Green = small delta (<1¢), amber = moderate (1–3¢), red = large (>3¢).

### Run Stats

At the end of every run:
```
------------------------------------------------------------
  Wall clock:    152s
  Plans:         129 results  |  0 API fallback  |  20 LLM fallback
  EFL cache:     129 PDF  +  5 HTML files
  LLM calls:     34  |  prompt 62,000 tok  |  completion 1,946 tok
  Model load:    3.8s
  Prompt eval:   27.9s  (1,668 tok/s)
  Generation:    42.0s  (58 tok/s)
------------------------------------------------------------
```

---

## Golden File & Testing Tools

> **Note:** The `_golden/` folder is a **development and validation system** — it is not required to run the comparator. You can use `efl_compare.py` normally without it. The golden system is useful if you want to verify output accuracy, contribute improvements, or rebuild ground-truth data for a different zip code or service area.

A **golden file** (`_golden/golden_plans.json`) contains ground-truth rate extraction for a set of Oncor plans, built via a 3-agent adversarial Claude pipeline and reviewed by a human for edge cases. It serves as the reference standard for regression testing. The golden file is not included in the repo — build your own with `golden_build_adversarial.js` for your zip code and service area.

**`_golden/golden_compare.py`** — compares script JSON output against the golden file:
```
py -3.12 _golden/golden_compare.py [plans_latest.json] [--tiers A,B,...] [--compare-tier N]
```
Reads `EFL_TIERS` and `EFL_COMPARE_TIER` env vars as defaults (same convention as the main script).

**`_golden/golden_review.py`** — records human review overrides on individual golden entries:
```
py -3.12 _golden/golden_review.py <pid> [--energy_charge_cents 7.3] [--notes "reason"]
```

**`_golden/golden_build_adversarial.js`** — rebuilds the golden file from scratch using Claude's API (requires Claude Code with workflow support).

---

## Cache and Data Files

| File/Dir | Description |
|---|---|
| `plans_latest.csv` | Last CSV download from powertochoose.org |
| `plans_latest.html` | Last generated comparison table |
| `plans_latest.json` | Last full JSON output (rates, credits, flags per plan) |
| `_golden/` | Golden system: ground-truth file, backups, build pipeline, compare and review scripts |
| `_golden/golden_plans.json` | Ground-truth rate extraction for all 129 plans |
| `_golden/golden_plans.backup_YYYYMMDD.json` | Dated in-project backup |
| `efls_cache/` | Cached EFL PDFs and HTML files |
| `efls_cache/cache_meta.json` | Per-plan metadata: URL, ETag, Last-Modified, timestamp |
| `models/` | Downloaded LLM model (~4.7 GB) |
| `_dev/` | Development and testing tools: LLM test harnesses, PDF parser tests |

The `efls_cache/` directory is pruned automatically — EFLs for plans no longer in the PUCT CSV are deleted each run.

---

## Known Limitations

- **TriEagle Energy LP** — uses EFL URLs that occasionally time out; Playwright browser fallback handles most cases. Rates may fall back to CSV estimate on slow server days.
- **Tara Energy / Amigo Energy** — EFL servers use legacy TLS renegotiation; the script retries automatically with a compatibility SSL context.
- **Some providers** host EFLs behind JavaScript (Octopus Energy, Chariot Energy) — Playwright handles these; intermittent server slowness can cause API fallback.
- Spanish-language plan listings are excluded (English only).

---

## Architecture Notes

- EFL downloads run **in parallel** (10 workers); LLM calls are **serial** (GPU singleton)
- **PUCT Rule §25.475**: EFL Electricity Price section stated rates are authoritative over CSV data and three-tier average prices. The script and golden file both enforce this hierarchy.
- Bill credit reconciliation: EFL regex → cross-check vs PUCT CSV via LLM → escalate to LLM EFL re-parse on disagreement
- Structural LLM guards: impossible states (tier boundary with zero above-tier rate; energy threshold on bill-credit plans) are caught, corrected, and flagged as `[LLM BUG]` warnings
- TDU rates fetched from `puc.texas.gov` each run; falls back to most recent local EFL, then hardcoded constants
- The LLM auto-detects GPU via `nvidia-smi`; falls back to CPU if unavailable
- `aria-hidden="true"` on the HTML table prevents accessibility tree rebuild on row toggle (~670ms UI lag eliminated)

---

## License

Copyright (C) 2026 John Greg Hossbach

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

See [LICENSE](LICENSE) for the full license text.
