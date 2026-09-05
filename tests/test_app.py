"""
Tests for the web interface.

The app is a thin layer over the engine, so these test the layer rather than
the reconciliation: that uploads are accepted, that malformed input produces a
useful message rather than a stack trace, and that nothing here needs a
language model.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

fastapi = pytest.importorskip("fastapi", reason="web layer is optional")
from fastapi.testclient import TestClient  # noqa: E402

from app import MAX_BYTES, app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    out = tmp_path_factory.mktemp("web")
    subprocess.run(
        [sys.executable, str(ROOT / "src" / "generate_data.py"),
         "--seed", "42", "--orders", "120", "--out", str(out)],
        check=True, capture_output=True)
    return out


def _files(batch, names=("ledger", "gateway", "bank", "settlements")):
    return {n: (f"{n}.csv", (batch / f"{n}.csv").read_bytes(), "text/csv")
            for n in names}


def _multipart(files: dict, question: str = ""):
    """
    Build a real multipart request without a running server.

    The endpoints are reached through TestClient everywhere else. This exists
    because the interesting failure happens after the provider check, which a
    keyless test never gets past.
    """
    from starlette.requests import Request

    boundary = "----recontest"
    body = b""
    for name, data in files.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"; '
                 f'filename="{name}.csv"\r\n'
                 f"Content-Type: text/csv\r\n\r\n").encode()
        body += data + b"\r\n"
    if question:
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="question"\r\n\r\n'
                 f"{question}\r\n").encode()
    body += f"--{boundary}--\r\n".encode()

    scope = {
        "type": "http", "method": "POST", "path": "/ask",
        "headers": [
            (b"content-type",
             f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(body)).encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class TestWebInterface:

    def test_page_loads(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Three-way payment reconciliation" in r.text

    def test_health_states_no_model_is_required(self, client):
        """The deterministic path must never depend on a provider."""
        assert client.get("/health").json()["model_required"] is False

    def test_sample_generates_and_reconciles(self, client):
        r = client.post("/sample")
        assert r.status_code == 200
        assert "reconciliation report" in r.text.lower()
        # Case-insensitive: the heading is uppercased by CSS, not in the markup.
        assert "exceptions" in r.text.lower()

    def test_upload_produces_a_report(self, client, batch):
        r = client.post("/reconcile", files=_files(batch))
        assert r.status_code == 200
        assert "90.8%" in r.text

    def test_ground_truth_is_optional(self, client, batch):
        """Without an answer key the report still renders, minus the metrics."""
        r = client.post("/reconcile", files=_files(batch))
        assert r.status_code == 200

    def test_settlements_are_optional(self, client, batch):
        """
        Omitting the settlement report is legitimate: it means every bank row
        must be matched by inference rather than by reference.
        """
        r = client.post("/reconcile",
                        files=_files(batch, ("ledger", "gateway", "bank")))
        assert r.status_code == 200

    def test_missing_column_explains_which(self, client, batch):
        f = _files(batch)
        f["ledger"] = ("ledger.csv", b"wrong,columns\n1,2\n", "text/csv")
        r = client.post("/reconcile", files=f)
        assert r.status_code == 400
        assert "required column is missing" in r.json()["detail"]

    def test_empty_required_file_is_rejected(self, client, batch):
        f = _files(batch)
        f["ledger"] = ("ledger.csv", b"", "text/csv")
        r = client.post("/reconcile", files=f)
        assert r.status_code == 400
        assert "empty" in r.json()["detail"]

    def test_missing_required_upload_is_rejected(self, client, batch):
        r = client.post("/reconcile",
                        files=_files(batch, ("ledger", "gateway")))
        assert r.status_code == 422        # FastAPI validation

    def test_oversized_upload_is_rejected(self, client, batch):
        f = _files(batch)
        f["ledger"] = ("ledger.csv", b"x" * (MAX_BYTES + 1), "text/csv")
        r = client.post("/reconcile", files=f)
        assert r.status_code == 413

    def test_errors_never_leak_a_stack_trace_for_bad_input(self, client, batch):
        """A malformed upload is a user error, not an internal one."""
        f = _files(batch)
        f["bank"] = ("bank.csv", b"a,b,c\n1,2,3\n", "text/csv")
        r = client.post("/reconcile", files=f)
        assert r.status_code == 400
        assert "Traceback" not in r.json()["detail"]


# Every environment variable that could make a provider available. Cleared
# explicitly in the degradation tests: those assert what happens with no model
# configured, so they must *guarantee* no model is configured rather than
# assume it. Without this they pass in CI and on a machine with no keys, and on
# a developer's machine they silently call the model instead -- which is both a
# false pass and a real cost.
PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY",
    "OPENROUTER_API_KEY", "TOGETHER_API_KEY", "MISTRAL_API_KEY",
    "RECON_LLM_PROVIDER",
)


@pytest.fixture
def no_provider(monkeypatch):
    """Guarantee the process has no usable model for the duration of a test."""
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestModelBackedEndpoints:
    """
    These endpoints need a language model. The tests assert they degrade
    correctly without one -- the state CI runs in, and the state a deployment
    with no configured key is in.

    None of them calls a model. That is deliberate: a test suite that spends
    money when run on a machine that happens to have a key configured is a
    test suite people stop running.
    """

    def test_investigate_without_a_key_explains_itself(self, client,
                                                       no_provider):
        r = client.post("/investigate")
        assert r.status_code == 503
        assert "No language model is configured" in r.json()["detail"]

    def test_ask_without_a_key_explains_itself(self, client, no_provider):
        r = client.post("/ask", json={"question": "how did it go?"})
        assert r.status_code == 503
        assert "never stored" in r.json()["detail"]

    def test_empty_question_is_rejected_before_any_model_call(self, client):
        r = client.post("/ask", json={"question": "   "})
        assert r.status_code == 400

    def test_overlong_question_is_rejected(self, client):
        r = client.post("/ask", json={"question": "x" * 500})
        assert r.status_code == 400

    def test_a_question_can_be_asked_about_uploaded_files(self, client, batch,
                                                          no_provider):
        """
        The layers answer about files sent with the request. Nothing is stored
        between calls, so the upload must reach the provider check -- a 503
        here proves the multipart body was parsed and a batch was prepared.
        """
        r = client.post("/ask", files=_files(batch),
                        data={"question": "how much went to fees?"})
        assert r.status_code == 503

    def test_a_partial_upload_is_rejected_before_any_model_call(self, client,
                                                               batch):
        """
        Two of the three required files is not a batch. Falling back to the
        sample silently would answer a question about data the caller never
        sent, which is worse than refusing.
        """
        r = client.post("/ask", files=_files(batch, ("ledger", "gateway")),
                        data={"question": "how much went to fees?"})
        assert r.status_code == 400
        assert "bank.csv" in r.json()["detail"]

    def test_investigate_accepts_uploaded_files(self, client, batch,
                                                no_provider):
        r = client.post("/investigate", files=_files(batch))
        assert r.status_code == 503

    @pytest.mark.parametrize("names", [
        ("ledger", "gateway", "bank"),
        ("ledger", "gateway", "bank", "settlements"),
    ])
    def test_an_upload_produces_a_batch_the_engine_can_load(self, batch, names):
        """
        Settlements are optional on the form and must be optional here too.

        This asserts on the loader rather than on a status code deliberately.
        Going through the endpoint without a model returns 503 before the
        loader ever runs, so an endpoint test passes whether or not the
        optional files are stubbed -- it cannot see this bug. The failure it
        guards against was exactly that: three files uploaded, no settlements
        written, and the engine unable to read its own working directory.
        """
        import asyncio

        from app import _read_request
        from matcher import load

        req = _multipart(
            {n: (batch / f"{n}.csv").read_bytes() for n in names},
            question="how much went to fees?")
        question, updir, err = asyncio.run(_read_request(req))
        assert err is None and updir is not None
        assert question == "how much went to fees?"
        try:
            orders, txns, settlements, bank = load(updir)
            assert orders, "the uploaded ledger produced no orders"
        finally:
            shutil.rmtree(updir, ignore_errors=True)

    def test_unreadable_files_are_rejected_before_any_model_call(self, client):
        """
        The same files that /reconcile rejects with a named column must be
        rejected the same way here. Without validation during preparation the
        loader fails inside the handler, after the provider check, and the
        caller gets an opaque 500 for a fixable typo.

        A 400 rather than a 503 is the assertion: it proves the batch was
        judged before a model was ever looked for.
        """
        d = ROOT / "datasets" / "07-malformed"
        files = {n: (f"{n}.csv", (d / f"{n}.csv").read_bytes(), "text/csv")
                 for n in ("ledger", "gateway", "bank")}
        r = client.post("/ask", files=files,
                        data={"question": "how much went to fees?"})
        assert r.status_code == 400
        assert "order_id" in r.json()["detail"]

    def test_a_request_key_is_never_left_in_the_environment(self, no_provider):
        """
        A visitor's key is used for one request and must not persist. If it
        leaked into the process environment it would silently become the
        operator's default for every later request.
        """
        import os

        from app import _provider_for
        before = os.environ.get("ANTHROPIC_API_KEY")
        _provider_for("sk-ant-not-a-real-key")
        assert os.environ.get("ANTHROPIC_API_KEY") == before

    def test_rate_limit_triggers(self):
        from app import RATE_LIMIT_PER_HOUR, _calls, _rate_limited
        _calls.clear()
        for _ in range(RATE_LIMIT_PER_HOUR):
            assert not _rate_limited()
        assert _rate_limited(), "limit should engage once the quota is used"
        _calls.clear()
