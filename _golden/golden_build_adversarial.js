export const meta = {
  name: 'golden-file-adversarial',
  description: 'Build a ground-truth golden JSON using a 3-agent adversarial pipeline (A+B extract independently, reconcile disagreements, Supervisor C verifies with growing cross-EFL context)',
  phases: [
    { title: 'Inventory',     detail: 'Read CSV, verify/download PDFs, return plan list' },
    { title: 'ExtractA',      detail: 'Agent A extracts rates from every EFL independently' },
    { title: 'ExtractB',      detail: 'Agent B extracts rates from every EFL independently' },
    { title: 'Verify',        detail: 'Sequential: reconcile A vs B, supervisor review with growing context' },
    { title: 'Write',         detail: 'Assemble and write golden_plans.json' },
  ],
}

// ─── Constants ────────────────────────────────────────────────────────────────

const ROOT   = require('path').dirname(__dirname)   // project root (one level up from _golden/)
const CACHE  = ROOT + String.raw`\efls_cache`
const CSV    = ROOT + String.raw`\plans_latest.csv`
const OUT    = ROOT + String.raw`\_golden\golden_plans.json`
const SCRIPT = ROOT + String.raw`\efl_compare.py`

const TDU_FIX_MO  = 4.06     // Oncor fixed monthly $/mo
const TDU_KWH_C   = 6.1196   // Oncor per-kWh ¢/kWh

// ─── Education primer (sent to every agent) ──────────────────────────────────
// Teaches Texas electricity pricing from first principles so agents can reason
// correctly rather than just pattern-match.

const EDUCATION = `
=== TEXAS ELECTRICITY PRICING — GROUND TRUTH EXTRACTION GUIDE ===

You are extracting GROUND TRUTH pricing data from Texas Electricity Facts Labels (EFLs).
Every number you produce will be used as a reference standard to verify automated parsing software.
Be meticulous. Show your arithmetic. Do not guess.

--- BILLING STRUCTURE ---

A Texas electricity bill has two parts:
  1. REP charges   — the Retail Electric Provider (the company you sign with)
  2. TDU charges   — Oncor Electric Delivery pass-through (always the same for all REPs in this area)

Current Oncor TDU rates:
  Fixed:    $${TDU_FIX_MO}/month  (applies regardless of usage)
  Per-kWh:  ${TDU_KWH_C}¢/kWh    (applies to every kWh used)

The EFL Electricity Price section discloses ALL pricing components.
Everything outside that section (Other Key Terms, Disclosure Chart, etc.) does NOT affect the price calculation.

--- AUTHORITATIVE SOURCE HIERARCHY (Texas PUCT Rule §25.475) ---

The Electricity Facts Label (EFL) PDF is the ONLY authoritative source for pricing data.
When any source conflicts with the EFL, the EFL always prevails. Specifically:

1. EFL > CSV data.  The PUCT CSV fields [Fees/Credits], [SpecialTerms], and the plan name
   are metadata for discovery, not authoritative pricing. If the CSV describes a credit or
   fee that does not appear as a line item in the EFL Electricity Price section, it does not
   exist for pricing purposes. Return bill_credits = [] in that case.

2. EFL Electricity Price section line items > three-tier average prices.  The 500/1000/2000 kWh
   average prices shown at the top of every EFL are illustrative disclosure examples computed
   by the REP. They are NOT contractually binding rate disclosures. The authoritative rates are
   the stated energy_charge_cents, base_charge_dollars, and any credit line items in the
   Electricity Price table. If your extracted rates reproduce those averages only when rounded
   or when using ceiling rounding, that is acceptable — the averages are examples, not exact.
   If the averages and the stated rates flatly contradict each other, trust the stated rates.

3. EFL $0 placeholder rows are not credits.  Some providers include rows like
   "Monthly Bill Credit: $0.00 per billing cycle" to declare the absence of a credit.
   These are structural placeholders — do NOT include them in bill_credits.

4. Promotional payments are not bill credits.  One-time or goodwill payments described in
   SpecialTerms (e.g. "2 × $100 promotional payments", "$25 credit on your first bill") are
   marketing offers, not EFL Electricity Price section disclosures. Exclude them entirely.

--- FIELDS TO EXTRACT ---

energy_charge_cents            The REP's energy charge in ¢/kWh.
                               Do NOT include the TDU per-kWh charge unless tdu_bundled=true.

base_charge_dollars            The REP's fixed monthly base charge in $.
                               Do NOT include the TDU $${TDU_FIX_MO}/month unless tdu_bundled=true.
                               Return 0.0 if the EFL says "N/A" or "$0".

tdu_bundled                    true ONLY when the EFL states delivery charges are bundled into the
                               stated base and energy charges, OR when TDU is listed as $0.00/0.00¢
                               explicitly. When tdu_bundled=true, the stated ec/bc ALREADY include
                               the TDU rates — do not add TDU separately in your verification.

energy_charge_threshold_kwh    The kWh level above which the energy charge applies.
                               0 means the energy charge applies from the first kWh.
                               Example: "Energy Charge only applicable to usage above 1000 kWh"
                               → energy_charge_threshold_kwh = 1000
                               Note: This is NOT the same as a bill credit threshold.

tier_boundary_kwh              0 unless the EFL shows two different energy charge rates for
                               different usage blocks (e.g. "0–1200 kWh: 12¢, >1200 kWh: 19.6¢").
                               If tiered: set tier_boundary_kwh = the boundary (e.g. 1200).
                               SIGNAL: Look for V-shape PUCT prices (kwh2000 much higher than kwh1000).

energy_charge_cents_above_tier The energy charge rate that applies ABOVE tier_boundary_kwh.
                               0.0 if tier_boundary_kwh = 0.

additional_monthly_fee_dollars One-time setup/enrollment fees that the EFL states are amortised
                               into the disclosed average prices.
                               Example: "$49.99 setup fee; 1/12 of this cost is included in the
                               average prices above" → additional_monthly_fee_dollars = 4.17
                               Return 0.0 if none.

bill_credits                   Array of {amount, threshold_kwh, cumulative, requires_enrollment}.
                               amount: dollar credit per billing cycle
                               threshold_kwh: minimum usage to receive credit (0 = applies always)
                               cumulative: true only if described as "additional" stacking credit
                               requires_enrollment: true if customer must enroll in a program
                               (auto-pay, paperless, smart thermostat, etc.)
                               IMPORTANT: An enrollment credit with no kWh threshold → threshold_kwh=0,
                               even if it appears next to a usage credit with a threshold.
                               See AUTHORITATIVE SOURCE HIERARCHY above — only non-zero credits
                               that appear as explicit line items in the EFL Electricity Price
                               section count. Plan names, CSV metadata, and SpecialTerms do not.

confidence                     "high"   — you verified your values reproduce the EFL average prices ±0.1¢
                               "medium" — minor ambiguity, or verification off by 0.1–0.5¢
                               "low"    — no PDF available, or EFL format was too ambiguous

--- VERIFICATION FORMULAS ---

Always verify your extracted rates by computing bill(kwh)/kwh×100 at 500, 1000, and 2000 kWh and
comparing against the EFL's three-tier average prices. These averages are a sanity check — they
are illustrative examples, not authoritative (see AUTHORITATIVE SOURCE HIERARCHY above). Minor
rounding differences of ±0.1¢ are acceptable; differences up to ±0.5¢ may reflect enrollment
credits. If your rates reproduce the averages within those tolerances, set confidence = "high".
If they do not, re-examine your rates — but if the EFL's stated rates are unambiguous and your
arithmetic is correct, trust the stated rates over the averages.

Standard plan (tdu_bundled=false, tier_boundary_kwh=0, energy_charge_threshold_kwh=0):
  bill(kwh) = ec/100*kwh + bc + ${TDU_FIX_MO} + ${TDU_KWH_C}/100*kwh − Σcredits_that_apply(kwh)
  rate_c    = bill(kwh) / kwh * 100

TDU-bundled plan (tdu_bundled=true, no threshold, no tier):
  bill(kwh) = ec/100*kwh + bc − Σcredits_that_apply(kwh)
  rate_c    = bill(kwh) / kwh * 100

TDU-bundled + threshold (tdu_bundled=true, energy_charge_threshold_kwh=T):
  billable  = max(0, kwh − T)
  bill(kwh) = ec/100*billable + bc − Σcredits_that_apply(kwh)
  rate_c    = bill(kwh) / kwh * 100

Tiered plan (tier_boundary_kwh=B, tdu_bundled=false):
  ec1 = energy_charge_cents (rate for 0..B kWh)
  ec2 = energy_charge_cents_above_tier (rate for >B kWh)
  bill(kwh) = ec1/100*min(kwh,B) + ec2/100*max(0,kwh−B) + bc + ${TDU_FIX_MO} + ${TDU_KWH_C}/100*kwh − credits
  rate_c    = bill(kwh) / kwh * 100

--- COMMON MISTAKES (avoid these) ---

✗ Including the TDU $${TDU_FIX_MO}/month or ${TDU_KWH_C}¢/kWh as the REP's base_charge_dollars or energy_charge_cents
  (These are TDU pass-throughs. Only include them if tdu_bundled=true.)
✗ Setting tdu_bundled=true just because TDU charges appear in the EFL
  (They will appear in almost all EFLs as a separate listed pass-through. tdu_bundled=true only
   when TDU is listed as $0 or the EFL explicitly says delivery is bundled into the stated rates.)
✗ Confusing a bill credit threshold with an energy charge threshold
  (A $125 credit "when usage ≥ 1000 kWh" is a credit, not a threshold energy charge.
   An energy charge threshold means the energy charge literally does not apply below that usage.)
✗ Assigning a kWh threshold to an enrollment credit (auto-pay, paperless)
  (Enrollment credits apply whenever the customer is enrolled, regardless of usage.
   Their threshold_kwh must be 0 even if listed alongside a usage-based credit.)
✗ Stopping at 3 digits for energy charge. Use the full precision from the EFL
  (e.g. "7.3130¢" should be stored as 7.313, not 7.31)
=== END OF GUIDE ===
`

// ─── Schemas ──────────────────────────────────────────────────────────────────

const EXTRACT_SCHEMA = {
  type: 'object',
  properties: {
    energy_charge_cents:            { type: 'number' },
    base_charge_dollars:            { type: 'number' },
    tdu_bundled:                    { type: 'boolean' },
    energy_charge_threshold_kwh:    { type: 'integer' },
    tier_boundary_kwh:              { type: 'integer' },
    energy_charge_cents_above_tier: { type: 'number' },
    additional_monthly_fee_dollars: { type: 'number' },
    bill_credits: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          amount:              { type: 'number' },
          threshold_kwh:       { type: 'integer' },
          cumulative:          { type: 'boolean' },
          requires_enrollment: { type: 'boolean' },
        },
        required: ['amount', 'threshold_kwh', 'cumulative', 'requires_enrollment'],
        additionalProperties: false,
      },
    },
    confidence:         { type: 'string', enum: ['high', 'medium', 'low'] },
    notes:              { type: 'string' },
    verification_shown: { type: 'string' },
  },
  required: [
    'energy_charge_cents', 'base_charge_dollars', 'tdu_bundled',
    'energy_charge_threshold_kwh', 'tier_boundary_kwh', 'energy_charge_cents_above_tier',
    'additional_monthly_fee_dollars', 'bill_credits',
    'confidence', 'notes', 'verification_shown',
  ],
  additionalProperties: false,
}

const SUPERVISOR_SCHEMA = {
  type: 'object',
  properties: {
    approved:    { type: 'boolean' },
    objections:  { type: 'string' },
    confidence_override: { type: 'string', enum: ['high', 'medium', 'low', 'unchanged'] },
  },
  required: ['approved', 'objections', 'confidence_override'],
  additionalProperties: false,
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function compactSummary(pid, plan) {
  // One-line summary of a verified golden record (grows the context window)
  const g = plan.golden
  const credits = g.bill_credits.length > 0
    ? g.bill_credits.map(c => `$${c.amount}@${c.threshold_kwh}kWh${c.requires_enrollment?'(enroll)':''}`).join('+')
    : 'none'
  const tier = g.tier_boundary_kwh > 0
    ? ` tier@${g.tier_boundary_kwh}kWh(${g.energy_charge_cents_above_tier}¢above)`
    : ''
  const thresh = g.energy_charge_threshold_kwh > 0
    ? ` ec_above_${g.energy_charge_threshold_kwh}kWh`
    : ''
  const fee = g.additional_monthly_fee_dollars > 0
    ? ` +$${g.additional_monthly_fee_dollars}/mo_fee`
    : ''
  return `[${pid}] ${plan.provider.slice(0,20)}/${plan.plan.slice(0,18)} ${plan.term_months}mo | ec=${g.energy_charge_cents}¢ bc=$${g.base_charge_dollars} bundled=${g.tdu_bundled}${thresh}${tier}${fee} | credits:${credits} | ${g.confidence} | ${g.notes.slice(0,60)}`
}

function extractionPrompt(plan, role, peerSummary) {
  const p5  = plan.kwh500  ? (parseFloat(plan.kwh500)  * 100).toFixed(2) : '?'
  const p10 = plan.kwh1000 ? (parseFloat(plan.kwh1000) * 100).toFixed(2) : '?'
  const p20 = plan.kwh2000 ? (parseFloat(plan.kwh2000) * 100).toFixed(2) : '?'

  const tieredHint = (plan.kwh2000 && plan.kwh1000 &&
    (parseFloat(plan.kwh2000) - parseFloat(plan.kwh1000)) * 100 > 1.5)
    ? `\n⚠ TIERED RATE SIGNAL: kwh2000 (${p20}¢) is more than 1.5¢ above kwh1000 (${p10}¢). Look for two different energy charge rates in the EFL.`
    : ''

  return `${EDUCATION}

You are Extraction Agent ${role}. Work INDEPENDENTLY — do not be influenced by any other agent.

PLAN: ${plan.provider} / ${plan.plan}
PID:  ${plan.pid} | Term: ${plan.term_months} months
${plan.pdf_path ? `PDF: ${plan.pdf_path}` : '⚠ NO PDF AVAILABLE — use PUCT back-calculation only'}

PUCT reported average prices: ${p5}¢@500kWh | ${p10}¢@1000kWh | ${p20}¢@2000kWh${tieredHint}
${plan.fees_credits ? `PUCT [Fees/Credits]: ${plan.fees_credits}` : ''}
${plan.special_terms ? `[SpecialTerms]: ${plan.special_terms}` : ''}

INSTRUCTIONS:
${plan.pdf_path ? `
1. Extract the full EFL text using PyMuPDF (spatial/reading-order mode):
   py -3.12 -c "import fitz,sys; sys.stdout.reconfigure(encoding='utf-8',errors='replace'); doc=fitz.open(r'${plan.pdf_path}'); [print(p.get_text('text',sort=True)) for p in doc]; doc.close()"

2. Locate the Electricity Price section (from "Average Monthly Use" table through "Other Key Terms").
   This section contains EVERYTHING needed to determine the price. Ignore all other sections.

3. Extract all schema fields per the guide above.

4. Show your verification math for all three PUCT tiers (500/1000/2000 kWh).
   If your values don't reproduce the EFL average prices within ±0.2¢, re-examine your extraction.
   The EFL average prices are the gold standard; PUCT values may differ by up to 0.5¢ (enrollment credits included in PUCT but not always in EFL avg).

5. Set confidence:
   "high"   — your values reproduce the EFL average prices within ±0.1¢ at all three tiers
   "medium" — off by 0.1–0.5¢ on one tier, or minor structural ambiguity
   "low"    — no PDF, or could not verify within ±0.5¢
` : `
No PDF available. Back-calculate from PUCT prices:
   ec_backCalc ≈ (${p10}¢ × 1000 - 4.06×100 - ${TDU_KWH_C}×1000) / 1000
   bc_backCalc = 0 (assumed — no EFL to verify)
   ${plan.fees_credits ? `Parse bill credits from: ${plan.fees_credits}` : 'No credits in CSV.'}
   Set confidence="low".
`}
${peerSummary ? `\nCONTEXT — verified records from other EFLs (for calibration, not copying):\n${peerSummary}` : ''}`
}

function supervisorPrompt(plan, agreed, isChallenged, prevObjections, verifiedContext) {
  const p5  = plan.kwh500  ? (parseFloat(plan.kwh500)  * 100).toFixed(2) : '?'
  const p10 = plan.kwh1000 ? (parseFloat(plan.kwh1000) * 100).toFixed(2) : '?'
  const p20 = plan.kwh2000 ? (parseFloat(plan.kwh2000) * 100).toFixed(2) : '?'

  return `${EDUCATION}

You are the Supervisor — an adversarial reviewer whose job is to FIND ERRORS.
A plan has been submitted for golden file inclusion. Your independent review is the last gate.

PLAN: ${plan.provider} / ${plan.plan} (${plan.term_months} months, pid=${plan.pid})
PUCT prices: ${p5}¢@500kWh | ${p10}¢@1000kWh | ${p20}¢@2000kWh

SUBMITTED RECORD:
${JSON.stringify(agreed, null, 2)}

${isChallenged ? `PREVIOUS OBJECTIONS THAT TRIGGERED THIS RETRY:\n${prevObjections}\n` : ''}

YOUR TASK:
1. Independently verify the math for all three PUCT tiers using the submitted values.
   Use the formulas from the guide above. Show your arithmetic.

2. Check for the common mistakes listed in the guide:
   - Is TDU being included in base_charge_dollars when tdu_bundled=false?
   - Is tdu_bundled=true correct (TDU listed as $0 or stated as bundled)?
   - Are bill credit threshold_kwh values correct (enrollment credits must have threshold_kwh=0)?
   - Is the tiered rate correctly identified if kwh2000 >> kwh1000?
   - Is the energy charge full precision (not rounded prematurely)?

3. Cross-reference against the verified context below. Flag if this plan's structure
   is unexpectedly different from similar providers without a clear EFL justification.

4. If you find errors: set approved=false with specific objections.
   If the record is correct: set approved=true, objections="none".
   You may upgrade or downgrade confidence via confidence_override.

VERIFIED RECORDS FROM OTHER EFLs (for pattern calibration):
${verifiedContext || '(none yet — this is the first plan)'}

Respond with the supervisor verdict JSON.`
}

// ─── Phase 1: Inventory ───────────────────────────────────────────────────────
// Two-step approach to avoid 32K output token limit:
//   Step A: write full plan data to a temp JSON file (agent returns only count/stats)
//   Step B: read back in 3 batches (~43 plans each, well under the token limit)
phase('Inventory')

const INVENTORY_FILE = ROOT + String.raw`\_dev\plans_inventory_tmp.json`

const PLAN_SCHEMA_ITEM = {
  type: 'object',
  properties: {
    pid: {type:'string'}, provider: {type:'string'}, plan: {type:'string'},
    term_months: {type:'integer'}, has_crd: {type:'boolean'},
    kwh500: {type:'string'}, kwh1000: {type:'string'}, kwh2000: {type:'string'},
    facts_url: {type:'string'}, fees_credits: {type:'string'},
    special_terms: {type:'string'}, pdf_path: {type:'string'},
  },
  required: ['pid','provider','plan','term_months','has_crd',
             'kwh500','kwh1000','kwh2000','facts_url',
             'fees_credits','special_terms','pdf_path'],
  additionalProperties: false,
}

// Step A: build inventory, write to disk, return only count stats (tiny output)
const invStatus = await agent(
  `Write the eligible plan inventory to this file: ${INVENTORY_FILE}
Run this command exactly. Do not list or explain each plan. Just run it and report the counts.

py -3.12 -c "
import csv, pathlib, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
cache = pathlib.Path(r'${CACHE}')
plans = []
with open(r'${CSV}', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        lang = (row.get('[Language]') or '').strip().lower()
        tdu  = (row.get('[TduCompanyName]') or '').lower()
        fixed = row.get('[Fixed]','').strip()
        prepaid = (row.get('[PrePaid]') or '').strip().upper()
        if lang != 'english': continue
        if 'oncor' not in tdu: continue
        if fixed != '1': continue
        if prepaid == 'TRUE': continue
        if (row.get('[TimeOfUse]') or '').strip() == 'True': continue
        pid = row['[idKey]']
        matches = list(cache.glob(f'*_{pid}.pdf'))
        plans.append({
            'pid': pid,
            'provider': (row.get('[RepCompany]') or '').strip(),
            'plan': (row.get('[Product]') or '').strip(),
            'term_months': int(row.get('[TermValue]') or 0),
            'has_crd': row.get('[MinUsageFeesCredits]','') == 'TRUE',
            'kwh500': (row.get('[kwh500]') or '').strip(),
            'kwh1000': (row.get('[kwh1000]') or '').strip(),
            'kwh2000': (row.get('[kwh2000]') or '').strip(),
            'facts_url': (row.get('[FactsURL]') or '').strip(),
            'fees_credits': (row.get('[Fees/Credits]') or '').strip(),
            'special_terms': (row.get('[SpecialTerms]') or '').strip(),
            'pdf_path': str(matches[0]) if matches else '',
        })
pathlib.Path(r'${INVENTORY_FILE}').write_text(json.dumps({'plans': plans}), encoding='utf-8')
print(f'count:{len(plans)} with_pdf:{sum(1 for p in plans if p[\"pdf_path\"])} missing:{sum(1 for p in plans if not p[\"pdf_path\"])}')
"

If missing > 0, also run this to download missing EFLs:
  cd "${ROOT}" && py -3.12 efl_compare.py --no-cache-check --no-html --no-llm 2>&1 | tail -3
Then re-run the write command above.

Return the count, with_pdf, and missing values from the printed output.`,
  {
    schema: {
      type: 'object',
      properties: {
        count:    { type: 'integer' },
        with_pdf: { type: 'integer' },
        missing:  { type: 'integer' },
      },
      required: ['count', 'with_pdf', 'missing'],
      additionalProperties: false,
    },
    label: 'inventory-write',
    phase: 'Inventory',
  }
)

log(`Inventory: ${invStatus.count} eligible plans, ${invStatus.with_pdf} with PDFs, ${invStatus.missing} unavailable`)

// Step B: read plans back in 3 batches (~43 plans each ≈ 3K tokens per batch, well under limit)
const B = Math.ceil((invStatus.count || 129) / 3)
const BATCH_SCHEMA = { type: 'object', properties: { plans: { type: 'array', items: PLAN_SCHEMA_ITEM } }, required: ['plans'], additionalProperties: false }

const [batch1, batch2, batch3] = await parallel([
  () => agent(
    `Read plans at indices 0 to ${B-1} from ${INVENTORY_FILE}.
Run: py -3.12 -c "import json,sys; sys.stdout.reconfigure(encoding='utf-8',errors='replace'); d=json.load(open(r'${INVENTORY_FILE}',encoding='utf-8')); print(json.dumps({'plans':d['plans'][0:${B}]}))"
Return the plans array exactly as output. Do not add explanations.`,
    { schema: BATCH_SCHEMA, label: 'inv-b1', phase: 'Inventory' }
  ),
  () => agent(
    `Read plans at indices ${B} to ${B*2-1} from ${INVENTORY_FILE}.
Run: py -3.12 -c "import json,sys; sys.stdout.reconfigure(encoding='utf-8',errors='replace'); d=json.load(open(r'${INVENTORY_FILE}',encoding='utf-8')); print(json.dumps({'plans':d['plans'][${B}:${B*2}]}))"
Return the plans array exactly as output. Do not add explanations.`,
    { schema: BATCH_SCHEMA, label: 'inv-b2', phase: 'Inventory' }
  ),
  () => agent(
    `Read plans at indices ${B*2} onwards from ${INVENTORY_FILE}.
Run: py -3.12 -c "import json,sys; sys.stdout.reconfigure(encoding='utf-8',errors='replace'); d=json.load(open(r'${INVENTORY_FILE}',encoding='utf-8')); print(json.dumps({'plans':d['plans'][${B*2}:]}))"
Return the plans array exactly as output. Do not add explanations.`,
    { schema: BATCH_SCHEMA, label: 'inv-b3', phase: 'Inventory' }
  ),
])

const plans = [
  ...(batch1?.plans || []),
  ...(batch2?.plans || []),
  ...(batch3?.plans || []),
].filter(Boolean)

log(`Inventory loaded: ${plans.length} plans (3 batches of ~${B})`)

// ─── Phase 2A: Agent A extracts from every EFL (parallel) ─────────────────────
phase('ExtractA')
log('Agent A running on all plans in parallel...')

const extractionsA = await pipeline(
  plans,
  async (plan) => {
    const result = await agent(
      extractionPrompt(plan, 'A', null),
      { schema: EXTRACT_SCHEMA, label: `A:${plan.pid}`, phase: 'ExtractA' }
    )
    return { plan, extraction: result }
  }
)

// ─── Phase 2B: Agent B extracts from every EFL (parallel, no A context) ───────
phase('ExtractB')
log('Agent B running on all plans in parallel (no knowledge of A results)...')

const extractionsB = await pipeline(
  plans,
  async (plan, _orig, idx) => {
    const result = await agent(
      extractionPrompt(plan, 'B', null),
      { schema: EXTRACT_SCHEMA, label: `B:${plan.pid}`, phase: 'ExtractB' }
    )
    return { plan, extraction: result }
  }
)

// Build lookup maps
const mapA = {}; for (const r of extractionsA.filter(Boolean)) mapA[r.plan.pid] = r.extraction
const mapB = {}; for (const r of extractionsB.filter(Boolean)) mapB[r.plan.pid] = r.extraction

// ─── Phase 3: Adversarial verification (sequential, growing context) ──────────
phase('Verify')
log('Sequential adversarial verification with growing context...')

const verifiedGolden = []
const contested       = []
let verifiedContext   = ''   // grows with each approved record (compact summaries)

// Process longest-term plans first (they tend to have more complex structures;
// establishing patterns early helps the supervisor calibrate for shorter terms)
const plansSorted = [...plans].sort((a,b) => b.term_months - a.term_months)

for (const plan of plansSorted) {
  const pid = plan.pid
  const extA = mapA[pid]
  const extB = mapB[pid]

  // Plans with no PDF and no extraction → skip adversarial, write unavailable record
  if (!plan.pdf_path && !extA && !extB) {
    const ec_back = plan.kwh1000
      ? (parseFloat(plan.kwh1000) * 100 - TDU_KWH_C - TDU_FIX_MO / 10).toFixed(4)
      : '0'
    verifiedGolden.push({
      pid,
      provider:      plan.provider,
      plan:          plan.plan,
      term_months:   plan.term_months,
      has_bill_credit: plan.has_crd,
      puct_kwh500c:  parseFloat(plan.kwh500  || 0) * 100,
      puct_kwh1000c: parseFloat(plan.kwh1000 || 0) * 100,
      puct_kwh2000c: parseFloat(plan.kwh2000 || 0) * 100,
      facts_url:     plan.facts_url,
      golden: {
        energy_charge_cents: parseFloat(ec_back),
        base_charge_dollars: 0.0,
        tdu_bundled: false,
        energy_charge_threshold_kwh: 0,
        tier_boundary_kwh: 0,
        energy_charge_cents_above_tier: 0.0,
        additional_monthly_fee_dollars: 0.0,
        bill_credits: [],
        confidence: 'low',
        notes: 'No PDF available — PUCT back-calculation only',
        verification_shown: 'N/A',
      },
    })
    log(`  [unavailable] ${plan.provider}/${plan.plan}`)
    continue
  }

  // ── Step 1: Compare A and B ─────────────────────────────────────────────────
  let agreed = extA || extB   // fallback if one is null
  let needsReconcile = false

  if (extA && extB) {
    // Check key fields for agreement
    const ecDiff  = Math.abs((extA.energy_charge_cents || 0) - (extB.energy_charge_cents || 0))
    const bcDiff  = Math.abs((extA.base_charge_dollars || 0) - (extB.base_charge_dollars || 0))
    const bundDiff = extA.tdu_bundled !== extB.tdu_bundled
    const thrDiff  = extA.energy_charge_threshold_kwh !== extB.energy_charge_threshold_kwh
    const tierDiff = extA.tier_boundary_kwh !== extB.tier_boundary_kwh

    needsReconcile = ecDiff > 0.05 || bcDiff > 0.10 || bundDiff || thrDiff || tierDiff
  }

  // ── Step 2: Reconcile if needed ─────────────────────────────────────────────
  if (needsReconcile && extA && extB) {
    log(`  [reconcile] ${plan.provider}/${plan.plan} — A and B disagree`)
    const reconciled = await agent(
      `${EDUCATION}

You are the Reconciler. Two independent agents extracted different results from the same EFL.
Examine both extractions, re-read the EFL if needed, and produce the single correct answer.

PLAN: ${plan.provider} / ${plan.plan} (${plan.term_months}mo, pid=${pid})
PUCT: ${(parseFloat(plan.kwh500||0)*100).toFixed(2)}¢@500 | ${(parseFloat(plan.kwh1000||0)*100).toFixed(2)}¢@1000 | ${(parseFloat(plan.kwh2000||0)*100).toFixed(2)}¢@2000
${plan.pdf_path ? `PDF: ${plan.pdf_path}` : 'NO PDF'}

AGENT A EXTRACTION:
${JSON.stringify(extA, null, 2)}

AGENT B EXTRACTION:
${JSON.stringify(extB, null, 2)}

DISAGREEMENTS:
${Math.abs((extA.energy_charge_cents||0)-(extB.energy_charge_cents||0)) > 0.05 ? `  energy_charge_cents: A=${extA.energy_charge_cents} vs B=${extB.energy_charge_cents}` : ''}
${Math.abs((extA.base_charge_dollars||0)-(extB.base_charge_dollars||0)) > 0.10 ? `  base_charge_dollars: A=${extA.base_charge_dollars} vs B=${extB.base_charge_dollars}` : ''}
${extA.tdu_bundled !== extB.tdu_bundled ? `  tdu_bundled: A=${extA.tdu_bundled} vs B=${extB.tdu_bundled}` : ''}
${extA.energy_charge_threshold_kwh !== extB.energy_charge_threshold_kwh ? `  energy_charge_threshold_kwh: A=${extA.energy_charge_threshold_kwh} vs B=${extB.energy_charge_threshold_kwh}` : ''}
${extA.tier_boundary_kwh !== extB.tier_boundary_kwh ? `  tier_boundary_kwh: A=${extA.tier_boundary_kwh} vs B=${extB.tier_boundary_kwh}` : ''}

Re-extract from the EFL if needed (use PyMuPDF):
py -3.12 -c "import fitz,sys; sys.stdout.reconfigure(encoding='utf-8',errors='replace'); doc=fitz.open(r'${plan.pdf_path||''}'); [print(p.get_text('text',sort=True)) for p in doc]; doc.close()"

Produce the single correct answer. Show your math. Reference the VERIFIED CONTEXT below
to calibrate against similar plan structures.

VERIFIED CONTEXT:
${verifiedContext || '(none yet)'}`,
      { schema: EXTRACT_SCHEMA, label: `reconcile:${pid}`, phase: 'Verify' }
    )
    if (reconciled) agreed = reconciled
  }

  // ── Step 3: Supervisor review (adversarial) ──────────────────────────────────
  let supervisorApproved = false
  let prevObjections     = ''
  let finalRecord        = agreed

  for (let attempt = 0; attempt < 2; attempt++) {
    const isRetry = attempt > 0
    if (isRetry) {
      log(`    [retry]  ${plan.provider}/${plan.plan} — supervisor objected: ${prevObjections.slice(0,80)}`)
    }

    const verdict = await agent(
      supervisorPrompt(plan, finalRecord, isRetry, prevObjections, verifiedContext),
      { schema: SUPERVISOR_SCHEMA, label: `supervisor:${pid}:${attempt}`, phase: 'Verify' }
    )

    if (!verdict) break

    if (verdict.approved) {
      supervisorApproved = true
      // Apply confidence override if specified
      if (verdict.confidence_override && verdict.confidence_override !== 'unchanged') {
        finalRecord = { ...finalRecord, confidence: verdict.confidence_override }
      }
      break
    }

    prevObjections = verdict.objections || ''

    if (attempt < 1) {
      // One retry: re-extract with supervisor's objections as context
      const retried = await agent(
        extractionPrompt(plan, 'RETRY', verifiedContext) + `

SUPERVISOR OBJECTIONS FROM PRIOR ATTEMPT (you must address all of these):
${prevObjections}

Re-examine the EFL carefully. Your previous extraction had errors. Fix them.`,
        { schema: EXTRACT_SCHEMA, label: `retry:${pid}`, phase: 'Verify' }
      )
      if (retried) finalRecord = retried
    }
  }

  // ── Step 4: Record outcome ───────────────────────────────────────────────────
  const record = {
    pid,
    provider:      plan.provider,
    plan:          plan.plan,
    term_months:   plan.term_months,
    has_bill_credit: plan.has_crd,
    puct_kwh500c:  parseFloat(plan.kwh500  || 0) * 100,
    puct_kwh1000c: parseFloat(plan.kwh1000 || 0) * 100,
    puct_kwh2000c: parseFloat(plan.kwh2000 || 0) * 100,
    facts_url:     plan.facts_url,
    golden: {
      ...finalRecord,
      // If supervisor never approved after retries, force low confidence and flag
      confidence: supervisorApproved ? finalRecord.confidence : 'low',
      notes: supervisorApproved
        ? finalRecord.notes
        : `CONTESTED — supervisor not satisfied after 2 attempts. ${finalRecord.notes}`,
    },
  }

  verifiedGolden.push(record)
  if (!supervisorApproved) contested.push(pid)

  // Grow the context (compact one-line summary)
  verifiedContext += '\n' + compactSummary(pid, record)

  log(`  [${supervisorApproved ? '✓' : 'CONTESTED'}] ${plan.provider.slice(0,25)}/${plan.plan.slice(0,20)} conf=${record.golden.confidence}`)
}

log(`Verification complete: ${verifiedGolden.length} records, ${contested.length} contested`)

// ─── Phase 4: Write golden file ────────────────────────────────────────────────
phase('Write')

// Build the full output JSON in JavaScript (correct format, no Python quoting issues)
// Use json.loads(r"""...""") to safely embed JSON (with false/true/null) in Python code.
// Python raw triple-quoted strings handle all JSON content since JSON never has """.
const contestedJson   = JSON.stringify(contested)
const verifiedJson    = JSON.stringify(verifiedGolden)

const writeResult = await agent(
  `Write the golden file to disk. ${verifiedGolden.length} verified records to write.

Run these two Python commands in order:

Step 1 — write to temp file (uses json.loads to handle JSON boolean/null syntax):
py -3.12 -c "
import json, pathlib, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
output = {
    'schema_version': '2.0',
    'description': 'Ground-truth golden records — 3-agent adversarial extraction (A+B independent, Supervisor C adversarial)',
    'agents': 'Claude (not local LLM)',
    'tdu': {'fixed_mo_dollars': ${TDU_FIX_MO}, 'per_kwh_cents': ${TDU_KWH_C}},
    'contested_pids': json.loads(r\"\"\"${contestedJson}\"\"\"),
    'plans': json.loads(r\"\"\"${verifiedJson}\"\"\"),
}
pathlib.Path(r'${ROOT}\\_dev\\golden_tmp_write.json').write_text(json.dumps(output, indent=2), encoding='utf-8')
print('Temp written:', len(output['plans']), 'plans,', len(output['contested_pids']), 'contested')
"

Step 2 — copy to final output path:
py -3.12 -c "
import json, pathlib, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
data = json.loads(pathlib.Path(r'${ROOT}\\_dev\\golden_tmp_write.json').read_text(encoding='utf-8'))
pathlib.Path(r'${OUT}').write_text(json.dumps(data, indent=2), encoding='utf-8')
print('Written', len(data['plans']), 'plans to', r'${OUT}')
"

Confirm both commands succeed and return the final plan count.`,
  { label: 'write-golden', phase: 'Write' }
)

return {
  total_plans:     plans.length,
  verified:        verifiedGolden.length,
  contested_count: contested.length,
  contested_pids:  contested,
  unavailable:     verifiedGolden.filter(r => r.golden.confidence === 'low' && r.golden.notes.includes('No PDF')).length,
  output:          OUT,
}
