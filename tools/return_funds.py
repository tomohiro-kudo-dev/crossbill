"""Send sandbox funds back from the vendor test account to the Japanese business account.

Use this after demo payouts to refill the sender's sandbox JPY balance.
It runs with a SECOND sandbox app that is linked to the vendor's Business account.

.env:
    RETURN_PAYPAL_CLIENT_ID=...       # app linked to the US Business (vendor) sandbox account
    RETURN_PAYPAL_CLIENT_SECRET=...
    PAYPAL_SENDER_EMAIL=sb-xxxx@business.example.com   # the Japanese business account (payer in the demo)

Usage:
    python return_funds.py 5000 JPY        # send 5,000 yen back
    python return_funds.py 30.00 USD       # if the vendor account converted the funds to USD
    python return_funds.py --check BATCH_ID
"""
import argparse
import os
import sys
import time
import uuid
from decimal import Decimal

from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # repo root .env

BASE_URL = "https://api-m.sandbox.paypal.com"  # sandbox only: never point this at live


def token() -> str:
    res = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(os.environ["RETURN_PAYPAL_CLIENT_ID"], os.environ["RETURN_PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def fmt(amount: str, currency: str) -> str:
    return str(int(Decimal(amount))) if currency == "JPY" else f"{Decimal(amount):.2f}"


def send_back(amount: str, currency: str) -> str:
    receiver = os.environ["PAYPAL_SENDER_EMAIL"]
    body = {
        "sender_batch_header": {
            "sender_batch_id": str(uuid.uuid4()),
            "email_subject": "Sandbox balance refill",
            "email_message": "Returning demo funds",
        },
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"value": fmt(amount, currency), "currency": currency},
            "receiver": receiver,
            "note": "Sandbox balance refill",
            "sender_item_id": f"REFILL-{uuid.uuid4().hex[:8]}",
        }],
    }
    res = requests.post(
        f"{BASE_URL}/v1/payments/payouts",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    data = res.json()
    if res.status_code >= 400:
        print(f"Failed: {data.get('name')} - {data.get('message')}")
        if data.get("name") == "INSUFFICIENT_FUNDS":
            other = "USD" if currency == "JPY" else "JPY"
            print(f"The vendor account may hold the funds in another currency. Try: python return_funds.py <amount> {other}")
        sys.exit(1)
    batch_id = data["batch_header"]["payout_batch_id"]
    print(f"Sent {currency} {fmt(amount, currency)} to {receiver} / batch {batch_id}")
    return batch_id


def check(batch_id: str) -> None:
    res = requests.get(
        f"{BASE_URL}/v1/payments/payouts/{batch_id}",
        headers={"Authorization": f"Bearer {token()}"},
        timeout=30,
    )
    res.raise_for_status()
    data = res.json()
    print("Batch status:", data["batch_header"]["batch_status"])
    for item in data.get("items", []):
        print("Item status:", item["transaction_status"], item["payout_item"]["amount"])
        for err in [item.get("errors")] if item.get("errors") else []:
            print("Reason:", err.get("name"), "-", err.get("message"))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("amount", nargs="?")
    p.add_argument("currency", nargs="?", default="JPY")
    p.add_argument("--check", metavar="BATCH_ID")
    a = p.parse_args()

    if a.check:
        check(a.check)
    elif a.amount:
        bid = send_back(a.amount, a.currency.upper())
        time.sleep(5)
        check(bid)
    else:
        p.print_help()
