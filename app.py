"""Cross-border AP agent: foreign invoice -> Japan tax rules -> human approval -> PayPal payout.

Run:
    streamlit run app.py
"""
import csv
import io
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

from japan_rules import apply_rules
import paypal_client

CATEGORIES = [
    "service_performed_abroad",
    "service_performed_in_japan",
    "copyright_assignment",
    "license_or_royalty",
    "digital_service",
    "goods",
    "other",
]
LOG_FILE = Path("audit_log.jsonl")

st.set_page_config(page_title="Cross-border AP Agent", layout="wide")
st.title("Cross-border AP Agent")
st.caption("Foreign invoice → Japanese tax rules → human approval → PayPal payout (sandbox)")


# ---------- 1. Load invoice ----------
@st.cache_data(show_spinner="Reading the invoice with Claude...")
def extract_cached(name: str, data: bytes) -> dict:
    from extract_invoice import check_totals, extract  # imported lazily so JSON-only demos need no API key
    tmp = Path(".tmp_upload.pdf")
    tmp.write_bytes(data)
    result = extract(tmp)
    result["check_warnings"] = check_totals(result)
    tmp.unlink(missing_ok=True)
    return result


uploaded = st.file_uploader("Upload a vendor invoice (PDF), or an extracted .json for offline demo", type=["pdf", "json"])
if not uploaded:
    st.info("Upload an invoice to start.")
    st.stop()

invoice = (json.loads(uploaded.getvalue().decode("utf-8")) if uploaded.name.endswith(".json")
           else extract_cached(uploaded.name, uploaded.getvalue()))

# ---------- 2. Settings ----------
with st.sidebar:
    st.header("Payment settings")
    pay_date = st.date_input("Payment date", value=date.today())
    pay_currency = st.selectbox("Pay in", ["JPY", invoice["currency"]],
                                help="JPY: convert the invoice and pay in yen. Otherwise pay as invoiced.")
    ttm = st.number_input("TTM (mid rate)", value=150.00, step=0.01, format="%.2f")
    ttb = st.number_input("TTB on payment date", value=149.00, step=0.01, format="%.2f")
    st.caption("Rate source: your main bank's published rates (demo values).")
    treaty_form = st.checkbox("Tax treaty form (租税条約に関する届出書) filed", value=False)
    approver = st.text_input("Approver name", value="")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Vendor", invoice["vendor_name"])
c2.metric("Country", invoice["vendor_country"])
c3.metric("Invoice", invoice["invoice_number"])
c4.metric("Total", f"{invoice['currency']} {invoice['total']:,.2f}")

# ---------- 3. Review line items (editable) ----------
st.subheader("1. Review the AI's reading — fix the category if it is wrong")
items_df = pd.DataFrame([{
    "description": i["description"],
    "amount": i["amount"],
    "category": i["category"],
    "ai_reason": i["category_reason"],
} for i in invoice["line_items"]])

gb = GridOptionsBuilder.from_dataframe(items_df)
gb.configure_default_column(resizable=True, wrapText=True, autoHeight=True)
gb.configure_column("description", flex=3)
gb.configure_column("amount", type=["numericColumn"], flex=1)
gb.configure_column("category", editable=True, flex=2, cellEditor="agSelectCellEditor",
                    cellEditorParams={"values": CATEGORIES},
                    cellStyle={"backgroundColor": "#FFF8E1"})
gb.configure_column("ai_reason", headerName="Why the AI chose this", flex=4)
grid = AgGrid(items_df, gridOptions=gb.build(), update_mode=GridUpdateMode.VALUE_CHANGED,
              fit_columns_on_grid_load=True, key=f"items_{invoice['invoice_number']}")
edited = pd.DataFrame(grid["data"])

overrides = []
for item, (_, row) in zip(invoice["line_items"], edited.iterrows()):
    if row["category"] != item["category"]:
        overrides.append({"description": item["description"], "from": item["category"], "to": row["category"]})
reviewed = {**invoice, "line_items": [
    {**item, "category": row["category"]} for item, (_, row) in zip(invoice["line_items"], edited.iterrows())
]}

# ---------- 4. Japanese tax decision ----------
decision = apply_rules(reviewed, Decimal(str(ttm)), Decimal(str(ttb)), treaty_form, pay_date,
                       pay_currency, None)

st.subheader("2. Japanese tax decision")
st.dataframe(pd.DataFrame([{
    "Description": l["description"],
    f"{invoice['currency']}": l["amount_fx"],
    "JPY": l["amount_jpy"],
    "Consumption tax": l["consumption_tax"],
    "Withholding (JPY)": l["withholding_jpy"],
    "Account": l["account"],
} for l in decision["lines"]]), hide_index=True, width="stretch")

m1, m2, m3 = st.columns(3)
m1.metric("Total (JPY)", f"{decision['total_jpy']:,}")
m2.metric("Withholding tax (JPY)", f"{decision['withholding_jpy']:,}")
m3.metric("Amount to pay", f"{decision['pay_currency']} {decision['pay_amount']:,}")

if decision["review_flags"]:
    st.warning("**Check before approving**\n\n" + "\n".join(f"- {f}" for f in decision["review_flags"]))
if overrides:
    st.info("Your corrections: " + "; ".join(f"{o['description'][:40]}: {o['from']} → {o['to']}" for o in overrides))


# ---------- 5. Journal entry ----------
def journal_rows(d: dict) -> list[list]:
    """Compound journal entry (debit lines, then credits). Map columns to your accounting software."""
    day = d["payment_date"]
    memo = f"{d['vendor']} {d['invoice_number']}"
    rows = [[day, l["account"], l["amount_jpy"], "対象外", "", None, "", memo] for l in d["lines"]]
    paid_jpy = d["total_jpy"] - d["withholding_jpy"]
    rows.append([day, "", None, "", "PayPal", paid_jpy, "対象外", memo])
    if d["withholding_jpy"]:
        rows.append([day, "", None, "", "預り金（非居住者源泉）", d["withholding_jpy"], "対象外", memo])
    return rows


HEADER = ["日付", "借方勘定科目", "借方金額", "借方税区分", "貸方勘定科目", "貸方金額", "貸方税区分", "摘要"]
st.subheader("3. Journal entry")
st.dataframe(pd.DataFrame(journal_rows(decision), columns=HEADER), hide_index=True, width="stretch")

# ---------- 6. Approve and pay ----------
st.subheader("4. Approve and pay")
receiver = os.getenv("PAYPAL_RECEIVER_EMAIL") or decision["payment_email"]
st.caption(f"Sandbox: the payout is sent to the test account {receiver} instead of the vendor's real email.")
confirm = st.checkbox("I have checked every item above")

if st.button("Approve and pay with PayPal", type="primary", disabled=not (confirm and approver)):
    try:
        batch = paypal_client.create_payout(
            decision["pay_amount"], decision["pay_currency"], receiver, decision["invoice_number"],
            f"Payment for invoice {decision['invoice_number']}"
            + (f" (Japanese withholding tax JPY {decision['withholding_jpy']:,} deducted)" if decision["withholding_jpy"] else ""),
        )
        st.session_state["batch_id"] = batch["payout_batch_id"]
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "approved_at": datetime.now().isoformat(timespec="seconds"),
                "approver": approver,
                "ai_categories": [i["category"] for i in invoice["line_items"]],
                "overrides": overrides,
                "treaty_form": treaty_form,
                "decision": decision,
                "payout_batch_id": batch["payout_batch_id"],
            }, ensure_ascii=False) + "\n")
        st.success(f"Payout created: {batch['payout_batch_id']} ({batch['batch_status']})")
    except Exception as e:  # show the PayPal error to the user instead of crashing
        st.error(f"Payout failed: {e}")
elif not approver:
    st.caption("Enter the approver name in the sidebar to enable payment.")

if "batch_id" in st.session_state:
    if st.button("Check payout status"):
        st.write(paypal_client.get_payout(st.session_state["batch_id"]))

buf = io.StringIO()
csv.writer(buf).writerows([HEADER] + journal_rows(decision))
st.download_button("Download journal CSV", buf.getvalue().encode("utf-8-sig"),
                   file_name=f"journal_{decision['invoice_number']}.csv", mime="text/csv")
