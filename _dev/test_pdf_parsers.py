#!/usr/bin/env python3
"""
test_pdf_parsers.py — Empirical comparison of pdfplumber vs PyMuPDF
across all cached EFL PDFs.

For each PDF:
  - Extract text with both parsers
  - Run _find_energy_charge() and _find_base_charge() on each
  - Compare: did one succeed where the other failed?
  - Report character count, text quality, and parse outcomes
  - Flag cases where results differ

Output: test_pdf_parsers_report.txt
"""

import pathlib
import sys
import time
import re
import io
import contextlib

ROOT = pathlib.Path(__file__).parent.parent   # project root
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pdfplumber
import fitz          # PyMuPDF

import efl_compare

CACHE = ROOT / "efls_cache"
OUT   = pathlib.Path(__file__).parent / "test_pdf_parsers_report.txt"  # report stays in _dev/


# ── PyMuPDF extraction ──────────────────────────────────────────────────────

def extract_pymupdf(path: pathlib.Path) -> str:
    """Extract text from PDF using PyMuPDF, using spatial (reading) order."""
    doc = fitz.open(str(path))
    pages = []
    for page in doc:
        # sort=True uses PyMuPDF's reading-order sort (left-to-right, top-to-bottom)
        pages.append(page.get_text("text", sort=True))
    doc.close()
    return "\n".join(pages)


def extract_pdfplumber(path: pathlib.Path) -> str:
    """Extract text using pdfplumber (stream order)."""
    with contextlib.redirect_stderr(io.StringIO()):
        with pdfplumber.open(str(path)) as doc:
            return "\n".join(pg.extract_text() or "" for pg in doc.pages)


# ── Helpers ─────────────────────────────────────────────────────────────────

def parse_results(text: str) -> dict:
    ec = efl_compare._find_energy_charge(text)
    bc = efl_compare._find_base_charge(text)
    bundled = efl_compare._detect_tdu_bundled(text)
    return {"ec": ec, "bc": bc, "bundled": bundled}


def text_quality(text: str) -> dict:
    """Rough quality metrics for extracted text."""
    lines = [l for l in text.splitlines() if l.strip()]
    # Count lines containing a ¢ or $ value (pricing lines)
    pricing_lines = sum(
        1 for l in lines
        if re.search(r"[\$¢][\s\d]|[\d.]+\s*[¢c]|per\s+kwh", l, re.I)
    )
    return {
        "chars":         len(text),
        "lines":         len(lines),
        "pricing_lines": pricing_lines,
        # Fraction of chars that are printable ASCII (proxy for garbling)
        "ascii_pct":     sum(1 for c in text if 32 <= ord(c) < 128) / max(len(text), 1),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    pdfs = sorted(CACHE.glob("*.pdf"))
    print(f"Comparing pdfplumber vs PyMuPDF on {len(pdfs)} PDFs ...\n")

    results = []

    t0 = time.perf_counter()
    for pdf in pdfs:
        try:
            t_plumb = time.perf_counter()
            text_plumb = extract_pdfplumber(pdf)
            t_plumb = time.perf_counter() - t_plumb

            t_mupdf = time.perf_counter()
            text_mupdf = extract_pymupdf(pdf)
            t_mupdf = time.perf_counter() - t_mupdf

            r_plumb = parse_results(text_plumb)
            r_mupdf = parse_results(text_mupdf)
            q_plumb = text_quality(text_plumb)
            q_mupdf = text_quality(text_mupdf)

            results.append({
                "name":    pdf.name,
                "t_plumb": t_plumb,
                "t_mupdf": t_mupdf,
                "plumb":   r_plumb,
                "mupdf":   r_mupdf,
                "qp":      q_plumb,
                "qm":      q_mupdf,
                "text_plumb": text_plumb,
                "text_mupdf": text_mupdf,
                "error":   None,
            })
        except Exception as e:
            results.append({"name": pdf.name, "error": str(e)})

        sys.stdout.write(f"\r  {len(results):>3}/{len(pdfs)}")
        sys.stdout.flush()

    elapsed = time.perf_counter() - t0
    print(f"\n  Done in {elapsed:.1f}s\n")

    ok = [r for r in results if not r.get("error")]

    # ── Summary stats ─────────────────────────────────────────────────────────
    both_ec     = sum(1 for r in ok if r["plumb"]["ec"] is not None and r["mupdf"]["ec"] is not None)
    plumb_only  = sum(1 for r in ok if r["plumb"]["ec"] is not None and r["mupdf"]["ec"] is None)
    mupdf_only  = sum(1 for r in ok if r["plumb"]["ec"] is None     and r["mupdf"]["ec"] is not None)
    neither_ec  = sum(1 for r in ok if r["plumb"]["ec"] is None     and r["mupdf"]["ec"] is None)

    both_bc     = sum(1 for r in ok if r["plumb"]["bc"] > 0 and r["mupdf"]["bc"] > 0)
    plumb_bc    = sum(1 for r in ok if r["plumb"]["bc"] > 0 and r["mupdf"]["bc"] == 0)
    mupdf_bc    = sum(1 for r in ok if r["plumb"]["bc"] == 0 and r["mupdf"]["bc"] > 0)
    neither_bc  = sum(1 for r in ok if r["plumb"]["bc"] == 0 and r["mupdf"]["bc"] == 0)

    ec_agree    = sum(1 for r in ok
                      if r["plumb"]["ec"] is not None and r["mupdf"]["ec"] is not None
                      and abs(r["plumb"]["ec"] - r["mupdf"]["ec"]) < 0.0001)

    ec_disagree = sum(1 for r in ok
                      if r["plumb"]["ec"] is not None and r["mupdf"]["ec"] is not None
                      and abs(r["plumb"]["ec"] - r["mupdf"]["ec"]) >= 0.0001)

    avg_t_plumb = sum(r["t_plumb"] for r in ok) / len(ok) if ok else 0
    avg_t_mupdf = sum(r["t_mupdf"] for r in ok) / len(ok) if ok else 0

    lines = []
    lines.append("=" * 80)
    lines.append("  PDFPLUMBER vs PYMUPDF — EMPIRICAL COMPARISON")
    lines.append(f"  PDFs tested: {len(pdfs)}  |  Successful: {len(ok)}  |  Errors: {len(results)-len(ok)}")
    lines.append("=" * 80)
    lines.append("")
    lines.append("ENERGY CHARGE EXTRACTION")
    lines.append(f"  Both found ec:         {both_ec}")
    lines.append(f"  pdfplumber only:       {plumb_only}")
    lines.append(f"  PyMuPDF only:          {mupdf_only}")
    lines.append(f"  Neither found ec:      {neither_ec}")
    lines.append(f"  Both agree on value:   {ec_agree}")
    lines.append(f"  Both found, disagree:  {ec_disagree}")
    lines.append("")
    lines.append("BASE CHARGE EXTRACTION")
    lines.append(f"  Both found bc>0:       {both_bc}")
    lines.append(f"  pdfplumber only:       {plumb_bc}")
    lines.append(f"  PyMuPDF only:          {mupdf_bc}")
    lines.append(f"  Neither found bc>0:    {neither_bc}")
    lines.append("")
    lines.append("SPEED")
    lines.append(f"  pdfplumber avg:        {avg_t_plumb*1000:.1f}ms/pdf")
    lines.append(f"  PyMuPDF avg:           {avg_t_mupdf*1000:.1f}ms/pdf")
    lines.append(f"  PyMuPDF speedup:       {avg_t_plumb/avg_t_mupdf:.1f}×")

    # ── Detailed differences ──────────────────────────────────────────────────
    lines.append("")
    lines.append("─" * 80)
    lines.append("  CASES WHERE PARSERS DISAGREE ON ENERGY CHARGE")
    lines.append("─" * 80)

    for r in ok:
        p_ec = r["plumb"]["ec"]
        m_ec = r["mupdf"]["ec"]
        if p_ec is None and m_ec is None:
            continue
        if p_ec is not None and m_ec is not None and abs(p_ec - m_ec) < 0.0001:
            continue

        label = (
            "MUPDF_WINS" if p_ec is None and m_ec is not None else
            "PLUMB_WINS" if p_ec is not None and m_ec is None else
            "VALUE_DIFF"
        )
        lines.append(f"\n[{label}]  {r['name'][:60]}")
        lines.append(f"  pdfplumber:  ec={p_ec}  bc={r['plumb']['bc']}")
        lines.append(f"  PyMuPDF:     ec={m_ec}  bc={r['mupdf']['bc']}")
        lines.append(f"  chars: plumb={r['qp']['chars']}  mupdf={r['qm']['chars']}")
        lines.append(f"  pricing_lines: plumb={r['qp']['pricing_lines']}  mupdf={r['qm']['pricing_lines']}")

    lines.append("")
    lines.append("─" * 80)
    lines.append("  CASES WHERE PARSERS DISAGREE ON BASE CHARGE")
    lines.append("─" * 80)
    for r in ok:
        p_bc = r["plumb"]["bc"]
        m_bc = r["mupdf"]["bc"]
        if abs(p_bc - m_bc) < 0.01:
            continue
        lines.append(f"\n[BC_DIFF]  {r['name'][:60]}")
        lines.append(f"  pdfplumber:  ec={r['plumb']['ec']}  bc={p_bc}")
        lines.append(f"  PyMuPDF:     ec={r['mupdf']['ec']}  bc={m_bc}")

    lines.append("")
    lines.append("─" * 80)
    lines.append("  TEXT QUALITY: PROBLEMATIC EFLs (low pricing_lines OR high garbling)")
    lines.append("─" * 80)
    for r in ok:
        qp = r["qp"]; qm = r["qm"]
        if qp["pricing_lines"] < 3 or qm["pricing_lines"] < 3:
            lines.append(f"\n  {r['name'][:60]}")
            lines.append(f"    pdfplumber: {qp['chars']}ch  {qp['pricing_lines']} pricing_lines  ascii={qp['ascii_pct']:.2%}")
            lines.append(f"    PyMuPDF:    {qm['chars']}ch  {qm['pricing_lines']} pricing_lines  ascii={qm['ascii_pct']:.2%}")

    lines.append("")
    lines.append("─" * 80)
    lines.append("  SIDE-BY-SIDE: CHAMPION ENERGY (multi-column showcase)")
    lines.append("─" * 80)
    for r in ok:
        if "champion" in r["name"].lower() and "saver-12" in r["name"].lower():
            lines.append(f"\n  File: {r['name']}")
            lines.append("\n  --- pdfplumber ---")
            lines.append(r["text_plumb"][:1200])
            lines.append("\n  --- PyMuPDF ---")
            lines.append(r["text_mupdf"][:1200])
            break

    lines.append("")
    lines.append("─" * 80)
    lines.append("  SIDE-BY-SIDE: TRIEAGLE (split column showcase)")
    lines.append("─" * 80)
    for r in ok:
        if "trieagle" in r["name"].lower() and "simple_savings_12" in r["name"].lower():
            lines.append(f"\n  File: {r['name']}")
            lines.append("\n  --- pdfplumber ---")
            lines.append(r["text_plumb"][:1200])
            lines.append("\n  --- PyMuPDF ---")
            lines.append(r["text_mupdf"][:1200])
            break

    # ── Overall recommendation ────────────────────────────────────────────────
    lines.append("")
    lines.append("─" * 80)
    lines.append("  OVERALL SCORE")
    lines.append("─" * 80)
    plumb_wins_total = plumb_only
    mupdf_wins_total = mupdf_only
    lines.append(f"  pdfplumber exclusively solves: {plumb_wins_total} plan(s)")
    lines.append(f"  PyMuPDF exclusively solves:    {mupdf_wins_total} plan(s)")
    lines.append(f"  PyMuPDF base charge wins:      {mupdf_bc} plan(s)")
    lines.append(f"  pdfplumber base charge wins:   {plumb_bc} plan(s)")
    speedup = avg_t_plumb / avg_t_mupdf if avg_t_mupdf > 0 else 0
    lines.append(f"  Speed advantage:               PyMuPDF {speedup:.1f}× faster")

    report = "\n".join(lines)
    OUT.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n  Full report saved to: {OUT}")


if __name__ == "__main__":
    main()
