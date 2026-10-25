"""Read an invoice PDF with Claude and save the extracted data as JSON.

Usage:
    python extract_invoice.py invoice_A_service_only.pdf
"""
import base64
import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-5-5"

EXTRACT_TOOL = {
    "name": "record_invoice",
    "description": "Record the structured contents of a vendor invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor_name": {"type": "string"},
            "vendor_country": {"type": "string", "description": "ISO 3166-1 alpha-2 code, e.g. US"},
            "vendor_is_individual": {
                "type": "boolean",
                "description": "True if the vendor appears to be an individual / sole proprietor rather than a corporation",
            },
            "invoice_number": {"type": "string"},
            "issue_date": {"type": "string", "description": "YYYY-MM-DD"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD"},
            "currency": {"type": "string", "description": "ISO 4217 code, e.g. USD"},
            "total": {"type": "number"},
            "tax_shown": {"type": "number", "description": "Tax amount printed on the invoice (0 if none)"},
            "payment_email": {"type": "string", "description": "PayPal or payment email, empty if none"},
            "work_location": {"type": "string", "description": "Where the work was performed, as stated on the invoice; empty if not stated"},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit_price": {"type": "number"},
                        "amount": {"type": "number"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "service_performed_abroad",
                                "service_performed_in_japan",
                                "copyright_assignment",
                                "license_or_royalty",
                                "digital_service",
                                "goods",
                                "other",
                            ],
                            "description": "Best guess of the nature of this line item",
                        },
                        "category_reason": {
                            "type": "string",
                            "description": "Short reason for the category, quoting the words on the invoice that support it",
                        },
                    },
                    "required": ["description", "quantity", "unit_price", "amount", "category", "category_reason"],
                },
            },
            "notes_summary": {"type": "string", "description": "One-sentence summary of notes and terms on the invoice"},
        },
        "required": [
            "vendor_name", "vendor_country", "vendor_is_individual", "invoice_number", "issue_date",
            "due_date", "currency", "total", "tax_shown", "payment_email", "work_location",
            "line_items", "notes_summary",
        ],
    },
}

PROMPT = (
    "You are an accounts-payable assistant for a Japanese company. "
    "Read this vendor invoice and record its contents with the record_invoice tool. "
    "Copy numbers exactly as printed. For each line item, choose the category that best "
    "describes what is being paid for, and explain why using words from the invoice. "
    "Do not decide tax treatment; only describe the facts."
)


def extract(pdf_path: Path) -> dict:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode()
    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )
    for block in message.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Claude did not return structured data")


def check_totals(data: dict) -> list[str]:
    """Simple arithmetic checks so a misread number is caught before payment."""
    warnings = []
    for item in data["line_items"]:
        if abs(item["quantity"] * item["unit_price"] - item["amount"]) > 0.01:
            warnings.append(f"Line amount mismatch: {item['description']}")
    line_sum = sum(i["amount"] for i in data["line_items"])
    if abs(line_sum + data["tax_shown"] - data["total"]) > 0.01:
        warnings.append(f"Total mismatch: lines {line_sum} + tax {data['tax_shown']} != total {data['total']}")
    return warnings


if __name__ == "__main__":
    path = Path(sys.argv[1])
    result = extract(path)
    result["check_warnings"] = check_totals(result)
    out = path.with_suffix(".json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nSaved: {out}")
