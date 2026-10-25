"""Wraps japan_rules.apply_rules and builds the journal entry. No tax logic lives here."""
import csv
import io
from datetime import date
from decimal import Decimal

from japan_rules import apply_rules

CATEGORIES = [
    "service_performed_abroad",
    "service_performed_in_japan",
    "copyright_assignment",
    "license_or_royalty",
    "digital_service",
    "goods",
    "other",
]

JOURNAL_HEADER = ["日付", "借方勘定科目", "借方金額", "借方税区分", "貸方勘定科目", "貸方金額", "貸方税区分", "摘要"]


def decide(invoice: dict, settings: dict) -> dict:
    """Run the Japan rule pack with the page settings and attach the journal rows.

    settings = {payment_date, pay_currency, ttm, ttb, pay_rate|None, treaty_form}
    """
    pay_rate = settings.get("pay_rate")
    decision = apply_rules(
        invoice,
        Decimal(str(settings["ttm"])),
        Decimal(str(settings["ttb"])),
        bool(settings["treaty_form"]),
        date.fromisoformat(str(settings["payment_date"])),
        settings.get("pay_currency") or "JPY",
        Decimal(str(pay_rate)) if pay_rate not in (None, "") else None,
    )
    decision["journal_rows"] = journal_rows(decision)
    return decision


def journal_rows(d: dict) -> list[list]:
    """Compound journal entry (debit lines, then credits). Same layout as app.py."""
    day = d["payment_date"]
    memo = f"{d['vendor']} {d['invoice_number']}"
    rows = [[day, l["account"], l["amount_jpy"], "対象外", "", None, "", memo] for l in d["lines"]]
    paid_jpy = d["total_jpy"] - d["withholding_jpy"]
    rows.append([day, "", None, "", "PayPal", paid_jpy, "対象外", memo])
    if d["withholding_jpy"]:
        rows.append([day, "", None, "", "預り金（非居住者源泉）", d["withholding_jpy"], "対象外", memo])
    return rows


def journal_csv(d: dict) -> bytes:
    """Journal CSV for freee / Money Forward import: UTF-8 with BOM, Japanese headers."""
    buf = io.StringIO()
    csv.writer(buf).writerows([JOURNAL_HEADER] + journal_rows(d))
    return buf.getvalue().encode("utf-8-sig")
