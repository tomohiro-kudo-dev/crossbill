"""Automatically send demo payouts back to the Japanese business account (sandbox only).

Run it in a second terminal while you test the app:
    python auto_refill.py            # keep watching (checks every 60 seconds)
    python auto_refill.py --once     # check once and exit

How it works
1. Reads payout batch IDs that the app wrote to audit_log.jsonl.
2. Asks PayPal for each payout's status and amount (with the main app's credentials).
3. When a payout is SUCCESS and went to the vendor test account, sends the same amount
   back to the Japanese business account (with the vendor app's credentials).
4. Remembers what it has refilled in refill_state.json, so nothing is sent back twice.
"""
import argparse
import json
import os
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # repo root .env

BASE_URL = "https://api-m.sandbox.paypal.com"  # sandbox only
LOG_FILE = Path(os.getenv("AUDIT_LOG", "audit_log.jsonl"))
STATE_FILE = Path("refill_state.json")
VENDOR_EMAIL = os.environ["PAYPAL_RECEIVER_EMAIL"].lower()
SENDER_EMAIL = os.environ["PAYPAL_SENDER_EMAIL"]


def token(client_id: str, secret: str) -> str:
    res = requests.post(f"{BASE_URL}/v1/oauth2/token", auth=(client_id, secret),
                        data={"grant_type": "client_credentials"}, timeout=30)
    res.raise_for_status()
    return res.json()["access_token"]


def main_token() -> str:
    return token(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"])


def vendor_token() -> str:
    return token(os.environ["RETURN_PAYPAL_CLIENT_ID"], os.environ["RETURN_PAYPAL_CLIENT_SECRET"])


def find_batch_ids(obj) -> list[str]:
    """Collect payout_batch_id values anywhere in a log entry (works with different log layouts)."""
    found = []
    if isinstance(obj, dict):
        if obj.get("dry_run") is True:
            return []
        for k, v in obj.items():
            if k == "payout_batch_id" and isinstance(v, str) and not v.upper().startswith("DRY"):
                found.append(v)
            else:
                found += find_batch_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            found += find_batch_ids(v)
    return found


def logged_batch_ids() -> list[str]:
    if not LOG_FILE.exists():
        return []
    ids = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                ids += find_batch_ids(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(dict.fromkeys(ids))


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def payout_item(batch_id: str, tok: str) -> dict | None:
    res = requests.get(f"{BASE_URL}/v1/payments/payouts/{batch_id}",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if res.status_code >= 400:
        return None
    items = res.json().get("items", [])
    if not items:
        return None
    it = items[0]
    return {
        "status": it["transaction_status"],
        "amount": it["payout_item"]["amount"],
        "receiver": it["payout_item"]["receiver"].lower(),
    }


def send_back(amount: dict, original_batch: str, tok: str) -> tuple[bool, str]:
    body = {
        "sender_batch_header": {"sender_batch_id": str(uuid.uuid4()), "email_subject": "Sandbox balance refill"},
        "items": [{
            "recipient_type": "EMAIL",
            "amount": amount,
            "receiver": SENDER_EMAIL,
            "note": f"Refill for {original_batch}",
            "sender_item_id": f"REFILL-{original_batch}"[:30],
        }],
    }
    res = requests.post(f"{BASE_URL}/v1/payments/payouts",
                        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                        json=body, timeout=30)
    data = res.json()
    if res.status_code >= 400:
        return False, f"{data.get('name')}: {data.get('message')}"
    return True, data["batch_header"]["payout_batch_id"]


def run_once() -> None:
    state = load_state()
    pending = [b for b in logged_batch_ids() if b not in state]
    if not pending:
        return
    mtok, vtok = main_token(), vendor_token()
    for batch_id in pending:
        item = payout_item(batch_id, mtok)
        if item is None:
            continue
        if item["receiver"] != VENDOR_EMAIL:
            state[batch_id] = {"skipped": f"sent to {item['receiver']}, not the vendor test account"}
            print(f"[skip] {batch_id}: {state[batch_id]['skipped']}")
        elif item["status"] in ("PENDING", "UNCLAIMED", "ONHOLD"):
            print(f"[wait] {batch_id}: {item['status']}")
            continue  # try again next round
        elif item["status"] != "SUCCESS":
            state[batch_id] = {"skipped": f"payout status {item['status']}"}
            print(f"[skip] {batch_id}: {item['status']}")
        else:
            ok, info = send_back(item["amount"], batch_id, vtok)
            amt = f"{item['amount']['currency']} {item['amount']['value']}"
            if ok:
                state[batch_id] = {"refilled": amt, "refill_batch_id": info}
                print(f"[refill] {batch_id}: sent back {amt} (batch {info})")
            else:
                print(f"[error] {batch_id}: {info} (will retry)")
                continue
        save_state(state)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks")
    a = p.parse_args()
    print(f"Watching {LOG_FILE} for payouts to {VENDOR_EMAIL} ...")
    while True:
        try:
            run_once()
        except requests.RequestException as e:
            print(f"[network] {e} (will retry)")
        if a.once:
            break
        time.sleep(a.interval)
