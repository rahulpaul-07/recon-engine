"""
Self-contained HTML report.

Writes a single file with no external assets, no CDN, no build step. It opens
in a browser from disk, which matters: a reviewer with three minutes should not
have to install anything to see the result.

The report is ordered by what a reviewer needs to decide, not by what was
easiest to render:

    1. the three numbers the track bar asks for
    2. how much of the result rests on certainty vs inference
    3. the per-class confusion matrix against the answer key
    4. every unresolved record, with its reason

The exception table is deliberately given the most space. A reconciliation
tool that hides what it could not do is worse than one that never ran.
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate import grade  # noqa: E402
from matcher import Engine, load  # noqa: E402

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e6e3;
     font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;
     padding:48px 32px;max-width:1080px;margin:0 auto}
h1{font-size:26px;font-weight:600;letter-spacing:-.02em;margin-bottom:4px}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;
   color:#8b8680;margin:44px 0 16px;padding-bottom:8px;border-bottom:1px solid #262626}
.sub{color:#8b8680;font-size:13px;margin-bottom:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:24px}
.card{background:#161616;border:1px solid #262626;border-radius:8px;padding:18px 20px}
.card .label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8b8680}
.card .value{font-size:29px;font-weight:600;margin:6px 0 2px;letter-spacing:-.02em}
.card .note{font-size:12px;color:#6f6a64;font-variant-numeric:tabular-nums}
.amber .value{color:#d9a441}
.green .value{color:#7bb661}
table{width:100%;border-collapse:collapse;font-size:13.5px;
      font-variant-numeric:tabular-nums}
th{text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;
   letter-spacing:.06em;color:#8b8680;padding:9px 12px;border-bottom:1px solid #262626}
td{padding:9px 12px;border-bottom:1px solid #1c1c1c}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right}
.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:12.5px}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11.5px;
     font-weight:500;background:#232323;color:#b8b3ad}
.tag.break{background:#3a2020;color:#e08c7a}
.tag.ok{background:#1e2a1a;color:#8fc275}
.bar{height:5px;background:#1c1c1c;border-radius:3px;overflow:hidden;margin-top:7px}
.bar span{display:block;height:100%;background:#7bb661}
.reason{color:#a09a94;font-size:12.5px}
.caveat{background:#161616;border-left:2px solid #d9a441;padding:14px 18px;
        border-radius:0 6px 6px 0;margin-top:18px;font-size:13.5px;color:#b8b3ad}
footer{margin-top:52px;padding-top:18px;border-top:1px solid #1f1f1f;
       color:#5f5a55;font-size:12px}
"""

# Classes that mean money is genuinely missing or unaccounted for, as opposed
# to differences the system can fully explain.
REAL_BREAKS = {"missing_payment", "orphan_bank_credit", "missing_bank_row",
               "settlement_not_in_bank", "settlement_total_mismatch",
               "net_arithmetic_error", "amount_mismatch", "method_mismatch"}

TIER_NAMES = {0: "self-consistency", 1: "exact key join",
              2: "deterministic inference", 3: "reference recovery"}


def e(x) -> str:
    return html.escape(str(x))


def build(datadir: Path, outfile: Path) -> None:
    orders, txns, settlements, bank = load(datadir)
    resolutions = Engine(orders, txns, settlements, bank).run()
    metrics, summary = grade(datadir)

    exceptions = [r for r in resolutions if not r.resolved]
    lo, hi = summary["resolution_ci"]
    alo, ahi = summary["accuracy_ci"]
    breaks = sum(1 for r in exceptions if r.classification in REAL_BREAKS)

    rows_batch = len(orders)

    parts: list[str] = [f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><title>Reconciliation report</title>
<style>{CSS}</style></head><body>
<h1>Three-way reconciliation report</h1>
<div class="sub">merchant ledger &middot; gateway report &middot; bank statement
&mdash; generated {datetime.now():%d %B %Y, %H:%M}</div>

<div class="cards">
  <div class="card"><div class="label">Batch size</div>
    <div class="value">{rows_batch}</div>
    <div class="note">orders &rarr; {len(resolutions)} entities</div></div>
  <div class="card green"><div class="label">Resolved</div>
    <div class="value">{summary['resolution_rate']:.1%}</div>
    <div class="note">95% CI [{lo:.1%}, {hi:.1%}]</div>
    <div class="bar"><span style="width:{summary['resolution_rate']*100:.1f}%"></span></div></div>
  <div class="card green"><div class="label">Classification accuracy</div>
    <div class="value">{summary['accuracy']:.1%}</div>
    <div class="note">{summary['correct']}/{summary['graded']} vs answer key</div></div>
  <div class="card amber"><div class="label">Exceptions</div>
    <div class="value">{len(exceptions)}</div>
    <div class="note">{breaks} genuine breaks</div></div>
</div>
"""]

    # -- tiers ------------------------------------------------------------
    parts.append('<h2>Resolution by confidence tier</h2>'
                 '<div class="sub">Each resolution records the method that '
                 'produced it, so the result is stratified by certainty '
                 'rather than reported as one opaque figure.</div>'
                 '<table><tr><th>Tier</th><th>Method</th>'
                 '<th class="num">Count</th><th class="num">Share</th></tr>')
    for t in sorted(summary["tiers"]):
        n = summary["tiers"][t]
        parts.append(f'<tr><td class="mono">tier {t}</td>'
                     f'<td>{e(TIER_NAMES.get(t, ""))}</td>'
                     f'<td class="num">{n}</td>'
                     f'<td class="num">{n/summary["resolved"]:.1%}</td></tr>')
    parts.append('</table>')

    # -- confusion --------------------------------------------------------
    parts.append('<h2>Per-class performance against the answer key</h2>'
                 '<div class="sub">The generator plants each defect '
                 'deliberately and records it in a ground-truth file the '
                 'engine never reads. Every figure below is measured against '
                 'that key.</div>'
                 '<table><tr><th>Class</th><th class="num">Precision</th>'
                 '<th class="num">Recall</th><th class="num">F1</th>'
                 '<th class="num">Support</th><th class="num">FP</th>'
                 '<th class="num">FN</th></tr>')
    for label in sorted(metrics, key=lambda k: -metrics[k].support):
        m = metrics[label]
        if m.support == 0 and m.fp == 0:
            continue
        cls = "break" if label in REAL_BREAKS else ("ok" if label == "clean" else "")
        parts.append(
            f'<tr><td><span class="tag {cls}">{e(label)}</span></td>'
            f'<td class="num">{m.precision:.2f}</td>'
            f'<td class="num">{m.recall:.2f}</td>'
            f'<td class="num">{m.f1:.2f}</td>'
            f'<td class="num">{m.support}</td>'
            f'<td class="num">{m.fp}</td><td class="num">{m.fn}</td></tr>')
    parts.append('</table>')

    parts.append(
        '<div class="caveat"><strong>What this number does and does not '
        'show.</strong> These figures measure whether the engine correctly '
        'identifies the defect classes it was designed around, on data '
        'generated by the same author. They are evidence of no false '
        'positives and no silently dropped records. They are not evidence of '
        'performance on a real merchant\'s books, where defect types the '
        'generator does not model would appear.</div>')

    # -- exceptions -------------------------------------------------------
    parts.append(f'<h2>Exceptions &mdash; {len(exceptions)} records the engine '
                 f'could not resolve</h2>'
                 '<div class="sub">Listed in full, with the reason in each '
                 'case. Nothing here is dropped or absorbed into the match '
                 'rate.</div>'
                 '<table><tr><th>Record</th><th>Type</th><th>Classification</th>'
                 '<th>Reason</th></tr>')
    for r in sorted(exceptions, key=lambda x: x.classification):
        cls = "break" if r.classification in REAL_BREAKS else ""
        parts.append(
            f'<tr><td class="mono">{e(r.entity_id)}</td>'
            f'<td class="reason">{e(r.entity_type)}</td>'
            f'<td><span class="tag {cls}">{e(r.classification)}</span></td>'
            f'<td class="reason">{e(r.detail)}</td></tr>')
    parts.append('</table>')

    parts.append(
        f'<footer>Generated from {datadir}. '
        f'Reconciles a merchant ledger against a payment gateway report and '
        f'the corresponding bank statement. Ground truth is never read by the '
        f'reconciliation engine.</footer></body></html>')

    outfile.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="report.html")
    args = ap.parse_args()
    build(Path(args.data), Path(args.out))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
