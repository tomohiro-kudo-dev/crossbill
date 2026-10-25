"""Wraps paypal_client: DRY_RUN switch, approval checks and the audit log writer."""
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

import paypal_client
from services.decision import decide

log = logging.getLogger("crossbill.payout")
_log_lock = threading.Lock()
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class PayoutError(Exception):
    """An error with an HTTP status and a message that is safe to show to the user."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_dry_run() -> bool:
    """A real payout happens only when PAYOUT_DRY_RUN is explicitly false."""
    return os.getenv("PAYOUT_DRY_RUN", "true").strip().lower() != "false"


def audit_log_path() -> Path:
    # same variable and default as tools/auto_refill.py
    return Path(os.getenv("AUDIT_LOG", "audit_log.jsonl"))


def _ai_categories(invoice: dict, overrides: list[dict]) -> list[str]:
    """Categories as the AI read them: the reviewed category, undone where the human overrode it."""
    by_desc = {o["description"]: o["from"] for o in overrides}
    return [by_desc.get(i["description"], i["category"]) for i in invoice["line_items"]]


def _note(decision: dict) -> str:
    note = f"Payment for invoice {decision['invoice_number']}"
    if decision["withholding_jpy"]:
        note += f" (Japanese withholding tax JPY {decision['withholding_jpy']:,} deducted)"
    return note


def approve(invoice: dict, settings: dict, decision: dict, approver: str, overrides: list[dict]) -> dict:
    approver = approver.strip()
    if not approver:
        raise PayoutError(400, "Approver name is required.")

    # Rules decide: recompute on the server and pay only what the approver saw on screen.
    final = decide(invoice, settings)
    seen = (decision.get("pay_amount"), decision.get("pay_currency"), decision.get("withholding_jpy"))
    if seen != (final["pay_amount"], final["pay_currency"], final["withholding_jpy"]):
        raise PayoutError(409, "The amounts changed since you reviewed them. Check the page again and re-approve.")

    dry_run = is_dry_run()
    if dry_run:
        batch = {"payout_batch_id": f"DRY-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}", "batch_status": "PENDING"}
        receiver = os.getenv("PAYPAL_RECEIVER_EMAIL") or final["payment_email"]
    else:
        receiver = os.getenv("PAYPAL_RECEIVER_EMAIL")
        if not (os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET") and receiver):
            raise PayoutError(503, "PayPal is not configured on this server "
                                   "(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_RECEIVER_EMAIL).")
        try:
            batch = paypal_client.create_payout(final["pay_amount"], final["pay_currency"], receiver,
                                                final["invoice_number"], _note(final))
        except RuntimeError as e:  # PayPal API error: name and message only
            raise PayoutError(502, f"PayPal rejected the payout: {e}")
        except requests.RequestException as e:
            log.warning("PayPal request failed: %s", type(e).__name__)
            raise PayoutError(502, "Could not reach PayPal. Nothing was paid; please try again.")

    entry = {
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approver": approver,
        "invoice_number": final["invoice_number"],
        "vendor_country": final["vendor_country"],
        "ai_categories": _ai_categories(invoice, overrides),
        "overrides": overrides,
        "settings": settings,
        "decision": final,
        "receiver": receiver,
        "dry_run": dry_run,
        "payout_batch_id": batch["payout_batch_id"],
        "batch_status": batch.get("batch_status"),
    }
    try:
        with _log_lock, audit_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # the payout already exists, so report it rather than fail; the server log keeps the ID
        log.error("Could not write audit log for payout %s", batch["payout_batch_id"])

    return {"payout_batch_id": batch["payout_batch_id"], "batch_status": batch.get("batch_status"), "dry_run": dry_run}


def status(batch_id: str) -> dict:
    if not _BATCH_ID_RE.match(batch_id):
        raise PayoutError(400, "Invalid payout batch ID.")
    if batch_id.upper().startswith("DRY-"):
        return {"payout_batch_id": batch_id, "batch_status": "SUCCESS", "item_status": "SUCCESS", "dry_run": True}
    if not (os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET")):
        raise PayoutError(503, "PayPal is not configured on this server.")
    try:
        res = paypal_client.get_payout(batch_id)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        raise PayoutError(404 if code == 404 else 502, "Payout not found." if code == 404 else "PayPal status check failed.")
    except requests.RequestException:
        raise PayoutError(502, "Could not reach PayPal.")
    return {"payout_batch_id": batch_id, **res, "dry_run": False}
