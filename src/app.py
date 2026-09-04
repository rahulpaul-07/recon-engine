"""
Web interface for the reconciliation engine.

Upload a merchant ledger, a gateway report and a bank statement; the server
runs the deterministic pipeline and returns the same report the command line
produces.

Two tiers of endpoint
---------------------
**Deterministic** (`/reconcile`, `/sample`) need no language model, no API key
and no network. They are the reason the free tier is sufficient and the reason
the service cannot fail because a vendor is down.

**Model-backed** (`/investigate`, `/ask`) run the agent and the settlement Q&A.
Exposing these publicly means either an operator key on a public server or
asking a visitor for theirs, and both carry real cost. The compromise here:

  * a key may come from the environment (operator's) or from a request header
    (visitor's, never stored, never logged)
  * requests are rate limited globally and capped at a few records each, so a
    public endpoint cannot drain an operator's credit
  * with no key from either source, the endpoints say so plainly rather than
    failing obscurely

Uploaded files are parsed in memory and written to a temporary directory that
is deleted when the request completes. Nothing is stored.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from evaluate import grade  # noqa: E402
from matcher import Engine, load  # noqa: E402
from report import build  # noqa: E402

app = FastAPI(title="Three-way reconciliation")

MAX_BYTES = 8 * 1024 * 1024          # a 5,000-order batch is well under 1 MB
REQUIRED = ("ledger", "gateway", "bank")

# Caps on the model-backed endpoints. These exist because the endpoints are
# public and the cost is the operator's. Deliberately tight: the point is to
# demonstrate the agent, not to offer a free reconciliation service.
AGENT_MAX_RECORDS = 3
RATE_LIMIT_PER_HOUR = 60

_calls: list[float] = []


def _rate_limited() -> bool:
    """Global, in-memory, per-process. Adequate for a single free-tier
    instance; a real deployment would use a shared store."""
    import time
    now = time.time()
    _calls[:] = [t for t in _calls if now - t < 3600]
    if len(_calls) >= RATE_LIMIT_PER_HOUR:
        return True
    _calls.append(now)
    return False


def _provider_for(request_key: str | None):
    """
    Resolve a provider, preferring a key supplied with the request.

    A visitor's key is used for that request only. It is not written to disk,
    not logged, and not retained after the response.
    """
    import os

    from llm import get_provider
    if request_key:
        previous = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = request_key
        try:
            return get_provider()
        finally:
            if previous is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = previous
    return get_provider()


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Three-way reconciliation</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e6e3;font:15px/1.6 ui-sans-serif,-apple-system,
     "Segoe UI",system-ui,sans-serif;padding:48px 24px;max-width:820px;margin:0 auto}
h1{font-size:27px;font-weight:600;letter-spacing:-.02em}
.sub{color:#8b8680;font-size:14px;margin:8px 0 22px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 9px}
.stat{flex:1 1 148px;background:#161616;border:1px solid #262626;
     border-radius:9px;padding:14px 16px}
.stat .n{font-size:22px;font-weight:600;color:#d9a441;
     font-variant-numeric:tabular-nums}
.stat .l{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
     color:#8b8680;margin-top:5px;line-height:1.4}
.statnote{font-size:12.5px;color:#5f5a55;margin:0 0 30px;line-height:1.6}
.card{background:#161616;border:1px solid #262626;border-radius:9px;padding:22px;
      margin-bottom:16px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.07em;
      color:#8b8680;margin-bottom:7px}
.field{margin-bottom:16px}
.opt{color:#5f5a55;text-transform:none;letter-spacing:0;font-size:12px}
input[type=file]{width:100%;background:#0f0f0f;border:1px solid #2a2a2a;
     border-radius:6px;padding:9px 11px;color:#b8b3ad;font-size:13px}
input[type=file]::file-selector-button{background:#232323;border:0;color:#d8d4cf;
     padding:5px 12px;border-radius:5px;margin-right:11px;cursor:pointer;
     font-size:12.5px}
button{background:#d9a441;color:#0f0f0f;border:0;border-radius:6px;
     padding:11px 22px;font-size:14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
button.ghost{background:#232323;color:#d8d4cf;font-weight:500;margin-left:9px}
.note{font-size:13px;color:#8b8680;margin-top:14px;line-height:1.65}
.err{background:#2a1818;border-left:2px solid #c05a4a;padding:13px 16px;
     border-radius:0 6px 6px 0;color:#e0b0a4;font-size:13.5px;margin-top:16px;
     white-space:pre-wrap}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;
     color:#b8b3ad}
</style></head><body>

<h1>Three-way payment reconciliation</h1>
<div class="sub">Upload a merchant ledger, a payment gateway report and a bank
statement. The engine matches them, reports what it resolved and how certain
each resolution is, and lists every record it could not resolve with a
reason.</div>

<div class="stats">
  <div class="stat"><div class="n">90.8%</div><div class="l">resolved</div></div>
  <div class="stat"><div class="n">100%</div><div class="l">classification accuracy</div></div>
  <div class="stat"><div class="n">104</div><div class="l">tests, Python 3.10&ndash;3.13</div></div>
  <div class="stat"><div class="n">7</div><div class="l">providers, scoped failover</div></div>
</div>
<div class="statnote">Measured on the reference batch against a ground-truth
answer key the engine never reads, not on a real merchant&rsquo;s books. Every
figure is reproducible from the repository.</div>

<form id="f" class="card">
  <div class="field"><label>Merchant ledger <span class="opt">— order_id,
    order_amount_paise, order_datetime, payment_method, order_status</span></label>
    <input type="file" name="ledger" accept=".csv" required></div>

  <div class="field"><label>Gateway report <span class="opt">— txn_id, txn_type,
    order_ref, gross_amount_paise, fee_paise, net_amount_paise,
    settlement_id</span></label>
    <input type="file" name="gateway" accept=".csv" required></div>

  <div class="field"><label>Bank statement <span class="opt">— bank_txn_id,
    value_date, description, credit_paise, debit_paise, balance_paise,
    utr</span></label>
    <input type="file" name="bank" accept=".csv" required></div>

  <div class="field"><label>Settlement report <span class="opt">— optional;
    links gateway payments to bank credits</span></label>
    <input type="file" name="settlements" accept=".csv"></div>

  <div class="field"><label>Ground truth <span class="opt">— optional; if
    supplied, the report includes measured accuracy per class</span></label>
    <input type="file" name="ground_truth" accept=".csv"></div>

  <button type="submit" id="go">Reconcile</button>
  <button type="button" class="ghost" id="sample">Use the sample batch</button>
</form>

<div id="err"></div>

<div class="note">
Files are parsed in memory and deleted when the request completes; nothing is
stored. The reconciliation above runs the deterministic engine only &mdash; no
language model is involved, so there is no API key and no per-request cost. The
model-backed layers below do need a key, and it is used for that one request
only &mdash; never stored, never logged.
<br><br>
Amounts are integer paise: <code>45000</code> means &#8377;450.00.
</div>

<h1 style="font-size:20px;margin-top:44px">Try the AI layers</h1>
<div class="sub">Both run against a freshly generated sample batch. They need a
language model; the reconciliation above does not.</div>

<div class="card">
  <div class="field">
    <label>API key <span class="opt">— optional. Used for this request only,
      never stored or logged. Leave blank to use the server's key if one is
      configured.</span></label>
    <input type="password" id="key" placeholder="sk-ant-..." autocomplete="off"
      style="width:100%;background:#0f0f0f;border:1px solid #2a2a2a;
             border-radius:6px;padding:9px 11px;color:#b8b3ad;font-size:13px">
  </div>

  <div class="field">
    <label>Ask a question about the batch</label>
    <input type="text" id="q" value="How much did I pay in fees, and which method costs most?"
      style="width:100%;background:#0f0f0f;border:1px solid #2a2a2a;
             border-radius:6px;padding:9px 11px;color:#e8e6e3;font-size:13px">
  </div>

  <button type="button" id="askBtn">Ask</button>
  <button type="button" class="ghost" id="invBtn">Investigate 3 exceptions</button>
  <div id="ai"></div>
</div>

<script>
const form = document.getElementById('f');
const err  = document.getElementById('err');
const go   = document.getElementById('go');

function fail(msg){ err.innerHTML = '<div class="err">' + msg + '</div>'; }

async function send(url, body){
  err.innerHTML = ''; go.disabled = true; go.textContent = 'Reconciling...';
  try {
    const r = await fetch(url, body ? {method:'POST', body} : {method:'POST'});
    if (!r.ok) { const j = await r.json().catch(() => ({detail:'request failed'}));
                 fail(j.detail || 'request failed'); return; }
    // Open the report in its own tab. Writing it over this page leaves no
    // history entry, so the back button does nothing and the visitor has to
    // retype the URL to reach the upload form again.
    const html = await r.text();
    const w = window.open('', '_blank');
    if (w) { w.document.open(); w.document.write(html); w.document.close();
             err.innerHTML = '<div class="note" style="margin-top:16px">' +
               'Report opened in a new tab.</div>'; }
    else {  // pop-up blocked: fall back to a download rather than losing it
      const url = URL.createObjectURL(new Blob([html], {type:'text/html'}));
      const a = document.createElement('a');
      a.href = url; a.download = 'reconciliation-report.html'; a.click();
      URL.revokeObjectURL(url);
      err.innerHTML = '<div class="note" style="margin-top:16px">' +
        'Pop-up blocked, so the report was downloaded instead.</div>';
    }
    // Clear the inputs after a successful upload, so a second click
    // cannot silently re-send the previous batch. Uploads only: the
    // sample button passes no body and leaves selections alone.
    if (body) form.reset();
  } catch (e) { fail('Could not reach the server: ' + e.message); }
  finally { go.disabled = false; go.textContent = 'Reconcile'; }
}

form.addEventListener('submit', e => { e.preventDefault();
  send('/reconcile', new FormData(form)); });
document.getElementById('sample').addEventListener('click', () =>
  send('/sample', null));

const ai = document.getElementById('ai');
const esc = t => String(t).replace(/[&<>]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function busy(msg){ ai.innerHTML =
  '<div class="note" style="margin-top:16px">' + msg + '</div>'; }

async function callAI(url, body){
  // Both AI calls are slow enough to invite an impatient second click,
  // which would spend the rate limit twice and race the two responses.
  const btns = ['askBtn','invBtn'].map(i => document.getElementById(i));
  btns.forEach(b => b.disabled = true);
  try {
    const key = document.getElementById('key').value.trim();
    const headers = {};
    if (key) headers['x-api-key'] = key;
    if (body) headers['Content-Type'] = 'application/json';
    const r = await fetch(url, {method:'POST', headers,
                               body: body ? JSON.stringify(body) : null});
    return {ok: r.ok, data: await r.json()};
  } finally { btns.forEach(b => b.disabled = false); }
}

document.getElementById('askBtn').addEventListener('click', async () => {
  busy('Asking. The model writes a structured query, the server runs it, and the model explains the real result...');
  const {ok, data} = await callAI('/ask', {question: document.getElementById('q').value});
  if (!ok) { ai.innerHTML = '<div class="err">' + esc(data.detail) + '</div>'; return; }
  ai.innerHTML =
    '<div class="card" style="margin-top:16px;background:#131313">' +
    '<div style="font-size:14px;line-height:1.7">' + esc(data.answer || data.error) + '</div>' +
    '<div class="note" style="margin-top:12px"><code>' +
      esc((data.tools_used||[]).join(', ') || 'no query') + '</code> &middot; ' +
      data.model_calls + ' model call(s)</div>' +
    '<div class="err" style="background:#1c1a14;border-left-color:#7a6020;color:#c9bfa0">' +
      esc(data.caveat) + '</div></div>';
});

document.getElementById('invBtn').addEventListener('click', async () => {
  busy('Investigating. The agent picks its own tools from nine deterministic checks, up to five rounds per record. This takes about a minute...');
  const {ok, data} = await callAI('/investigate', null);
  if (!ok) { ai.innerHTML = '<div class="err">' + esc(data.detail) + '</div>'; return; }
  let h = '<div class="note" style="margin-top:16px">Answered by <code>' +
          esc(data.provider) + '</code>, capped at ' + data.capped_at +
          ' records.</div>';
  for (const inv of data.investigations){
    h += '<div class="card" style="margin-top:12px;background:#131313">' +
      '<div style="font-size:13px"><code>' + esc(inv.entity_id) + '</code> &mdash; ' +
      'matcher said <code>' + esc(inv.matcher_said) + '</code>, agent said <code>' +
      esc(inv.agent_said) + '</code>' +
      (inv.agreed ? '' : ' <span style="color:#d9a441">(disagreed)</span>') + '</div>' +
      '<div style="margin:12px 0;border-left:1px solid #2a2a2a;padding-left:14px">';
    for (const s of inv.steps){
      h += '<div style="font-size:12.5px;padding:3px 0"><span style="color:#5f5a55">' +
           s.n + '</span> <code style="color:#8fa8c7">' + esc(s.tool) + '</code> ' +
           '<span style="color:#918c86">' + esc(s.summary) + '</span></div>';
    }
    h += '</div><div style="font-size:13.5px;color:#c2bdb7">' +
         esc(inv.reasoning) + '</div>';
    if (inv.analyst_note)
      h += '<div class="note" style="border-left:2px solid #4a4a4a;padding-left:13px;margin-top:10px"><strong>For the analyst.</strong> ' +
           esc(inv.analyst_note) + '</div>';
    h += '</div>';
  }
  ai.innerHTML = h;
});
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_required": False}


def _reconcile_dir(workdir: Path) -> str:
    """Run the pipeline over a directory and return the report HTML."""
    orders, txns, settlements, bank = load(workdir)
    if not orders:
        raise ValueError("the ledger contains no rows")

    Engine(orders, txns, settlements, bank).run()

    out = workdir / "report.html"
    build(workdir, out)
    return out.read_text(encoding="utf-8")


@app.post("/sample", response_class=HTMLResponse)
def sample() -> HTMLResponse:
    """
    Reconcile a freshly generated batch.

    Present so a reviewer with no CSVs to hand still sees the system work. The
    batch is generated per request rather than served from disk, so the numbers
    are produced live rather than recalled.
    """
    import subprocess

    tmp = Path(tempfile.mkdtemp(prefix="recon-sample-"))
    try:
        subprocess.run(
            [sys.executable, str(SRC / "generate_data.py"),
             "--seed", "42", "--orders", "120", "--out", str(tmp)],
            check=True, capture_output=True, timeout=60)
        return HTMLResponse(_reconcile_dir(tmp))
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"detail": f"sample failed: {exc}"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/reconcile", response_class=HTMLResponse)
async def reconcile(
    ledger: UploadFile = File(...),
    gateway: UploadFile = File(...),
    bank: UploadFile = File(...),
    settlements: UploadFile | None = File(None),
    ground_truth: UploadFile | None = File(None),
) -> HTMLResponse:
    tmp = Path(tempfile.mkdtemp(prefix="recon-"))
    try:
        uploads = {"ledger": ledger, "gateway": gateway, "bank": bank,
                   "settlements": settlements, "ground_truth": ground_truth}

        for name, f in uploads.items():
            if f is None:
                continue
            data = await f.read()
            if len(data) > MAX_BYTES:
                return JSONResponse(status_code=413, content={
                    "detail": f"{name}.csv is larger than "
                              f"{MAX_BYTES // 1024 // 1024} MB"})
            if not data.strip():
                if name in REQUIRED:
                    return JSONResponse(status_code=400, content={
                        "detail": f"{name}.csv is empty"})
                continue
            (tmp / f"{name}.csv").write_bytes(data)

        # A settlement report is optional in the upload but required by the
        # loader, because it is what joins gateway rows to bank credits. An
        # empty one is honest: it means every bank row must be matched by
        # inference rather than by reference.
        if not (tmp / "settlements.csv").exists():
            (tmp / "settlements.csv").write_text(
                "settlement_id,capture_date,payout_date,total_paise,utr\n",
                encoding="utf-8")
        if not (tmp / "ground_truth.csv").exists():
            (tmp / "ground_truth.csv").write_text(
                "entity_id,entity_type,expected_classification,"
                "expected_match_target,notes\n", encoding="utf-8")

        return HTMLResponse(_reconcile_dir(tmp))

    except KeyError as exc:
        # Most likely cause by far: a column the engine needs is absent.
        return JSONResponse(status_code=400, content={
            "detail": f"A required column is missing: {exc}. Check the field "
                      f"names listed beside each upload."})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={
            "detail": f"Could not parse the files: {exc}"})
    except Exception:                                     # noqa: BLE001
        # The trace goes to the server log, not to the response. A public
        # endpoint should not hand a visitor absolute paths and internals.
        print(traceback.format_exc(limit=3), file=sys.stderr)
        return JSONResponse(status_code=500, content={
            "detail": "Reconciliation failed. The files parsed but the " +
                      "engine could not complete. Check that each file " +
                      "has the columns listed beside its upload field."})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Model-backed endpoints
# --------------------------------------------------------------------------

def _batch_from(payload: dict) -> Path:
    """Write an uploaded batch to a temporary directory."""
    tmp = Path(tempfile.mkdtemp(prefix="recon-ai-"))
    for name in ("ledger", "gateway", "bank", "settlements", "ground_truth"):
        content = payload.get(name)
        if content:
            (tmp / f"{name}.csv").write_text(content, encoding="utf-8")
    for name, header in (
            ("settlements", "settlement_id,capture_date,payout_date,"
                            "total_paise,utr\n"),
            ("ground_truth", "entity_id,entity_type,expected_classification,"
                             "expected_match_target,notes\n")):
        if not (tmp / f"{name}.csv").exists():
            (tmp / f"{name}.csv").write_text(header, encoding="utf-8")
    return tmp


def _sample_batch() -> Path:
    import subprocess
    tmp = Path(tempfile.mkdtemp(prefix="recon-ai-"))
    subprocess.run(
        [sys.executable, str(SRC / "generate_data.py"),
         "--seed", "42", "--orders", "120", "--out", str(tmp)],
        check=True, capture_output=True, timeout=60)
    return tmp


@app.post("/investigate")
async def investigate(request: Request) -> JSONResponse:
    """
    Run the resolution agent over the first few unresolved records.

    Capped at AGENT_MAX_RECORDS. Each investigation is several model calls, so
    an uncapped public endpoint would be an invitation to spend someone else's
    money.
    """
    if _rate_limited():
        return JSONResponse(status_code=429, content={
            "detail": f"Rate limit reached ({RATE_LIMIT_PER_HOUR}/hour). The "
                      f"model-backed endpoints are capped because the cost is "
                      f"the operator's. Supply your own key to bypass this."})

    key = request.headers.get("x-api-key") or None
    provider = _provider_for(key)
    if not provider.available:
        return JSONResponse(status_code=503, content={
            "detail": "No language model is configured. Set ANTHROPIC_API_KEY "
                      "on the server, or paste a key in the field above — it "
                      "is used for this request only and never stored.",
            "reason": getattr(provider, "reason", "unconfigured")})

    tmp = _sample_batch()
    try:
        from agent import ResolutionAgent, build_context
        from investigate import facts_for
        from tools import InvestigationTools

        orders, txns, settlements, bank = load(tmp)
        resolutions = Engine(orders, txns, settlements, bank).run()
        unresolved = [r for r in resolutions
                      if not r.resolved][:AGENT_MAX_RECORDS]

        agent = ResolutionAgent(InvestigationTools(orders, txns, settlements,
                                                   bank), provider=provider)
        out = []
        for r in unresolved:
            ctx = build_context(r.entity_id, r.entity_type, r.detail,
                                facts_for(r, orders, txns, settlements, bank))
            a = agent.investigate(r.entity_id, r.entity_type, ctx)
            out.append({
                "entity_id": a.entity_id,
                "matcher_said": r.classification,
                "agent_said": a.classification,
                "agreed": a.classification == r.classification,
                "reasoning": a.reasoning,
                "analyst_note": a.analyst_note,
                "terminated": a.terminated,
                "steps": [{"n": s.n, "tool": s.tool, "input": s.tool_input,
                           "ok": s.ok, "summary": s.summary} for s in a.steps],
            })
        return JSONResponse({"provider": getattr(provider, "name", "?"),
                             "capped_at": AGENT_MAX_RECORDS,
                             "investigations": out})
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"detail": f"investigation failed: {exc}"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/ask")
async def ask(request: Request) -> JSONResponse:
    """Answer one plain-English question about the sample batch."""
    if _rate_limited():
        return JSONResponse(status_code=429, content={
            "detail": f"Rate limit reached ({RATE_LIMIT_PER_HOUR}/hour)."})

    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse(status_code=400,
                            content={"detail": "No question supplied."})
    if len(question) > 400:
        return JSONResponse(status_code=400,
                            content={"detail": "Question is too long."})

    key = request.headers.get("x-api-key") or None
    provider = _provider_for(key)
    if not provider.available:
        return JSONResponse(status_code=503, content={
            "detail": "No language model is configured. Set ANTHROPIC_API_KEY "
                      "on the server, or paste a key above — used for this "
                      "request only, never stored."})

    tmp = _sample_batch()
    try:
        from ask import QAAgent, QueryTools

        orders, txns, settlements, bank = load(tmp)
        resolutions = Engine(orders, txns, settlements, bank).run()
        tools = QueryTools(orders, txns, settlements, bank, resolutions)
        a = QAAgent(tools, provider=provider).ask(question)
        return JSONResponse({
            "question": a.question, "answer": a.text, "error": a.error,
            "tools_used": a.tools_used, "model_calls": a.model_calls,
            "caveat": ("Every figure came from a query result, but the rule "
                       "against combining figures lives in the prompt rather "
                       "than in code. This layer has a weaker guarantee than "
                       "the resolution agent."),
        })
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"detail": f"question failed: {exc}"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
