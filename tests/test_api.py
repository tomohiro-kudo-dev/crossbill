"""API and page smoke tests. Claude and PayPal are never called here: both are mocked."""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

import extract_invoice
import paypal_client
from main import app
from services import extraction, samples

client = TestClient(app)

SETTINGS = {"payment_date": "2026-11-15", "pay_currency": "JPY", "ttm": 150.00, "ttb": 149.00,
            "pay_rate": None, "treaty_form": False}
PDF = b"%PDF-1.4\n% test\n"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Every test: dry run, temp audit log, fresh rate limit, and no real Claude or PayPal call possible."""
    monkeypatch.delenv("PAYOUT_DRY_RUN", raising=False)
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "audit_log.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXTRACT_RATE_LIMIT_PER_HOUR", "30")
    monkeypatch.setenv("MAX_UPLOAD_MB", "5")
    monkeypatch.setattr(extract_invoice, "MODEL", extract_invoice.MODEL)  # restored after each test
    extraction.limiter.reset()

    def forbidden(*a, **k):
        raise AssertionError("external API must not be called in tests")
    monkeypatch.setattr(extract_invoice, "extract", forbidden)
    monkeypatch.setattr(paypal_client, "create_payout", forbidden)
    monkeypatch.setattr(paypal_client, "get_payout", forbidden)
    return tmp_path / "audit_log.jsonl"


def fake_extract(monkeypatch, sample_id="invoice_B_logo_copyright"):
    data = samples.load_json(sample_id)
    data.pop("check_warnings")
    calls = []

    def fake(path):
        calls.append(path.read_bytes())
        return json.loads(json.dumps(data))
    monkeypatch.setattr(extract_invoice, "extract", fake)
    return calls


def upload(content=PDF, name="inv.pdf", **kw):
    return client.post("/api/extract", files={"file": (name, content, "application/pdf")}, **kw)


def sample_b():
    return client.get("/api/samples/invoice_B_logo_copyright").json()["invoice"]


def approve_body(invoice=None, settings=SETTINGS, **extra):
    invoice = invoice or sample_b()
    decision = client.post("/api/decide", json={"invoice": invoice, "settings": settings}).json()
    return {"invoice": invoice, "settings": settings, "decision": decision, "approver": "T. Kudo",
            "overrides": [], **extra}


# ---------- page ----------
def test_page_renders_in_dry_run():
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "AI reads · rules decide · humans approve" in html
    assert "Approve (simulated payout)" in html and "PAYOUT_DRY_RUN=true" in html
    assert '"copyright_assignment"' in html  # categories handed to the grid


def test_page_real_mode_button(monkeypatch):
    monkeypatch.setenv("PAYOUT_DRY_RUN", "false")
    html = client.get("/").text
    assert "Approve and pay with PayPal" in html and "PAYOUT_DRY_RUN=true" not in html


def test_static_assets_served():
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


# ---------- /api/extract ----------
def test_upload_extracts_with_check_warnings(monkeypatch):
    calls = fake_extract(monkeypatch)
    r = upload()
    assert r.status_code == 200
    assert r.json()["invoice_number"] == "MCDS-2026-047" and r.json()["check_warnings"] == []
    assert calls == [PDF]


def test_model_comes_from_env(monkeypatch):
    fake_extract(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test-model")
    upload()
    assert extract_invoice.MODEL == "claude-test-model"


@pytest.mark.parametrize("name, content, status", [
    ("notes.txt", b"hello, not a pdf", 415),
    ("fake.pdf", b"PK\x03\x04 zip renamed to pdf", 415),
    ("empty.pdf", b"", 400),
])
def test_upload_rejects_bad_files(monkeypatch, name, content, status):
    calls = fake_extract(monkeypatch)
    r = upload(content, name)
    assert r.status_code == status and "error" in r.json()
    assert calls == []


def test_upload_rejects_oversized_file(monkeypatch):
    calls = fake_extract(monkeypatch)
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    r = upload(PDF + b"0" * (1024 * 1024), "big.pdf")
    assert r.status_code == 413 and "1 MB" in r.json()["error"]
    assert calls == []


def test_upload_without_file_is_json_error():
    r = client.post("/api/extract")
    assert r.status_code == 422 and "error" in r.json()


def test_extract_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    r = upload()
    assert r.status_code == 503 and "sample" in r.json()["error"]


def test_ai_failure_is_clean_json(monkeypatch):
    def boom(path):
        raise ConnectionError("secret details sk-ant-xyz")
    monkeypatch.setattr(extract_invoice, "extract", boom)
    r = upload()
    assert r.status_code == 502 and "error" in r.json() and "sk-ant" not in r.text


def test_rate_limit_per_ip(monkeypatch):
    fake_extract(monkeypatch)
    monkeypatch.setenv("EXTRACT_RATE_LIMIT_PER_HOUR", "2")

    def send(ip):  # a spoofed first entry must not matter: the proxy's last entry counts
        return upload(headers={"X-Forwarded-For": f"6.6.6.6, {ip}"})
    assert [send("1.1.1.1").status_code for _ in range(3)] == [200, 200, 429]
    assert "limit" in send("1.1.1.1").json()["error"]
    assert send("2.2.2.2").status_code == 200  # other visitors are not blocked

    extraction.limiter.reset()
    for _ in range(3):  # invalid uploads do not use up the allowance
        upload(b"nope", "x.txt", headers={"X-Forwarded-For": "1.1.1.1"})
    assert send("1.1.1.1").status_code == 200


def test_live_sample_extraction(monkeypatch):
    calls = fake_extract(monkeypatch)
    r = client.post("/api/samples/invoice_B_logo_copyright/extract")
    assert r.status_code == 200 and r.json()["total"] == 600.0
    assert calls[0].startswith(b"%PDF")
    assert client.post("/api/samples/nope/extract").status_code == 404


# ---------- /api/approve and /api/payout ----------
def test_approve_dry_run_writes_audit_and_never_calls_paypal(isolated):
    inv = sample_b()
    inv["line_items"][1]["category"] = "service_performed_abroad"
    override = {"description": inv["line_items"][1]["description"], "from": "copyright_assignment",
                "to": "service_performed_abroad"}
    r = client.post("/api/approve", json=approve_body(inv, overrides=[override]))
    assert r.status_code == 200
    res = r.json()
    assert res["dry_run"] is True and res["payout_batch_id"].startswith("DRY-")

    lines = isolated.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["payout_batch_id"] == res["payout_batch_id"] and entry["dry_run"] is True
    assert entry["approver"] == "T. Kudo" and entry["invoice_number"] == "MCDS-2026-047"
    assert entry["vendor_country"] == "US"
    assert entry["ai_categories"] == ["service_performed_abroad", "copyright_assignment"]
    assert entry["overrides"] == [override]
    assert entry["settings"]["payment_date"] == "2026-11-15"
    assert entry["decision"]["pay_amount"] == 90_000

    s = client.get(f"/api/payout/{res['payout_batch_id']}").json()
    assert s["batch_status"] == "SUCCESS" and s["dry_run"] is True


def test_approve_real_mode_calls_paypal_with_integer_jpy(monkeypatch, isolated):
    monkeypatch.setenv("PAYOUT_DRY_RUN", "false")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PAYPAL_RECEIVER_EMAIL", "sb-test@business.example.com")
    sent = []

    def fake_create(amount, currency, receiver, invoice_number, note):
        sent.append((amount, currency, receiver, invoice_number, note))
        return {"payout_batch_id": "5UXD2E8A7EBQJ", "batch_status": "PENDING"}
    monkeypatch.setattr(paypal_client, "create_payout", fake_create)
    monkeypatch.setattr(paypal_client, "get_payout", lambda b: {"batch_status": "SUCCESS", "item_status": "SUCCESS"})

    r = client.post("/api/approve", json=approve_body())
    assert r.json() == {"payout_batch_id": "5UXD2E8A7EBQJ", "batch_status": "PENDING", "dry_run": False}
    amount, currency, receiver, number, note = sent[0]
    assert (amount, currency, receiver, number) == (83_874, "JPY", "sb-test@business.example.com", "MCDS-2026-047")
    assert isinstance(amount, int) and "JPY 6,126" in note
    entry = json.loads(isolated.read_text(encoding="utf-8"))
    assert entry["dry_run"] is False and entry["payout_batch_id"] == "5UXD2E8A7EBQJ"
    assert client.get("/api/payout/5UXD2E8A7EBQJ").json()["batch_status"] == "SUCCESS"


def test_real_mode_without_config_is_refused(monkeypatch, isolated):
    monkeypatch.setenv("PAYOUT_DRY_RUN", "false")
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_RECEIVER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    r = client.post("/api/approve", json=approve_body())
    assert r.status_code == 503 and not isolated.exists()


def test_paypal_error_is_reported_and_not_logged(monkeypatch, isolated):
    monkeypatch.setenv("PAYOUT_DRY_RUN", "false")
    for k, v in (("PAYPAL_CLIENT_ID", "id"), ("PAYPAL_CLIENT_SECRET", "s"), ("PAYPAL_RECEIVER_EMAIL", "sb@x.example")):
        monkeypatch.setenv(k, v)

    def reject(*a):
        raise RuntimeError("INSUFFICIENT_FUNDS: Sender does not have sufficient funds.")
    monkeypatch.setattr(paypal_client, "create_payout", reject)
    r = client.post("/api/approve", json=approve_body())
    assert r.status_code == 502 and "INSUFFICIENT_FUNDS" in r.json()["error"]
    assert not isolated.exists()


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "", "anything"])
def test_only_explicit_false_disables_dry_run(monkeypatch, value):
    monkeypatch.setenv("PAYOUT_DRY_RUN", value)
    assert client.post("/api/approve", json=approve_body()).json()["dry_run"] is True


def test_tampered_decision_is_rejected(isolated):
    body = approve_body()
    body["decision"]["pay_amount"] = 90_000  # page claims no withholding; the rules say 83,874
    r = client.post("/api/approve", json=body)
    assert r.status_code == 409 and not isolated.exists()


def test_approver_required(isolated):
    r = client.post("/api/approve", json=approve_body(approver="   "))
    assert r.status_code == 400 and "Approver" in r.json()["error"]


def test_payout_status_rejects_bad_ids():
    r = client.get("/api/payout/bad%20id!")
    assert r.status_code == 400 and "error" in r.json()


def test_audit_log_works_with_auto_refill(monkeypatch, isolated):
    """tools/auto_refill.py must skip dry-run lines and pick up real batch IDs."""
    monkeypatch.setenv("PAYPAL_RECEIVER_EMAIL", "sb@x.example")
    monkeypatch.setenv("PAYPAL_SENDER_EMAIL", "sb2@x.example")
    auto_refill = importlib.import_module("tools.auto_refill")
    client.post("/api/approve", json=approve_body())
    dry_entry = json.loads(isolated.read_text(encoding="utf-8"))
    assert auto_refill.find_batch_ids(dry_entry) == []
    real_entry = {**dry_entry, "dry_run": False, "payout_batch_id": "5UXD2E8A7EBQJ"}
    assert auto_refill.find_batch_ids(real_entry) == ["5UXD2E8A7EBQJ"]
