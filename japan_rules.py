"""Japan rule pack: apply Japanese tax rules to an extracted foreign invoice.

The AI only describes facts (extract_invoice.py). This module makes the
tax decisions with fixed, auditable rules and flags anything a person
must confirm before payment.

Usage:
    python japan_rules.py invoice_B_logo_copyright.json --ttm 150.00 --ttb 149.00 --treaty-form no
"""
import argparse
import json
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import holidays

WITHHOLDING_RATE = Decimal("0.2042")  # domestic rate: income tax 20% + reconstruction surtax 0.42%
SMALL_ASSET_LIMIT_JPY = 100_000       # demo policy threshold for capitalising copyright (confirm with your advisor)

# Treaty limit rates on royalties (使用料) paid from Japan, applied only when the treaty form
# (租税条約に関する届出書) is filed before payment. When a treaty rate applies, the reconstruction
# surtax is not added on top. Rates below were checked against public treaty summaries; anything
# not listed here is treated as "no verified treaty rate" and withheld at the domestic 20.42%.
TREATY_ROYALTY_RATE = {
    "US": Decimal("0"),
    "GB": Decimal("0"),
    "FR": Decimal("0"),
    "DE": Decimal("0"),
    "SG": Decimal("0.10"),
    "CN": Decimal("0.10"),
    "KR": Decimal("0.10"),
    "IN": Decimal("0.10"),
}
# Countries that have a treaty with Japan but whose royalty rate has NOT been verified in this
# rule pack yet. Add them to TREATY_ROYALTY_RATE once confirmed with the treaty text.
TREATY_RATE_UNVERIFIED = {"IT", "NL", "SE", "CH", "AU", "CA", "IE", "ES", "BE", "TW", "TH", "VN", "MY", "ID"}

ROYALTY_CATEGORIES = ("copyright_assignment", "license_or_royalty")


def is_tax_office_closed(d: date) -> bool:
    """Weekends, Japanese public holidays and the year-end break (Dec 29 - Jan 3)."""
    if d.weekday() >= 5 or d in holidays.Japan(years=d.year):
        return True
    return (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3)


def withholding_due_date(payment_date: date) -> date:
    """10th of the following month, moved to the next business day if closed.
    Non-resident withholding is not eligible for the semi-annual payment special rule."""
    year, month = (payment_date.year + 1, 1) if payment_date.month == 12 else (payment_date.year, payment_date.month + 1)
    due = date(year, month, 10)
    while is_tax_office_closed(due):
        due += timedelta(days=1)
    return due


def yen(value: Decimal) -> int:
    """JPY has no decimals: round down to whole yen."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_DOWN))


def consumption_tax(category: str) -> tuple[str, list[str]]:
    """Return (treatment, review flags) for Japanese consumption tax."""
    if category == "service_performed_abroad":
        return "Out of scope (国外取引・不課税)", []
    if category == "copyright_assignment":
        # Place of supply for a copyright transfer is the transferor's address -> abroad -> out of scope.
        return "Out of scope (国外取引・不課税)", []
    if category == "license_or_royalty":
        return "Out of scope (国外取引・不課税) — confirm delivery method", [
            "License from abroad: if the licensed item is delivered online (software, fonts, stock files), "
            "it may count as 電気通信利用役務の提供. For B2B supplies check reverse charge "
            "(not required if taxable sales ratio >= 95% or simplified taxation); "
            "for consumer-type supplies only registered foreign vendors' invoices are creditable."
        ]
    if category == "digital_service":
        return "Possible reverse charge (リバースチャージ候補)", [
            "Digital service from abroad: check B2B/B2C and whether reverse charge applies "
            "(not required if taxable sales ratio >= 95% or simplified taxation)."
        ]
    if category == "service_performed_in_japan":
        return "Taxable in Japan (国内課税)", [
            "Service performed in Japan: check the vendor's qualified invoice registration number; "
            "if not registered, only 70% of the tax is creditable (Oct 2026 - Sep 2028)."
        ]
    if category == "goods":
        return "Import consumption tax at customs (輸入消費税)", ["Goods: consumption tax is paid at import."]
    return "Needs review (要確認)", ["Category unclear: decide consumption tax treatment manually."]


def pct(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}%"


def withholding(category: str, country: str, is_individual: bool, treaty_form: bool) -> tuple[Decimal, str, list[str]]:
    """Return (withholding rate, basis label, review flags) for Japanese non-resident withholding tax.

    rate 0 means no withholding. basis is one of: none / domestic / treaty.
    """
    if category in ROYALTY_CATEGORIES:
        treaty_rate = TREATY_ROYALTY_RATE.get(country)
        gross_note = "Confirm with the vendor whether the invoice amount is gross or net of withholding."

        if treaty_rate is not None and treaty_form:
            if treaty_rate == 0:
                return Decimal("0"), "treaty", [
                    f"Royalty exempt under the Japan-{country} tax treaty (treaty form on file)."]
            return treaty_rate, "treaty", [
                f"Royalty taxed at the Japan-{country} treaty limit rate {pct(treaty_rate)} "
                "(treaty form on file; no reconstruction surtax on top).", gross_note]

        flags = [f"Royalty paid to a non-resident: domestic {pct(WITHHOLDING_RATE)} withholding applies."]
        if treaty_rate is not None:
            saving = "exempt it" if treaty_rate == 0 else f"reduce it to {pct(treaty_rate)}"
            flags.append(
                f"The Japan-{country} treaty can {saving}. File the treaty form "
                "(租税条約に関する届出書) BEFORE payment; otherwise withhold in full and the vendor "
                "must claim a refund later."
            )
        elif country in TREATY_RATE_UNVERIFIED:
            flags.append(
                f"Japan has a tax treaty with {country}, but its royalty rate is not verified in this rule pack. "
                "Check the treaty text before relying on a reduced rate."
            )
        else:
            flags.append(f"No treaty rate on file for {country}: domestic rate applies.")
        flags.append(gross_note)
        return WITHHOLDING_RATE, "domestic", flags

    if category == "service_performed_in_japan" and is_individual:
        flags = [f"Personal services performed in Japan by a non-resident individual: "
                 f"{pct(WITHHOLDING_RATE)} withholding applies."]
        if country in TREATY_ROYALTY_RATE or country in TREATY_RATE_UNVERIFIED:
            flags.append(
                f"Under the Japan-{country} treaty this may be exempt if the individual has no fixed base "
                "in Japan and the stay is short. Exemption needs the treaty form before payment; "
                "withheld at the domestic rate until confirmed."
            )
        return WITHHOLDING_RATE, "domestic", flags

    return Decimal("0"), "none", []


def account_for(category: str, amount_jpy: int) -> tuple[str, list[str]]:
    if category == "copyright_assignment":
        if amount_jpy >= SMALL_ASSET_LIMIT_JPY:
            return "無形固定資産（著作権）", [
                f"Copyright purchase of JPY {SMALL_ASSET_LIMIT_JPY:,} or more: recorded as an intangible asset "
                "under the demo policy. Confirm capitalisation vs. expensing (and trademark treatment if the "
                "logo will be registered) with your tax advisor."]
        return "支払報酬", []
    if category == "license_or_royalty":
        return "支払手数料", []
    if category == "digital_service":
        return "通信費", []
    if category == "goods":
        return "仕入高", []
    return "外注費", []


def apply_rules(invoice: dict, ttm: Decimal, ttb: Decimal, treaty_form: bool, payment_date: date,
                pay_currency: str = "JPY", pay_rate: Decimal | None = None) -> dict:
    """pay_currency JPY: the foreign amount is converted at pay_rate (default TTM) and paid in yen.
    pay_currency = invoice currency: paid as invoiced; withholding base uses TTB on the payment date."""
    pay_rate = pay_rate or ttm
    pay_in_jpy = pay_currency == "JPY"
    lines = []
    flags = list(invoice.get("check_warnings", []))
    for item in invoice["line_items"]:
        amount = Decimal(str(item["amount"]))
        gross_jpy = yen(amount * (pay_rate if pay_in_jpy else ttm))
        ct, ct_flags = consumption_tax(item["category"])
        wht_rate, wht_basis, wht_flags = withholding(
            item["category"], invoice["vendor_country"], invoice["vendor_is_individual"], treaty_form
        )
        # Withholding base: the yen actually paid when paying in JPY; TTB conversion when paying in
        # foreign currency (所得税基本通達213-1). Tax is rounded down to whole yen.
        wht_base = gross_jpy if pay_in_jpy else yen(amount * ttb)
        wht_jpy = yen(wht_base * wht_rate) if wht_rate else 0
        account, acc_flags = account_for(item["category"], gross_jpy)
        lines.append({
            "description": item["description"],
            "category": item["category"],
            "amount_fx": float(amount),
            "amount_jpy": gross_jpy,
            "consumption_tax": ct,
            "withholding_rate": float(wht_rate),
            "withholding_basis": wht_basis,
            "withholding_jpy": wht_jpy,
            "account": account,
            "ai_reason": item["category_reason"],
        })
        flags += ct_flags + wht_flags + acc_flags

    total_jpy = sum(l["amount_jpy"] for l in lines)
    total_wht = sum(l["withholding_jpy"] for l in lines)
    total_fx = sum(Decimal(str(l["amount_fx"])) for l in lines)
    if pay_in_jpy:
        pay_amount = Decimal(total_jpy - total_wht)
    else:
        # Deduct the yen withholding from the foreign-currency payment at TTB, rounded down to cents.
        pay_amount = (total_fx - Decimal(total_wht) / ttb).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    due = withholding_due_date(payment_date) if total_wht else None
    if due:
        flags.append(
            f"Pay the withholding tax to the tax office by {due.isoformat()} "
            "(10th of next month, next business day if closed; the semi-annual special rule does not apply "
            "to non-resident payments)."
        )

    return {
        "invoice_number": invoice["invoice_number"],
        "vendor": invoice["vendor_name"],
        "vendor_country": invoice["vendor_country"],
        "currency": invoice["currency"],
        "fx_rate_ttm": float(ttm),
        "fx_rate_ttb": float(ttb),
        "fx_rate_payment": float(pay_rate) if pay_in_jpy else None,
        "lines": lines,
        "total_jpy": total_jpy,
        "withholding_jpy": total_wht,
        "pay_currency": "JPY" if pay_in_jpy else invoice["currency"],
        "pay_amount": int(pay_amount) if pay_in_jpy else float(pay_amount),
        "payment_date": payment_date.isoformat(),
        "withholding_due_date": due.isoformat() if due else None,
        "payment_email": invoice["payment_email"],
        "review_flags": list(dict.fromkeys(flags)),  # keep order, drop duplicates
    }


def print_summary(r: dict) -> None:
    print(f"Invoice {r['invoice_number']} / {r['vendor']} ({r['vendor_country']})  TTM {r['fx_rate_ttm']}  TTB {r['fx_rate_ttb']}")
    for l in r["lines"]:
        print(f"- {l['description'][:55]}")
        print(f"    {r['currency']} {l['amount_fx']:,.2f} -> JPY {l['amount_jpy']:,}"
              f" | {l['consumption_tax']} | withholding {pct(Decimal(str(l['withholding_rate'])))}"
              f" ({l['withholding_basis']}) JPY {l['withholding_jpy']:,} | {l['account']}")
    print(f"Total JPY {r['total_jpy']:,} | withholding JPY {r['withholding_jpy']:,} "
          f"| PAY {r['pay_currency']} {r['pay_amount']:,}")
    print("Review before approval:")
    for f in r["review_flags"]:
        print(f"  * {f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("invoice_json")
    p.add_argument("--ttm", default="150.00", help="mid rate on the transaction date (demo value)")
    p.add_argument("--ttb", default="149.00", help="TTB on the payment date, used when paying in foreign currency")
    p.add_argument("--pay-currency", default="JPY", help="JPY (convert and pay in yen) or the invoice currency")
    p.add_argument("--pay-rate", default=None, help="rate used to convert the invoice into yen (default: TTM)")
    p.add_argument("--treaty-form", choices=["yes", "no"], default="no")
    p.add_argument("--pay-date", default=date.today().isoformat(), help="payment date YYYY-MM-DD")
    a = p.parse_args()

    src = Path(a.invoice_json)
    inv = json.loads(src.read_text(encoding="utf-8"))
    result = apply_rules(inv, Decimal(a.ttm), Decimal(a.ttb), a.treaty_form == "yes",
                         date.fromisoformat(a.pay_date), a.pay_currency.upper(),
                         Decimal(a.pay_rate) if a.pay_rate else None)
    out = src.with_name(src.stem + "_decision.json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print_summary(result)
    print(f"\nSaved: {out}")
