"""Minimal PayPal Payouts client (sandbox)."""
import os
import uuid
from decimal import Decimal

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")


def _token() -> str:
    res = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def format_amount(amount, currency: str) -> str:
    """JPY has no decimals; most other currencies use two."""
    if currency == "JPY":
        return str(int(Decimal(str(amount))))
    return f"{Decimal(str(amount)):.2f}"


def create_payout(amount, currency: str, receiver: str, invoice_number: str, note: str) -> dict:
    body = {
        "sender_batch_header": {
            "sender_batch_id": str(uuid.uuid4()),
            "email_subject": f"Payment for invoice {invoice_number}",
            "email_message": note,
        },
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"value": format_amount(amount, currency), "currency": currency},
            "receiver": receiver,
            "note": note,
            "sender_item_id": invoice_number,
        }],
    }
    res = requests.post(
        f"{BASE_URL}/v1/payments/payouts",
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    data = res.json()
    if res.status_code >= 400:
        raise RuntimeError(f"{data.get('name')}: {data.get('message')}")
    return data["batch_header"]


def get_payout(batch_id: str) -> dict:
    res = requests.get(
        f"{BASE_URL}/v1/payments/payouts/{batch_id}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    res.raise_for_status()
    data = res.json()
    items = data.get("items", [])
    return {
        "batch_status": data["batch_header"]["batch_status"],
        "item_status": items[0]["transaction_status"] if items else None,
    }
