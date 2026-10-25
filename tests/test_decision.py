"""Expected numbers from docs/rebuild_brief.md (acceptance table). No external API calls."""
import pytest
from fastapi.testclient import TestClient

from main import app
from services import samples
from services.decision import decide

SETTINGS = {"payment_date": "2026-11-15", "pay_currency": "JPY", "ttm": 150.00, "ttb": 149.00,
            "pay_rate": None, "treaty_form": False}

client = TestClient(app)


def run(sample_id, **overrides):
    return decide(samples.load_json(sample_id), {**SETTINGS, **overrides})


@pytest.mark.parametrize("sample_id, treaty, total, wht, pay", [
    ("invoice_A_service_only", False, 67_500, 0, 67_500),
    ("invoice_A_service_only", True, 67_500, 0, 67_500),
    ("invoice_B_logo_copyright", False, 90_000, 6_126, 83_874),
    ("invoice_B_logo_copyright", True, 90_000, 0, 90_000),
])
def test_acceptance_table(sample_id, treaty, total, wht, pay):
    d = run(sample_id, treaty_form=treaty)
    assert (d["total_jpy"], d["withholding_jpy"], d["pay_amount"]) == (total, wht, pay)
    assert d["pay_currency"] == "JPY"


def test_withholding_basis_labels():
    no_form = run("invoice_B_logo_copyright")["lines"]
    assert [(l["withholding_basis"], l["withholding_rate"]) for l in no_form] == [("none", 0), ("domestic", 0.2042)]
    with_form = run("invoice_B_logo_copyright", treaty_form=True)["lines"]
    assert with_form[1]["withholding_basis"] == "treaty"


def test_category_override_removes_withholding():
    inv = samples.load_json("invoice_B_logo_copyright")
    inv["line_items"][1]["category"] = "service_performed_abroad"
    d = decide(inv, SETTINGS)
    assert d["withholding_jpy"] == 0
    assert d["pay_amount"] == 90_000


def test_due_date_moves_past_new_year_break():
    d = run("invoice_B_logo_copyright", payment_date="2026-12-15")
    assert d["withholding_due_date"] == "2027-01-12"
    assert any("2027-01-12" in f for f in d["review_flags"])


def test_journal_rows_balance():
    d = run("invoice_B_logo_copyright")
    debit = sum(r[2] or 0 for r in d["journal_rows"])
    credit = sum(r[5] or 0 for r in d["journal_rows"])
    assert debit == credit == 90_000
    assert ["2026-11-15", "", None, "", "預り金（非居住者源泉）", 6_126, "対象外",
            "Maya Chen Design Studio MCDS-2026-047"] in d["journal_rows"]


# ---------- API ----------
def test_healthz_defaults_to_dry_run(monkeypatch):
    monkeypatch.delenv("PAYOUT_DRY_RUN", raising=False)
    body = client.get("/healthz").json()
    assert body["ok"] is True and body["dry_run"] is True


def test_samples_endpoints():
    listing = client.get("/api/samples").json()
    ids = {s["id"] for s in listing}
    assert {"invoice_A_service_only", "invoice_B_logo_copyright"} <= ids
    assert all(s["has_json"] for s in listing)

    one = client.get("/api/samples/invoice_B_logo_copyright").json()
    assert one["invoice"]["invoice_number"] == "MCDS-2026-047"
    pdf = client.get(one["pdf_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    missing = client.get("/api/samples/nope")
    assert missing.status_code == 404 and "error" in missing.json()
    assert client.get("/api/samples/..%2Fmain/pdf").status_code == 404


def test_decide_and_journal_csv_api():
    inv = client.get("/api/samples/invoice_B_logo_copyright").json()["invoice"]
    r = client.post("/api/decide", json={"invoice": inv, "settings": SETTINGS})
    assert r.status_code == 200
    d = r.json()
    assert d["pay_amount"] == 83_874 and len(d["journal_rows"]) == 4

    csv = client.post("/api/journal.csv", json=d)
    assert csv.status_code == 200
    assert csv.content.startswith(b"\xef\xbb\xbf")
    assert csv.content.decode("utf-8-sig").splitlines()[0] == "日付,借方勘定科目,借方金額,借方税区分,貸方勘定科目,貸方金額,貸方税区分,摘要"


def test_decide_bad_input_returns_json_error():
    r = client.post("/api/decide", json={"invoice": {}, "settings": SETTINGS})
    assert r.status_code == 400 and "error" in r.json()
    r = client.post("/api/decide", json={"invoice": {}, "settings": {"ttm": "abc"}})
    assert r.status_code == 422 and "error" in r.json()
