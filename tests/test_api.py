"""API and page smoke tests. Claude and PayPal are never called here."""
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_page_renders_in_dry_run(monkeypatch):
    monkeypatch.delenv("PAYOUT_DRY_RUN", raising=False)
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
