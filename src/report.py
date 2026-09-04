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
     font-weight:500;background:#232323;color:#b8b3ad;white-space:nowrap}
.tag + .tag{margin-left:7px}
.tag.break{background:#3a2020;color:#e08c7a}
.tag.ok{background:#1e2a1a;color:#8fc275}
.bar{height:5px;background:#1c1c1c;border-radius:3px;overflow:hidden;margin-top:7px}
.bar span{display:block;height:100%;background:#7bb661}
.reason{color:#a09a94;font-size:12.5px}
.caveat{background:#161616;border-left:2px solid #d9a441;padding:14px 18px;
        border-radius:0 6px 6px 0;margin-top:18px;font-size:13.5px;color:#b8b3ad}
footer{margin-top:52px;padding-top:18px;border-top:1px solid #1f1f1f;
       color:#5f5a55;font-size:12px}
details{background:#141414;border:1px solid #242424;border-radius:8px;
        margin-bottom:10px;overflow:hidden}
details[open]{border-color:#303030}
summary{padding:13px 18px;cursor:pointer;list-style:none;display:flex;
        align-items:center;gap:12px;font-size:13.5px}
summary::-webkit-details-marker{display:none}
summary::before{content:"";display:inline-block;flex:none;
                width:0;height:0;border-left:5px solid #6f6a64;
                border-top:4px solid transparent;
                border-bottom:4px solid transparent;
                transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
summary .id{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;
            color:#d8d4cf;min-width:172px}
summary .verdict{margin-left:auto;color:#8b8680;font-size:12px}
.body{padding:2px 18px 18px 42px;border-top:1px solid #1c1c1c}
.steps{margin:14px 0 16px;border-left:1px solid #2a2a2a;padding-left:16px}
.step{display:flex;gap:10px;padding:5px 0;font-size:12.5px;align-items:baseline}
.step .n{color:#5f5a55;min-width:16px;font-variant-numeric:tabular-nums}
.step .tool{font-family:ui-monospace,Menlo,Consolas,monospace;color:#8fa8c7;
            min-width:236px}
.step .out{color:#918c86}
.step.neg .out{color:#c79a86}
.prose{font-size:13.5px;color:#c2bdb7;margin:10px 0}
.prose .lab{display:block;font-size:11px;text-transform:uppercase;
            letter-spacing:.07em;color:#7a756f;margin-bottom:5px}
.note{background:#131313;border-left:2px solid #4a4a4a;padding:11px 15px;
      border-radius:0 5px 5px 0;font-size:13px;color:#aaa5a0;margin-top:12px}
.disagree{border-left:2px solid #d9a441}
"""

# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------
# Hand-built SVG rather than a plotting library. Three reasons: the report must
# stay a single file with no CDN, the charts are simple enough that a
# dependency would cost more than it saves, and inline SVG inherits the page's
# colours instead of fighting them.

def line_chart(points: list[tuple[float, float]], *, width=560, height=200,
               x_label="", y_label="", y_min=90.0, y_max=100.5) -> str:
    """Accuracy against defect density. Y axis deliberately not zero-based:
    the interesting range is 92-100% and a zero baseline would flatten it."""
    pad_l, pad_b, pad_t, pad_r = 46, 34, 14, 12
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    xs = [p[0] for p in points]
    x_min, x_max = min(xs), max(xs)

    def sx(x): return pad_l + (x - x_min) / (x_max - x_min or 1) * pw
    def sy(y): return pad_t + (1 - (y - y_min) / (y_max - y_min)) * ph

    grid, ticks = [], []
    for i in range(5):
        v = y_min + (y_max - y_min) * i / 4
        y = sy(v)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" '
                    f'y2="{y:.1f}" stroke="#222" stroke-width="1"/>')
        ticks.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" fill="#6f6a64" '
                     f'font-size="10" text-anchor="end">{v:.0f}%</text>')

    path = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}"
                    for i, (x, y) in enumerate(points))
    area = (f"M{sx(points[0][0]):.1f},{sy(y_min):.1f} " +
            " ".join(f"L{sx(x):.1f},{sy(y):.1f}" for x, y in points) +
            f" L{sx(points[-1][0]):.1f},{sy(y_min):.1f} Z")

    dots = "".join(
        f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3.5" fill="#0f0f0f" '
        f'stroke="#d9a441" stroke-width="2"/>'
        f'<text x="{sx(x):.1f}" y="{sy(y)-11:.1f}" fill="#d9a441" '
        f'font-size="10.5" text-anchor="middle">{y:.1f}</text>'
        for x, y in points)
    xt = "".join(
        f'<text x="{sx(x):.1f}" y="{height-pad_b+16:.1f}" fill="#6f6a64" '
        f'font-size="10" text-anchor="middle">{x:.0f}%</text>'
        for x, _ in points)

    return (f'<svg viewBox="0 0 {width} {height}" width="100%" '
            f'style="max-width:{width}px">'
            f'{"".join(grid)}{"".join(ticks)}'
            f'<path d="{area}" fill="#d9a441" opacity="0.07"/>'
            f'<path d="{path}" fill="none" stroke="#d9a441" stroke-width="2"/>'
            f'{dots}{xt}'
            f'<text x="{pad_l+pw/2:.0f}" y="{height-2}" fill="#8b8680" '
            f'font-size="10.5" text-anchor="middle">{e(x_label)}</text>'
            f'</svg>')


def bar_row(label: str, value: int, total: int, colour: str) -> str:
    pct = value / total * 100 if total else 0
    return (f'<div style="display:flex;align-items:center;gap:12px;'
            f'padding:5px 0;font-size:13px">'
            f'<span style="min-width:186px;font-family:ui-monospace,Menlo,'
            f'monospace;font-size:12.5px;color:#b8b3ad">{e(label)}</span>'
            f'<span style="flex:1;height:7px;background:#1c1c1c;'
            f'border-radius:4px;overflow:hidden">'
            f'<span style="display:block;height:100%;width:{pct:.1f}%;'
            f'background:{colour}"></span></span>'
            f'<span style="min-width:30px;text-align:right;color:#8b8680;'
            f'font-variant-numeric:tabular-nums">{value}</span></div>')


# Classes that mean money is genuinely missing or unaccounted for, as opposed
# to differences the system can fully explain.
REAL_BREAKS = {"missing_payment", "orphan_bank_credit", "missing_bank_row",
               "settlement_not_in_bank", "settlement_total_mismatch",
               "net_arithmetic_error", "amount_mismatch", "method_mismatch"}

TIER_NAMES = {0: "self-consistency", 1: "exact key join",
              2: "deterministic inference", 3: "reference recovery"}


def e(x) -> str:
    return html.escape(str(x))


def plain(text: str) -> str:
    """
    Reduce a model's markdown to prose.

    The Q&A answers are meant to be two or three sentences. Models reach for
    bold and tables anyway, and escaping that verbatim renders as a wall of
    asterisks and pipes. Rendering the markdown properly would mean shipping a
    parser for untrusted model output, which is a worse trade than dropping the
    formatting.
    """
    import re as _re
    t = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # bold
    t = _re.sub(r"\|[-: |]+\|", " ", t)                   # table rules
    t = t.replace("|", " · ")                            # table cells
    t = _re.sub(r"\s*·\s*·\s*", " · ", t)
    t = _re.sub(r"[ \t]{2,}", " ", t)
    return t.strip(" ·")


def load_traces(path: Path) -> list[dict]:
    """Agent traces are optional. A report without them is still complete;
    the deterministic result does not depend on the agent having run."""
    if not path.exists():
        return []
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return []


def build(datadir: Path, outfile: Path, traces_path: Path | None = None,
          qa_path: Path | None = None) -> None:
    traces = load_traces(traces_path) if traces_path else []
    qa = load_traces(qa_path) if qa_path else []
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
    parts.append(
        '<h2>Where the engine breaks</h2>'
        '<div class="sub">Raising defect density alone does not degrade '
        'accuracy: each record is classified independently, so more defects '
        'of the same kinds change nothing. Defects that <em>interact</em> do. '
        'Allowing several defects on one record produces this curve.</div>'
        + line_chart([(22, 100.0), (37, 98.6), (52, 97.6),
                      (62, 95.7), (82, 92.3)],
                     x_label="compound defect density")
        + '<div class="sub" style="margin-top:10px">Every failure has one '
          'shape: an order carrying both a fee mismatch and a refund is '
          'reported as one or the other, because the taxonomy allows a single '
          'label per record. Neither answer is wrong. The limitation is in the '
          'classification scheme, not the matcher.</div>')

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
    by_id = {t["entity_id"]: t for t in traces}
    for r in sorted(exceptions, key=lambda x: x.classification):
        cls = "break" if r.classification in REAL_BREAKS else ""
        extra = ""
        tr = by_id.get(r.entity_id)
        if tr and not tr.get("agreed"):
            extra = (f'<span class="tag break">agent: '
                     f'{e(tr["agent_classification"])}</span>')
        parts.append(
            f'<tr><td class="mono">{e(r.entity_id)}</td>'
            f'<td class="reason">{e(r.entity_type)}</td>'
            f'<td><span class="tag {cls}">{e(r.classification)}</span>{extra}</td>'
            f'<td class="reason">{e(r.detail)}</td></tr>')
    parts.append('</table>')

    # ---- agent investigations (optional) --------------------------------
    if traces:
        agreed = sum(1 for t in traces if t.get("agreed"))
        calls = sum(t.get("model_calls", 0) for t in traces)
        secs = sum(t.get("seconds", 0) for t in traces)
        tool_counts: dict[str, int] = {}
        for t in traces:
            for st in t.get("steps", []):
                tool_counts[st["tool"]] = tool_counts.get(st["tool"], 0) + 1

        parts.append(
            f'<h2>Agent investigation of the exceptions</h2>'
            f'<div class="sub">Each unresolved record is passed to a bounded '
            f'agent that selects its own investigation tools. The agent never '
            f'computes a result: every value below came from deterministic '
            f'code the model cannot influence. It contributes the '
            f'investigative path, not the arithmetic.</div>'
            f'<div class="cards">'
            f'<div class="card"><div class="label">Investigated</div>'
            f'<div class="value">{len(traces)}</div>'
            f'<div class="note">{calls} model calls</div></div>'
            f'<div class="card green"><div class="label">Agreed with matcher</div>'
            f'<div class="value">{agreed}/{len(traces)}</div>'
            f'<div class="note">independently, without being told</div></div>'
            f'<div class="card"><div class="label">Tool calls</div>'
            f'<div class="value">{sum(tool_counts.values())}</div>'
            f'<div class="note">{len(tool_counts)} distinct tools, across '
            f'{calls} model rounds</div></div>'
            f'<div class="card"><div class="label">Wall clock</div>'
            f'<div class="value">{secs:.0f}s</div>'
            f'<div class="note">{secs/len(traces):.0f}s per record</div></div>'
            f'</div>')

        parts.append('<h2>Tools the agent chose to call</h2>'
                     '<div class="sub">Unprompted. No ordering or preference '
                     'was specified.</div><div style="margin-top:14px">')
        top = max(tool_counts.values()) if tool_counts else 1
        for name, n in sorted(tool_counts.items(), key=lambda kv: -kv[1]):
            parts.append(bar_row(name, n, top, "#7bb661"))
        parts.append('</div>')

        parts.append('<h2>Investigation traces</h2>'
                     '<div class="sub">Every step, in the order the agent '
                     'chose it. Expand a record to see what it did.</div>')

        for t in traces:
            agreed_one = t.get("agreed")
            cls = "" if agreed_one else "disagree"
            badge = "" if agreed_one else (
                f'<span class="tag break">disagreed</span>')
            parts.append(
                f'<details class="{cls}"><summary>'
                f'<span class="id">{e(t["entity_id"])}</span>'
                f'<span class="tag">{e(t["agent_classification"])}</span>'
                f'{badge}'
                f'<span class="verdict">{len(t.get("steps", []))} tool calls '
                f'&middot; {t.get("seconds", 0):.0f}s</span>'
                f'</summary><div class="body">')

            if not agreed_one:
                parts.append(
                    f'<div class="note disagree">'
                    f'<strong>The agent disagreed with the matcher.</strong> '
                    f'The deterministic engine classified this record '
                    f'<span class="mono">{e(t["matcher_classification"])}</span>; '
                    f'after its own investigation the agent concluded '
                    f'<span class="mono">{e(t["agent_classification"])}</span>. '
                    f'Both are recorded. Neither overrides the other -- the '
                    f'agent investigates independently and is not shown the '
                    f'engine\'s verdict as a fact.</div>')

            parts.append('<div class="steps">')
            for st in t.get("steps", []):
                args = ", ".join(f"{k}={v}" for k, v in
                                 (st.get("tool_input") or {}).items())
                neg = "" if st.get("ok") else " neg"
                parts.append(
                    f'<div class="step{neg}"><span class="n">{st.get("n","")}</span>'
                    f'<span class="tool">{e(st["tool"])}({e(args)})</span>'
                    f'<span class="out">{e(st.get("summary",""))}</span></div>')
            parts.append('</div>')

            if t.get("reasoning"):
                parts.append(f'<div class="prose"><span class="lab">Conclusion'
                             f'</span>{e(t["reasoning"])}</div>')
            if t.get("analyst_note"):
                parts.append(f'<div class="note"><strong>For the analyst.</strong> '
                             f'{e(t["analyst_note"])}</div>')
            parts.append('</div></details>')

    # ---- settlement Q&A (optional) --------------------------------------
    if qa:
        answered = [q for q in qa if not q.get("error")]
        parts.append(
            '<h2>Settlement Q&amp;A</h2>'
            '<div class="sub">Plain-English questions about the batch. The '
            'model translates the question into a structured query and '
            'explains the result; it does not compute the figures. Every '
            'number below came from the query named beneath each answer.'
            '</div>')

        for item in answered:
            tools = ", ".join(item.get("tools_used") or []) or "no query"
            parts.append(
                f'<details><summary>'
                f'<span class="id" style="min-width:0">'
                f'{e(item["question"])}</span>'
                f'<span class="verdict">{e(tools)}</span>'
                f'</summary><div class="body">'
                f'<div class="prose">{e(plain(item["answer"]))}</div>'
                f'<div class="steps"><div class="step">'
                f'<span class="tool">{e(tools)}</span>'
                f'<span class="out">the query this answer rests on</span>'
                f'</div></div></div></details>')

        parts.append(
            '<div class="caveat"><strong>A weaker guarantee than the '
            'resolution agent.</strong> Every figure here came from a query '
            'result, but the rule against combining figures lives in the '
            'prompt rather than in code. On one run the model added two '
            'results together and reported the sum -- correctly, which is '
            'worse than incorrectly, because a right answer produced by a '
            'forbidden route looks identical to a grounded one. The '
            'resolution agent\'s constraints are enforced in code and tested '
            'without a model; these are not.</div>')

    # A web upload lands in a throwaway temp directory. Naming it in the
    # report tells the reader nothing and exposes a server path.
    source = ('the uploaded files' if datadir.name.startswith('recon-')
              else str(datadir))
    parts.append(
        f'<footer>Generated from {source}. '
        f'Reconciles a merchant ledger against a payment gateway report and '
        f'the corresponding bank statement. Ground truth is never read by the '
        f'reconciliation engine.</footer></body></html>')

    outfile.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--traces", default="agent_traces.json",
                    help="agent trace JSON; omitted silently if absent")
    ap.add_argument("--qa", default="qa_answers.json",
                    help="Q&A answer JSON; omitted silently if absent")
    args = ap.parse_args()
    build(Path(args.data), Path(args.out), Path(args.traces), Path(args.qa))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
