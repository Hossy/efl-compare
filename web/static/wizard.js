/* Plain-language guided view over the plan data.
 *
 * Reads the JSON that efl_compare.py already produces (via this server's
 * /api/plans) and prices every plan client-side. Nothing here needs the parent
 * CLI to know the wizard exists: the pricing below mirrors the CLI's own
 * effective_cents_per_kwh() using the raw rate components the CLI already
 * exports (energy_charge_cents, base_charge_dollars, tdu_bundled,
 * energy_threshold_kwh, tier_boundary_kwh, ec_cents_above_tier, bill_credits).
 */
'use strict';

var DATA = null;
var FAMILIES = [];              // brand_families.json, normalised at boot
var app = document.getElementById('app');
var state = {step: 0, usage: null, entry: null, termPref: 'any',
             manualBill: null, manualEtf: null, manualMonths: null};

// ---------------------------------------------------------------------------
// Theme
//
// Three states, cycled by the header button: auto -> light -> dark -> auto.
// Auto is the resting state and follows the OS; picking anything else is
// remembered. The <head> applies the stored choice before first paint, so this
// only has to handle changes and the button's own labelling.
// ---------------------------------------------------------------------------

var THEMES = [
  {id: 'auto',  label: 'Theme: following your system. Switch to light.'},
  {id: 'light', label: 'Theme: light. Switch to dark.'},
  {id: 'dark',  label: 'Theme: dark. Switch to follow your system.'}
];

function currentTheme(){
  return document.documentElement.getAttribute('data-theme') || 'auto';
}

function setTheme(id){
  var root = document.documentElement;
  if (id === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', id);
  try{
    if (id === 'auto') localStorage.removeItem('efl-theme');
    else localStorage.setItem('efl-theme', id);
  }catch(e){}          // storage unavailable: the choice just won't persist
  var btn = document.getElementById('themeToggle');
  if (btn){
    var t = THEMES.filter(function(x){ return x.id === id; })[0] || THEMES[0];
    btn.setAttribute('aria-label', t.label);
    btn.setAttribute('title', t.label);
  }
}

function initTheme(){
  var btn = document.getElementById('themeToggle');
  if (!btn) return;
  setTheme(currentTheme());
  btn.onclick = function(){
    var i = 0;
    for (var j = 0; j < THEMES.length; j++) if (THEMES[j].id === currentTheme()) i = j;
    setTheme(THEMES[(i + 1) % THEMES.length].id);
  };
}

// ---------------------------------------------------------------------------
// Who actually owns the brand
//
// PUCT lists brands, not companies. Tara, Amigo and Just Energy are one
// company; so are Reliant, Green Mountain, Cirro, Discount Power and Direct
// Energy. A shopper reading a top-three can be looking at a single company
// three times and have no way to tell -- which is exactly what a comparison
// site is supposed to prevent. See brand_families.json.
// ---------------------------------------------------------------------------

function normProvider(s){
  return ('' + (s || '')).toUpperCase()
    .replace(/[.,'"]/g, ' ')
    .replace(/\b(LLC|LP|INC|INCORPORATED|CORP|CORPORATION|COMPANY|HOLDINGS|GROUP)\b/g, ' ')
    .replace(/\s+/g, ' ').trim();
}

// null when the brand stands alone -- most do, and saying nothing is correct.
//
// Matching is prefix-based, not exact: PUCT says "TXU ENERGY" but an EFL signs
// itself "TXU Energy Retail Company LLC", and the bill reader returns whatever
// the document says. Anchoring at a word boundary keeps that tolerant without
// letting "TRUE POWER" collide with "TRUEPOWER GREEN" or similar.
function familyOf(provider){
  var n = normProvider(provider);
  if (!n) return null;
  for (var i = 0; i < FAMILIES.length; i++){
    var brands = FAMILIES[i]._brands;
    for (var j = 0; j < brands.length; j++){
      var b = brands[j];
      if (n === b || n.indexOf(b + ' ') === 0) return FAMILIES[i];
    }
  }
  return null;
}

function money(cents, kwh){ return Math.round(cents / 100 * kwh); }
function fmtMonths(n){ return (Math.round(n * 10) / 10).toString().replace(/\.0$/, ''); }
function esc(s){ return ('' + s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function setDots(){ for (var i = 0; i < 3; i++){ document.getElementById('d' + i).className = 'dot' + (i <= state.step ? ' on' : ''); } }

// ---------------------------------------------------------------------------
// Data: /api/plans (the CLI's --json payload) -> the shape this page renders
// ---------------------------------------------------------------------------

var _MONTHS = ['January','February','March','April','May','June','July',
               'August','September','October','November','December'];

function fmtGenerated(iso){
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return ('' + iso).slice(0, 10);
  return _MONTHS[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
}

function toPlan(p){
  return {
    provider:   p.provider || '',
    plan:       p.plan || '',
    term:       p.term_months,
    etf:        p.cancellation_fee,
    rnw:        p.renewable_pct,
    tiers:      p.rates_cents_per_kwh || {},
    enroll_url: p.enroll_url || '',
    website_url: p.website_url || '',
    facts_url:  p.facts_url || '',
    manual:     !!p.manual,
    // The CLI exports cents; every calculation below is in dollars.
    ec:         (p.energy_charge_cents || 0) / 100,   // $/kWh
    bc:         p.base_charge_dollars || 0,           // $/mo
    tduBundled: !!p.tdu_bundled,
    ecThresh:   p.energy_threshold_kwh || 0,
    tierKwh:    p.tier_boundary_kwh || 0,
    ecAbove:    (p.ec_cents_above_tier || 0) / 100,   // $/kWh
    credits:    (p.bill_credits || []).map(function(c){
      return {amount:              +c.amount || 0,
              threshold_kwh:       +c.threshold_kwh || 0,
              requires_enrollment: !!c.requires_enrollment};
    })
  };
}

function buildData(raw){
  var plans = raw.plans || [];
  var t     = raw.tdu || {};
  var picks = plans.filter(function(p){ return !p.current; }).map(toPlan);
  var curRaw = plans.filter(function(p){ return p.current; })[0] || null;

  // Friendly home-size presets, kept only for usage tiers actually priced.
  var avail = {};
  plans.forEach(function(p){
    Object.keys(p.rates_cents_per_kwh || {}).forEach(function(k){ avail[parseInt(k, 10)] = true; });
  });
  var presets = [
    {kwh: 500,  label: 'Smaller home', sub: 'Apartment or condo'},
    {kwh: 1000, label: 'Average home', sub: '2–3 bedroom house'},
    {kwh: 2000, label: 'Larger home',  sub: 'Big house, pool, or electric heat'}
  ].filter(function(p){ return avail[p.kwh]; });
  if (!presets.length){
    var keys = Object.keys(avail).map(Number).sort(function(a, b){ return a - b; });
    if (keys.length){
      var mid = keys[Math.floor(keys.length / 2)];
      presets = [{kwh: mid, label: 'Your usage', sub: mid.toLocaleString() + ' kWh/mo'}];
    }
  }

  return {
    zip:       raw.zip || '',
    generated: fmtGenerated(raw.generated),
    planCount: picks.length,
    picks:     picks,
    current:   curRaw ? toPlan(curRaw) : null,
    presets:   presets,
    // The CLI's tdu block is {fixed_mo_dollars, per_kwh_cents}; the maths below
    // is all in dollars, so per-kWh is converted once here.
    tdu: {fixed_mo: t.fixed_mo_dollars || 0,
          per_kwh:  (t.per_kwh_cents || 0) / 100}
  };
}

// ---------------------------------------------------------------------------
// Pricing — mirrors the CLI's effective_cents_per_kwh()
// ---------------------------------------------------------------------------

function planMonthlyExact(p, kwh){
  if (!kwh || kwh <= 0 || p.ec == null) return null;
  var credit = 0;
  (p.credits || []).forEach(function(c){ if (c.threshold_kwh <= kwh) credit += c.amount; });
  var energyCost;
  if (p.tierKwh > 0 && kwh > p.tierKwh){
    energyCost = p.ec * p.tierKwh + p.ecAbove * (kwh - p.tierKwh);
  } else {
    var billable = p.ecThresh > 0 ? Math.max(0, kwh - p.ecThresh) : kwh;
    energyCost = p.ec * billable;
  }
  var total;
  if (p.tduBundled){
    total = energyCost + (p.bc || 0) - credit;
  } else {
    var tdu = DATA.tdu || {fixed_mo: 0, per_kwh: 0};
    total = energyCost + (p.bc || 0) + tdu.fixed_mo + tdu.per_kwh * kwh - credit;
  }
  return Math.round(total);
}

// Fallback for plans with no usable rate components: piecewise-linear on the
// tier bill totals the CLI publishes.
function interpMonthly(p, kwh){
  var pts = Object.keys(p.tiers).map(function(k){ var kk = parseInt(k, 10); return {k: kk, $: money(p.tiers[k], kk)}; })
    .filter(function(x){ return x.$ != null; }).sort(function(a, b){ return a.k - b.k; });
  if (!pts.length) return null;
  if (pts.length === 1) return Math.round(pts[0].$ * kwh / pts[0].k);
  for (var i = 0; i < pts.length - 1; i++){
    if (kwh >= pts[i].k && kwh <= pts[i + 1].k){
      var t = (kwh - pts[i].k) / (pts[i + 1].k - pts[i].k);
      return Math.round(pts[i].$ + t * (pts[i + 1].$ - pts[i].$));
    }
  }
  var a = kwh < pts[0].k ? pts[0] : pts[pts.length - 2];
  var b = kwh < pts[0].k ? pts[1] : pts[pts.length - 1];
  var slope = (b.$ - a.$) / (b.k - a.k);
  var base = kwh < pts[0].k ? pts[0] : pts[pts.length - 1];
  return Math.max(0, Math.round(base.$ + slope * (kwh - base.k)));
}

function planMonthly(p, kwh){
  var c = p.tiers[String(kwh)];
  if (c != null) return money(c, kwh);        // exact tier value straight from the CLI
  var exact = planMonthlyExact(p, kwh);
  if (exact != null && p.ec > 0) return exact; // exact formula at arbitrary usage
  return interpMonthly(p, kwh);                // last-resort estimate
}

// ---------------------------------------------------------------------------
// Break-even: the one-time exit fee to leave early vs. the recurring saving
// ---------------------------------------------------------------------------

function parseEtf(s){
  if (s == null) return 0;
  s = ('' + s).trim();
  if (!s || /^unknown/i.test(s) || s === '?') return null;
  var m = s.replace(/,/g, '').match(/[\d]+(?:\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}

// The CLI publishes cancellation_fee as a bare string ("175.00"), so it needs a
// currency sign before it goes on screen.
function fmtEtf(etfStr){
  var v = parseEtf(etfStr);
  if (v === null) return 'not stated';
  if (v <= 0) return 'none';
  return '$' + (v % 1 ? v.toFixed(2) : v.toFixed(0));
}

// A saving only holds while the rate is locked. Plenty of these plans run 5
// months, so projecting a full year would promise money the contract doesn't
// guarantee -- past the term you roll to a variable rate, which is how most
// people end up overpaying in the first place.
function totalSaveNote(saveMo, term){
  if (saveMo <= 0) return '';
  var t = parseFloat(term);
  if (!t || t < 2) return '';
  if (t >= 12)
    return 'About <b>$' + Math.round(saveMo * 12).toLocaleString() + '</b> over 12 months';
  return 'About <b>$' + Math.round(saveMo * t).toLocaleString() + '</b> over the ' + t +
         '-month term &mdash; then the rate ends and you shop again';
}

// Flags a parent company that owns more than one of the plans on screen, and a
// pick that shares a parent with the plan the shopper is already on (switching
// to it keeps them with the same company). Returns '' when the ranking really
// is as varied as it looks.
function sameCompanyNote(ranked, curProvider){
  var byParent = {};
  ranked.forEach(function(x){
    var f = familyOf(x.p.provider);
    if (!f) return;
    (byParent[f.parent] = byParent[f.parent] || {f: f, brands: []}).brands.push(x.p.provider);
  });

  var out = '';
  Object.keys(byParent).forEach(function(parent){
    var g = byParent[parent];
    // Dedupe BEFORE counting. Two plans from one brand is not a disclosure --
    // nobody is fooled that TriEagle and TriEagle are the same company. Only
    // distinct brand names hiding one owner are worth saying out loud.
    var uniq = g.brands.filter(function(b, i){ return g.brands.indexOf(b) === i; });
    if (uniq.length < 2) return;
    var names = uniq.map(function(b){ return '<b>' + esc(b) + '</b>'; });
    var list = names.length > 1
      ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1]
      : names[0];
    out +=
      '<div class="famwarn">' +
        '&#9888;&#65039; ' + list + ' are the same company &mdash; ' +
        (uniq.length === 2 ? 'both are brands' : 'all ' + uniq.length + ' are brands') +
        ' of <b>' + esc(parent) + '</b>. ' +
        'They look like competing offers, but picking between them is not really a choice. ' +
        (g.f.note ? '<span class="famnote">' + esc(g.f.note) + '</span>' : '') +
        (g.f.source ? '<a class="famsrc" href="' + esc(g.f.source) + '" target="_blank" rel="noopener">source</a>' : '') +
      '</div>';
  });

  // "Switching" to your own parent company is worth knowing before you click.
  var curF = curProvider ? familyOf(curProvider) : null;
  if (curF){
    // Same dedupe: three plans from one sibling brand is still one sibling.
    var seenBrand = {};
    var shared = ranked.filter(function(x){
      var f = familyOf(x.p.provider);
      if (!f || f.parent !== curF.parent) return false;
      var n = normProvider(x.p.provider);
      if (n === normProvider(curProvider) || seenBrand[n]) return false;
      seenBrand[n] = true;
      return true;
    });
    if (shared.length){
      var sn = shared.map(function(x){ return '<b>' + esc(x.p.provider) + '</b>'; });
      var sl = sn.length > 1 ? sn.slice(0, -1).join(', ') + ' and ' + sn[sn.length - 1] : sn[0];
      out +=
        '<div class="famwarn">' +
          '&#8505;&#65039; You are with <b>' + esc(curProvider) + '</b>, which is owned by <b>' +
          esc(curF.parent) + '</b> &mdash; and so ' + (sn.length > 1 ? 'are ' : 'is ') + sl +
          ' above. Moving there is a new rate, but the same company.' +
        '</div>';
    }
  }
  return out;
}

function breakevenNote(saveMo, etfStr, monthsRemaining){
  if (saveMo <= 0) return '';
  var etf = parseEtf(etfStr);
  if (etf === null) return "&#9888;&#65039; We couldn't read your current plan's exit fee &mdash; check your bill before switching, as it eats into the saving.";
  if (etf <= 0) return '&#10003; Your current plan has no exit fee, so you can switch now and keep the whole saving.';
  var be = etf / saveMo;
  if (monthsRemaining == null)
    return 'Leaving early costs a $' + Math.round(etf) + ' exit fee, earned back in about ' + be.toFixed(1) + ' months of savings. Worth it if you\'ve more than ~' + be.toFixed(1) + ' months left.';
  if (be <= monthsRemaining){
    var net = saveMo * monthsRemaining - etf;
    return '&#128077; <b>Worth switching now.</b> You earn back the $' + Math.round(etf) + ' exit fee in about ' + be.toFixed(1) + ' months, and come out ~$' + Math.round(net) + ' ahead over your remaining ' + fmtMonths(monthsRemaining) + ' months.';
  }
  var lost = etf - saveMo * monthsRemaining;
  return '&#9995; <b>Better to wait.</b> With only ' + fmtMonths(monthsRemaining) + ' months left, switching now costs ~$' + Math.round(lost) + ' after the $' + Math.round(etf) + ' exit fee. Switch for free when your contract ends.';
}

// ---------------------------------------------------------------------------
// Screens
// ---------------------------------------------------------------------------

function renderIntro(){
  state.step = 0; setDots();
  document.getElementById('subline').textContent =
    DATA.planCount + ' plans compared' + (DATA.zip ? ' near ' + DATA.zip : '') + ' · ' + DATA.generated;
  app.innerHTML =
    '<div class="card">' +
      '<h2 class="q">Let\'s find you a better deal.</h2>' +
      '<p class="lead">We\'ll show you the three cheapest electricity plans &mdash; in plain dollars per month, no fine print. Pick how you\'d like to start:</p>' +
      '<div class="choices">' +
        '<button class="choice" id="goEnter">' +
          '<span class="emoji">&#128221;</span>' +
          '<span><span class="t">Enter your info</span><br><span class="d">Type your real monthly usage (and current bill) for exact pricing</span></span>' +
        '</button>' +
        '<button class="choice" id="goUpload">' +
          '<span class="emoji">&#128196;</span>' +
          '<span><span class="t">Upload your bill</span><br><span class="d">Send us a PDF and we\'ll read the numbers off it for you</span></span>' +
        '</button>' +
        '<button class="choice" id="goEstimate">' +
          '<span class="emoji">&#10024;</span>' +
          '<span><span class="t">Estimate</span><br><span class="d">Not sure? Pick your home size and we\'ll estimate it for you</span></span>' +
        '</button>' +
      '</div>' +
      '<input type="file" id="billFile" accept="application/pdf,.pdf" class="hidden">' +
    '</div>';
  document.getElementById('goEnter').onclick = renderManualEntry;
  document.getElementById('goEstimate').onclick = renderUsage;
  var picker = document.getElementById('billFile');
  document.getElementById('goUpload').onclick = function(){ picker.click(); };
  picker.onchange = function(){ if (picker.files && picker.files[0]) uploadBill(picker.files[0]); };
}

// Upload a bill PDF and prefill the manual form from whatever came back.
// Anything the reader couldn't find stays blank for the user to fill in.
function uploadBill(file){
  state.step = 1; setDots();
  app.innerHTML =
    '<div class="card">' +
      '<h2 class="q">Reading your bill…</h2>' +
      '<p class="lead">Pulling out your usage and costs. This takes a few seconds.</p>' +
      '<div class="err" id="uErr"></div>' +
      '<div class="navrow"><button class="link" id="back">&larr; Cancel</button><span></span></div>' +
    '</div>';
  document.getElementById('back').onclick = renderIntro;

  fetch('/api/parse-bill', {method: 'POST', body: file,
                            headers: {'Content-Type': 'application/pdf'}})
    .then(function(r){ return r.json().then(function(j){ return {status: r.status, body: j}; }); })
    .then(function(res){
      if (!res.body || !res.body.ok){
        var msg = (res.body && res.body.error) || 'That bill could not be read.';
        renderManualEntry(msg + ' You can type the numbers in instead.');
        return;
      }
      var f = res.body.fields || {};
      if (f.usage_kwh != null) state.usage = f.usage_kwh;
      if (f.total_bill_dollars != null) state.manualBill = f.total_bill_dollars;
      if (f.exit_fee_dollars != null) state.manualEtf = f.exit_fee_dollars;
      if (f.months_remaining != null) state.manualMonths = f.months_remaining;
      // Kept so the results can say whether a "switch" stays inside the same
      // parent company as the plan they're already on.
      if (f.provider) state.manualProvider = f.provider;
      state.billSource = f;
      // Straight to the form, prefilled, so the user can check it before pricing.
      renderManualEntry(null, f);
    })
    .catch(function(err){
      renderManualEntry('The upload failed (' + esc(err && err.message ? err.message : err) +
                        '). You can type the numbers in instead.');
    });
}

function renderUsage(){
  state.step = 1; state.entry = 'estimate'; state.manualBill = null; setDots();
  var emojis = ['🏢', '🏡', '🏠'];
  var rows = '';
  DATA.presets.forEach(function(p, i){
    var lo = null;
    DATA.picks.forEach(function(pl){ var m = planMonthly(pl, p.kwh); if (m != null && (lo == null || m < lo)) lo = m; });
    rows +=
      '<button class="choice" data-kwh="' + p.kwh + '">' +
        '<span class="emoji">' + emojis[i % 3] + '</span>' +
        '<span><span class="t">' + esc(p.label) + '</span><br><span class="d">' + esc(p.sub) + ' · about ' + p.kwh.toLocaleString() + ' kWh/mo</span></span>' +
        (lo != null ? '<span class="from"><b>from $' + lo + '</b><span>per month</span></span>' : '') +
      '</button>';
  });
  app.innerHTML =
    '<div class="card">' +
      '<h2 class="q">How big is your home?</h2>' +
      '<p class="lead">A rough idea is fine &mdash; it sets the usage we price every plan at. Not sure? Most homes are Average.</p>' +
      '<div class="choices">' + rows + '</div>' +
      '<div class="navrow"><button class="link" id="back">&larr; Back</button><span></span></div>' +
    '</div>';
  document.getElementById('back').onclick = renderIntro;
  Array.prototype.forEach.call(document.querySelectorAll('.choice'), function(b){
    b.onclick = function(){ state.usage = parseInt(b.dataset.kwh, 10); renderResults(); };
  });
}

// notice: optional warning to show above the form (e.g. an upload that failed).
// prefill: the fields read off an uploaded bill, echoed back so the user can
// check them — a misread number is worse than a blank one if it goes unnoticed.
function renderManualEntry(notice, prefill){
  state.step = 1; state.entry = 'manual'; setDots();
  var uVal = state.usage != null ? state.usage : '';
  var bVal = state.manualBill != null ? state.manualBill : '';
  var eVal = state.manualEtf != null ? state.manualEtf : '';
  var mVal = state.manualMonths != null ? state.manualMonths : '';
  var lead = 'Grab your latest electricity bill. Only the first box is required &mdash; add your total to see what you\'d save.';
  var banner = '';
  if (typeof notice === 'string' && notice){
    banner = '<div class="curbanner">&#9888;&#65039; ' + esc(notice) + '</div>';
  } else if (prefill){
    var who = [prefill.provider, prefill.plan].filter(Boolean).join(' — ');
    var found = [];
    if (prefill.usage_kwh != null) found.push('usage');
    if (prefill.total_bill_dollars != null) found.push('bill total');
    if (prefill.exit_fee_dollars != null) found.push('exit fee');
    if (prefill.months_remaining != null) found.push('months left');
    banner = '<div class="curbanner">&#10003; Read from your bill' + (who ? ' (<b>' + esc(who) + '</b>)' : '') +
      ': ' + (found.length ? esc(found.join(', ')) : 'nothing usable') +
      '.<br><span style="color:var(--mid);font-size:.85em">Please check these against your bill before continuing — anything blank or wrong, just type over it.</span></div>';
    lead = 'We filled in what we could read. Correct anything that looks off.';
  }
  app.innerHTML =
    '<div class="card">' +
      '<h2 class="q">Enter your info</h2>' +
      '<p class="lead">' + lead + '</p>' +
      banner +
      '<div class="field">' +
        '<label>How many kWh did you use last month? <span class="hint">shown on your bill</span></label>' +
        '<div class="inrow"><input id="mUsage" type="number" inputmode="numeric" min="1" step="50" placeholder="e.g. 1200" value="' + uVal + '"><span class="unit">kWh</span></div>' +
      '</div>' +
      '<div class="field">' +
        '<label>What was your total bill? <span class="hint">optional &mdash; the whole amount you paid</span></label>' +
        '<div class="inrow"><span class="unit">$</span><input id="mBill" type="number" inputmode="decimal" min="0" step="1" placeholder="e.g. 180" value="' + bVal + '"></div>' +
      '</div>' +
      '<div class="optional">' +
        '<p class="why">Still under contract? These two let us work out whether leaving early is worth the exit fee. ' +
          'They\'re not printed on your bill &mdash; they live in your contract, so upload that and we\'ll read them, or just type them in.</p>' +
        '<button class="choice" id="goContract" type="button" style="margin-bottom:16px">' +
          '<span class="emoji">&#128220;</span>' +
          '<span><span class="t">Upload your contract</span><br><span class="d" id="cStatus">Optional &mdash; your contract or Electricity Facts Label (PDF)</span></span>' +
        '</button>' +
        '<input type="file" id="contractFile" accept="application/pdf,.pdf" class="hidden">' +
        '<div class="field">' +
          '<label>Your plan\'s exit fee <span class="hint">optional &mdash; on your contract as an early-termination fee</span></label>' +
          '<div class="inrow"><span class="unit">$</span><input id="mEtf" type="number" inputmode="decimal" min="0" step="10" placeholder="e.g. 150" value="' + eVal + '"></div>' +
        '</div>' +
        '<div class="field">' +
          '<label>Months left on your contract <span class="hint">optional &mdash; roughly is fine</span></label>' +
          '<div class="inrow"><input id="mMonths" type="number" inputmode="numeric" min="0" step="1" placeholder="e.g. 8" value="' + mVal + '"><span class="unit">months</span></div>' +
        '</div>' +
      '</div>' +
      '<div class="err" id="mErr"></div>' +
      '<button class="big" id="mGo">See my 3 best plans &nbsp;&rarr;</button>' +
      '<div class="navrow"><button class="link" id="back">&larr; Back</button><span></span></div>' +
    '</div>';
  document.getElementById('back').onclick = renderIntro;

  // Contract upload fills the two fields a bill can't supply. It writes into the
  // inputs rather than replacing the screen, so the user sees what it found and
  // can correct it.
  var cPicker = document.getElementById('contractFile');
  var cStatus = document.getElementById('cStatus');
  document.getElementById('goContract').onclick = function(){ cPicker.click(); };
  cPicker.onchange = function(){
    if (!cPicker.files || !cPicker.files[0]) return;
    cStatus.textContent = 'Reading your contract…';
    fetch('/api/parse-contract', {method: 'POST', body: cPicker.files[0],
                                  headers: {'Content-Type': 'application/pdf'}})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if (!j || !j.ok){
          cStatus.textContent = (j && j.error) || 'That contract could not be read — type the two below instead.';
          return;
        }
        var f = j.fields || {}, got = [];
        if (f.provider) state.manualProvider = f.provider;
        if (f.exit_fee_dollars != null){
          document.getElementById('mEtf').value = f.exit_fee_dollars;
          state.manualEtf = f.exit_fee_dollars;
          got.push(f.exit_fee_dollars > 0 ? 'exit fee $' + f.exit_fee_dollars : 'no exit fee');
        }
        if (f.months_remaining != null){
          document.getElementById('mMonths').value = f.months_remaining;
          state.manualMonths = f.months_remaining;
          if (f.contract_type !== 'month-to-month')
            got.push(fmtMonths(f.months_remaining) + ' months left');
        }
        // Month-to-month is the happiest answer there is: nothing to wait out
        // and nothing to pay. Say so, rather than showing "exit fee $0".
        if (f.contract_type === 'month-to-month'){
          cStatus.innerHTML = '&#10003; You\'re on a <b>month-to-month</b> plan' +
            (f.plan ? ' (' + esc(f.plan) + ')' : '') +
            ' with no exit fee &mdash; you can switch whenever you like.';
          return;
        }
        cStatus.textContent = got.length
          ? '✓ Read: ' + got.join(', ') + (f.plan ? ' (' + f.plan + ')' : '') + ' — check them below'
          : 'Nothing usable found in that PDF — type the two below instead.';
      })
      .catch(function(){ cStatus.textContent = 'The upload failed — type the two below instead.'; });
  };

  var go = function(){
    var err = document.getElementById('mErr');
    var u = parseInt(document.getElementById('mUsage').value, 10);
    if (!u || u <= 0){ err.textContent = 'Please enter how many kWh you used last month.'; return; }
    if (u > 100000){ err.textContent = 'That looks too high — enter monthly kWh (most homes are 500–3,000).'; return; }
    var bRaw = document.getElementById('mBill').value.trim();
    var b = bRaw === '' ? null : parseFloat(bRaw);
    if (b != null && (isNaN(b) || b < 0)){ err.textContent = 'Please enter your total bill as a dollar amount, or leave it blank.'; return; }
    var eRaw = document.getElementById('mEtf').value.trim();
    var e = eRaw === '' ? null : parseFloat(eRaw);
    if (e != null && (isNaN(e) || e < 0)){ err.textContent = 'Please enter your exit fee as a dollar amount, or leave it blank.'; return; }
    var mRaw = document.getElementById('mMonths').value.trim();
    var mo = mRaw === '' ? null : parseFloat(mRaw);
    if (mo != null && (isNaN(mo) || mo < 0)){ err.textContent = 'Please enter the months left on your contract, or leave it blank.'; return; }
    state.usage = u;
    state.manualBill = (b != null && !isNaN(b) && b > 0) ? b : null;
    state.manualEtf = (e != null && !isNaN(e)) ? e : null;
    state.manualMonths = (mo != null && !isNaN(mo)) ? mo : null;
    renderResults();
  };
  document.getElementById('mGo').onclick = go;
  ['mUsage', 'mBill', 'mEtf', 'mMonths'].forEach(function(id){
    document.getElementById(id).addEventListener('keydown', function(ev){
      if (ev.key === 'Enter'){ ev.preventDefault(); go(); }
    });
  });
  document.getElementById('mUsage').focus();
}

// How long the rate is locked. Sorting on price alone puts a 5-month teaser at
// the top of a list a shopper reads as "the best plan", so let them say what
// they actually want and rank inside it.
var TERM_BANDS = [
  {id: 'any',   label: 'Any length',  sub: 'Cheapest first',        test: function(){ return true; }},
  {id: 'short', label: '6 months',    sub: 'Or less — stay nimble', test: function(t){ return t <= 6; }},
  {id: 'year',  label: 'About a year', sub: '7–18 months',          test: function(t){ return t >= 7 && t <= 18; }},
  {id: 'long',  label: '2 years +',   sub: 'Lock it in',            test: function(t){ return t >= 19; }}
];

function termFilter(){
  var band = TERM_BANDS.filter(function(b){ return b.id === state.termPref; })[0] || TERM_BANDS[0];
  return function(p){
    var t = parseFloat(p.term);
    if (!t) return state.termPref === 'any';   // unknown term only survives "Any"
    return band.test(t);
  };
}

function renderResults(){
  state.step = 2; setDots();
  var kwh = state.usage;
  var keep = termFilter();
  var priced = DATA.picks
    .map(function(p){ return {p: p, m: planMonthly(p, kwh)}; })
    .filter(function(x){ return x.m != null; })
    .sort(function(a, b){ return a.m - b.m; });
  var inBand = priced.filter(function(x){ return keep(x.p); });
  // Never strand the user on an empty screen: fall back to the full list and
  // say so, rather than silently ignoring their choice.
  var fellBack = (!inBand.length && priced.length > 0);
  var ranked = (fellBack ? priced : inBand).slice(0, 3);

  // Current-plan comparison: a typed bill (from "Enter your info") takes
  // priority; otherwise use the plan the CLI marked as current (--current-efl).
  var cur = DATA.current;
  var curM = null, curEtf = null, months = state.manualMonths;
  var curNote = '';
  if (state.manualBill != null){
    curM = Math.round(state.manualBill);
    // Exit fee is only known if the user typed it.
    curEtf = state.manualEtf != null ? String(state.manualEtf) : '?';
    var allIn = (state.manualBill / kwh * 100);
    curNote = 'Your bill was about <b>$' + curM + '</b> for ' + kwh.toLocaleString() +
      ' kWh &mdash; roughly <b>' + allIn.toFixed(1) + '&cent;/kWh</b> all-in. ' +
      'Bills can include taxes and city fees these estimates leave out, so real savings may be a little smaller.';
  } else if (cur){
    curM = planMonthly(cur, kwh);
    curEtf = cur.etf;
    if (curM != null){
      curNote = 'You’re on <b>' + esc(cur.provider) + ' &mdash; ' + esc(cur.plan) +
        '</b>, about <b>$' + curM + '/mo</b> at this usage.';
    }
  }

  // The prototype's verdict block: the answer in one line, with what you pay
  // today beside it. Savings are stated per month, not per year -- a year of
  // savings on a 5-month plan is money the contract doesn't guarantee.
  var banner = '';
  if (curM != null && ranked.length){
    var bestSave = curM - ranked[0].m;
    var cheaper = priced.filter(function(x){ return x.m < curM; }).length;
    var nowCard =
      '<div class="vnow">' +
        '<div class="eyebrow">You pay now</div>' +
        '<div class="vamt">$' + curM + '<span>/mo</span></div>' +
      '</div>';
    if (bestSave > 0){
      banner =
        '<div class="verdict good">' +
          '<div class="vmain">' +
            '<h2 class="vhead">Good news &mdash; you could save $' + bestSave + ' a month</h2>' +
            '<p class="vsub">' + (cheaper === 1 ? 'We found 1 plan' : 'We found ' + cheaper + ' plans') +
            ' cheaper than your current one. ' +
            (ranked.length > 1 ? 'Here are the best ' + (ranked.length === 2 ? 'two' : 'three') + '.' : 'Here it is.') +
            '</p>' +
          '</div>' + nowCard +
        '</div>';
    } else {
      // No saving is still an answer, and a useful one -- don't dress it in green.
      banner =
        '<div class="verdict flat">' +
          '<div class="vmain">' +
            '<h2 class="vhead">You’re already on a good deal</h2>' +
            '<p class="vsub">Nothing here beats what you pay now. Worth checking again when your contract ends.</p>' +
          '</div>' + nowCard +
        '</div>';
    }
  }
  if (curNote) banner += '<div class="curbanner">' + curNote + '</div>';

  var labels = ['Best deal', '2nd best', '3rd best'];
  var cards = '';
  ranked.forEach(function(x, i){
    var p = x.p, m = x.m;
    var green = (parseFloat(p.rnw) >= 99) ? '<span class="leaf">🌱 100% green</span>' : (p.rnw && p.rnw !== '?' ? esc(p.rnw) + '% renewable' : '');
    var saveHtml = '', beHtml = '', totHtml = '';
    if (curM != null){
      var save = curM - m;
      if (save > 0){
        saveHtml = '<div class="save">Save about $' + save + '/mo vs your plan</div>';
        var tot = totalSaveNote(save, p.term);
        if (tot) totHtml = '<div class="tot">' + tot + '</div>';
        var note = breakevenNote(save, curEtf, months);
        if (note) beHtml = '<div class="be">' + note + '</div>';
      } else {
        saveHtml = '<div class="save" style="background:transparent;color:var(--mid)">About the same as your current plan</div>';
      }
    }
    // Same fallback the CLI's own HTML uses for its 🛒 / 🌐 icons: the PUCT
    // enrolment link first (populated on ~99% of rows), then the provider's
    // site, then the EFL as a last resort. The button has to say which one it
    // is -- "Choose this plan" landing on a PDF is a promise the link doesn't
    // keep, and that's what it did while enroll_url went unexported.
    var link = p.enroll_url || p.website_url || p.facts_url || '';
    var cta = p.enroll_url  ? 'Sign up for this plan'
            : p.website_url ? 'Go to ' + esc(p.provider)
            : 'Read the plan’s fact sheet';
    var act = link
      ? '<div class="act"><a href="' + esc(link) + '" target="_blank" rel="noopener">' +
          cta + ' &nbsp;&rarr;</a>' +
        (p.enroll_url && p.facts_url
          ? '<a class="actsub" href="' + esc(p.facts_url) + '" target="_blank" rel="noopener">' +
            'or read the fact sheet first</a>'
          : '') +
        '</div>'
      : '';
    // Name the owner on every card that has one, not just the flagged clashes.
    var fam = familyOf(p.provider);
    var owner = (fam && normProvider(fam.parent) !== normProvider(p.provider))
      ? '<span class="owner">part of ' + esc(fam.parent) + '</span>' : '';
    cards +=
      '<div class="plan n' + (i + 1) + (i === 0 ? ' best' : '') + '">' +
        '<span class="badge">' + labels[i] + '</span>' +
        '<div class="prov">' + esc(p.provider) + owner + '</div>' +
        '<div class="name">' + esc(p.plan) + '</div>' +
        '<div class="price"><span class="amt">$' + m + '</span><span class="per">/ month at ' + kwh.toLocaleString() + ' kWh</span>' +
          '<span class="rate">' + (m / kwh * 100).toFixed(1) + '&cent;/kWh all-in</span></div>' +
        saveHtml + totHtml +
        '<div class="meta"><span>&#128274; Locked in for ' + (p.term || '?') + ' months</span>' + (green ? '<span>' + green + '</span>' : '') + '<span>Exit fee: ' + fmtEtf(p.etf) + '</span></div>' +
        beHtml +
        act +
      '</div>';
  });
  if (!cards) cards = '<div class="card"><p class="lead">No plans could be priced at this usage. Try another home size.</p></div>';

  var chips = TERM_BANDS.map(function(b){
    var n = priced.filter(function(x){ return b.test(parseFloat(x.p.term) || 0); }).length;
    return '<button class="chip' + (b.id === state.termPref ? ' on' : '') + '" data-term="' + b.id + '"' +
           (n ? '' : ' disabled') + '>' + b.label +
           '<span class="chipsub">' + (n ? b.sub : 'none here') + '</span></button>';
  }).join('');

  var fellBackMsg = fellBack
    ? '<div class="famwarn">No plans here match that contract length, so these are the cheapest of any length.</div>'
    : '';

  app.innerHTML =
    '<div class="card" style="background:transparent;border:0;box-shadow:none;padding:0">' +
      '<h2 class="q">Your 3 best plans</h2>' +
      '<p class="lead">Cheapest first, priced for a ' + kwh.toLocaleString() + ' kWh month. Tap a plan to sign up on the provider’s site.</p>' +
      '<div class="termpick"><span class="termlab">How long do you want the rate locked?</span>' +
        '<div class="chips">' + chips + '</div></div>' +
      banner +
      fellBackMsg +
      sameCompanyNote(ranked, state.manualProvider || (cur && cur.provider)) +
      '<div class="rank">' + cards + '</div>' +
      '<div class="navrow"><button class="link" id="back">' + (state.entry === 'manual' ? '&larr; Edit my info' : '&larr; Change home size') + '</button><button class="link" id="restart">Start over</button></div>' +
    '</div>';
  Array.prototype.forEach.call(document.querySelectorAll('.chip'), function(el){
    el.onclick = function(){ state.termPref = el.getAttribute('data-term'); renderResults(); };
  });
  document.getElementById('back').onclick = (state.entry === 'manual' ? renderManualEntry : renderUsage);
  document.getElementById('restart').onclick = renderIntro;
}

function renderMessage(title, body){
  document.getElementById('subline').textContent = '';
  app.innerHTML = '<div class="card"><h2 class="q">' + title + '</h2><p class="lead">' + body + '</p></div>';
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

// Wired up before any fetch, so the toggle still works on a page that failed to
// load its data.
initTheme();

// Ownership data is a nice-to-have: if it fails to load the page still ranks
// plans correctly, it just can't tell you who owns whom.
fetch('/static/brand_families.json')
  .then(function(r){ return r.ok ? r.json() : null; })
  .catch(function(){ return null; })
  .then(function(fam){
    FAMILIES = ((fam && fam.families) || []).map(function(f){
      f._brands = (f.brands || []).map(normProvider);
      return f;
    });
    return fetch('/api/plans');
  })
  .then(function(r){ return r.json(); })
  .then(function(raw){
    if (!raw || (raw._source && raw._source.ok === false)){
      var msg = (raw && raw._source && raw._source.message) || 'No plan data is available yet.';
      renderMessage('No plan data yet', esc(msg) +
        '<br><br>Generate it from the repo root with:<br><code>python3 efl_compare.py --zip YOUR_ZIP --json plans_latest.json</code>');
      return;
    }
    DATA = buildData(raw);
    if (!DATA.picks.length){
      renderMessage('No plans to compare', 'The data file loaded, but it contains no plans to rank.');
      return;
    }
    document.getElementById('foot').innerHTML =
      '<a class="tablelink" href="/table">View table comparison &rarr;</a>' +
      '<p>Prices are estimated monthly bills including delivery charges, at the usage you pick. ' +
      'Taxes excluded. Always confirm on the provider’s official Electricity Facts Label before enrolling.</p>';
    renderIntro();
  })
  .catch(function(err){
    renderMessage('Couldn’t load the plan data', 'The server didn’t return usable data (' + esc(err && err.message ? err.message : err) + '). Is serve.py still running?');
  });
