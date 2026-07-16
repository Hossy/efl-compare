# Mustang — design and style guide

Adapted from the `Mustang.dc.html` prototype (`web/.tmp/html.zip`). Every token
lives in [`static/theme.css`](static/theme.css); this file explains the intent
so the next change extends the system instead of fighting it.

The feeling to protect: **a kitchen table, not a trading floor.** Warm off-white
paper, one confident orange, generous radii, plain sentences. Most comparison
sites look like a bank. This one shouldn't.

---

## Type

| Role | Face | Weights | Where |
|---|---|---|---|
| Headings, numbers | **Sora** | 700, 800 | `h1`–`h3`, prices, savings, badges |
| Body, UI | **Karla** | 400, 500, 700 | prose, labels, buttons, inputs |

Headings are tight: `letter-spacing:-.02em`, `-.03em` on the big `h1`, and
`line-height:1.08`. Sora is geometric and gets loose and airy at display sizes
without it.

Money and rates carry `font-variant-numeric:tabular-nums` (the `.num` class, and
baked into `.price .amt` / `.price .rate`). Figures line up column to column, and
a number that changes doesn't nudge its neighbours sideways.

**Fonts are self-hosted** in `static/fonts/` (latin subset, 7 files, ~170 KB).
The prototype links `fonts.googleapis.com`; doing that here would hand every
visitor's IP to a third party to render a page that otherwise phones nobody, and
would break the site offline. If you add a weight, download it rather than
linking it.

## Colour

Semantic names, never literals. Reach for `var(--brand)`, not `#D96E1E`.

| Token | Light | Meaning |
|---|---|---|
| `--bg` | `#FDFDFB` | The page. Warm off-white — the whole mood rests on this not being blue-grey. |
| `--card` | `#FFFFFF` | Raised surfaces. |
| `--surface-alt` | `#F7F5F0` | Full-width bands that break up a long page. |
| `--surface-sunk` | `#FBFAF7` | Rows inset *within* a card. |
| `--ink` | `#20242B` | Body text. Near-black, never pure. |
| `--mid` | `#5A6070` | Secondary prose. |
| `--faint` | `#9CA1AD` | Eyebrows, captions, disabled. |
| `--line` / `--line-strong` | `#EEECE6` / `#E2E0DB` | Borders; the stronger one for inputs and secondary buttons. |
| `--brand` / `--brand-dark` | `#D96E1E` / `#B65812` | Actions, top pick, focus. |
| `--warn-bg` | `#FBF1E7` | Brand tint: badges, icon chips, notices. |
| `--good` / `--good-bg` | `#2E7D4F` / `#E7F3EC` | **Money saved only.** |
| `--dark` | `#20242B` | Inverted CTA panels. |
| `--cat-blue` / `--cat-gold` | `#4A6FA5` / `#B08A3E` | Categorical (avatars, provider marks). Never meaning. |

Two rules worth keeping:

- **Green means money.** Savings, "no exit fee", green plans. Not "success",
  not "valid", not a generic tick.
- **Orange means *we* chose this.** The top-pick card takes the brand border and
  the only filled button on screen; runners-up get outline buttons. The prototype
  is deliberate here — if everything is emphasised, nothing is.

## Shape and depth

- Radius: `--r-pill` (999px) badges and chips · `--r-lg` (24px) cards ·
  `--r-md` (20px) plan cards · `--r-sm` (14px) buttons and notices ·
  `--r-xs` (11px) inputs and small controls.
- Borders are `1.5px`, not `1px`. At 1px this palette's lines disappear.
- Shadows are broad and soft (`--shadow`: 50px blur at 7% opacity), never a hard
  drop. `--shadow-brand` sits under the primary button; `--shadow-card` lifts the
  top pick.

## The mark

`static/images/logo.svg` is the source artwork. `logo-mark.svg` is what the app
uses, derived from it by dropping two things: the baked-in `#F5F6F7` rectangle
(a cool-grey plate that clashes with the warm page and would be a light box in
dark mode) and the fixed `1685pt` size (the `viewBox` alone scales to any box).

It renders through `-webkit-mask`/`mask` with `background:currentColor`, not as
an `<img>` — so it takes the theme's colour while staying an external, cached
file.

The prototype's nav puts a letter in a rounded orange square. That doesn't work
with the real artwork: the mustang's mane is fine line-art, and at 24px inside a
square it packed down to an unreadable smudge. So the horse *is* the mark, at
46×32, and the orange moved from the plate onto the glyph. If a compact square
mark is ever needed (a favicon, an app icon), it needs a redrawn glyph, not this
one scaled down.

## Dark mode

The prototype is light-only. The dark scheme in `theme.css` is derived from the
same hues, because the app already honoured `prefers-color-scheme` and dropping
that to match a mockup would be a regression for anyone who set it.

**The palette's spine is warm surfaces, warm lines, cool-grey text — and that
holds in dark too.** This is the one rule to not break. It's easy to miss
because the light theme's *text* is cool (`--mid` `#5A6070`, `--faint`
`#9CA1AD` both lean blue) while everything under it is warm. That tension is the
prototype's, and it's deliberate.

Three things needed real decisions rather than inversion:

- `--brand` lifts to `#E8853C`. `#D96E1E` on a dark ground goes muddy and fails
  contrast.
- `--ink` is a *warm* white (`#F4F2ED`). A blue-white fights the orange.
- **Tints are alpha, not darkened hexes.** Darkening the light cream `#FBF1E7`
  gave `#2C2117` — saturated enough to read brown, dark enough to read mud.
  `--warn-bg` in dark is `rgba(232,133,60,.12)`: the brand at low alpha over
  whatever sits behind it. Same for `--good-bg`. If you add a tint, do it this
  way.

### Measure warmth before touching `--d-*`

Two passes got this wrong, and the same check would have caught both. Take
**R−B** (red channel minus blue) of any surface: positive is warm, negative is
cool. Light runs `--bg` **+2**, `--surface-alt` **+7**, `--line` **+8**.

The dark surfaces were first built from the prototype's `#20242B` — but that
colour is a small accent panel *inside* a warm page, not a surface system.
Promoted to a page background it made every dark surface blue-grey (**−11**)
while the brand tint over it composited warm (**+12**). Warm on cool: a
mud-brown box dropped on a fintech-grey page. The tint was never the fault; the
ground under it was.

The dark surfaces now run **+4 to +9**, and `--warn-bg` lands **+20** warmer than
its surface — the exact step it takes from `--card` in light. Keep it that way:
if a dark surface goes negative, the orange has nothing to sit on.

The dark palette is declared once as `--d-*` values and *mapped* onto the live
token names by two selectors (the media query and `[data-theme="dark"]`). Each
value therefore has one home; only the mapping list is repeated. Keep the two
mapping lists identical.

## Theme toggle

The header button cycles **Auto → Light → Dark**, and the choice persists in
`localStorage` under `efl-theme`.

- **Auto is the resting state** and follows the OS. It gets its own icon (a
  half-filled circle) so "following your system" is a visible state rather than
  something you infer from the colours.
- An explicit choice outranks the OS **in both directions** — hence
  `:root:not([data-theme="light"])` on the media query, so someone on a dark OS
  can actually get to the designed light palette and have it stick.
- The stored choice is applied by a tiny inline script in `<head>`, before first
  paint. Reading `localStorage` after the stylesheet paints means a flash of the
  wrong theme on every load; that's why it's inline rather than in `wizard.js`.

## Writing

The visual style is only half of it; the prototype's voice is the other half.

- Plain sentences. "Your energy bill is probably too high. Let's fix that."
- Concrete over abstract: "$258/mo", not "significant savings".
- Name the catch in one sentence, in the same size as everything else. Never
  bury it in grey 11px.
- Second person, active. "You can switch whenever you like."

---

## Not built yet

**The landing page.** The prototype has a full one — hero, "Easier than packing
a lunchbox" three-step band, promises section, dark CTA panel. Only the design
system was adopted; the page wasn't. It's wanted, and it's deliberately parked.

It isn't blocked on effort. It's blocked on having true things to put in it: the
prototype's landing copy is placeholder, and most of the trust-building content
on it is false for this app (see the table below). Building it means writing the
copy from scratch, not adapting the mockup's.

**Vendor reviews.** Wanted, to build confidence in the top three. Parked until
real data exists — there is no rating field anywhere in the PUCT feed. Do not
generate, estimate, or infer them. The honest source is
[PUCT complaint statistics](https://www.puc.texas.gov/industry/electric/directories/rep/):
official, per-provider, citable.

---

## What was deliberately not copied

The prototype is a **visual** reference. Its *content* is placeholder, and some
of it would be false — or a lie — if shipped as-is.

| In the prototype | Why it's not here |
|---|---|
| "★ Rated 4.8 by 12,000+ households" | No such rating exists. |
| Hero testimonials — "Katie M. · Dallas · $412/yr", captioned **"Real switches from the last 30 days"** | Invented people, invented savings, captioned as real. |
| "Jenna R., Mom of three, Austin TX · saved $372/yr" | Invented. |
| "Most families save $340 a year" | No basis. The real per-user figure is computed and shown instead. |
| "Your bill is deleted after we read it… within 24 hours" | Understates the truth — see below. |
| "Bank-level encryption" | Means nothing; nothing here is measured against it. |
| "Switch for me →", "we'll handle the switch", "we'll email you when it's live" | The app cannot switch anyone. It links to the provider's own EFL. |
| "How we get paid: the supplier pays us a referral fee" | No such arrangement. |
| "No spam", "No sales calls — promise" | No email or phone number is ever collected. |
| "Mustang Energy" as a legal entity, © footer | Not a company. |

This matters more here than on most projects. The point of this tool is that the
top three results are often **one company wearing three brand names**, and it
says so out loud. A page that opens with fabricated five-star reviews to earn
that trust has spent the credibility it was about to ask for.

If social proof is wanted later, the honest source is
[PUCT complaint statistics](https://www.puc.texas.gov/industry/electric/directories/rep/) —
official, per-provider, and citable. Not generated.

### On the privacy claim specifically

Don't copy "your bill is deleted after we read it". It's **weaker than what the
code actually does**, and it invites a promise nobody is keeping.

`serve.py` reads the upload into memory and hands the bytes straight to
`fitz.open(stream=…)`. The PDF is **never written to disk** — there is nothing to
delete, and no retention window to honour. The accurate line is:

> Your bill is read in memory and never stored.

(`web/uploads/` is git-ignored and holds test documents put there by hand. The
server neither writes to it nor reads from it.)
