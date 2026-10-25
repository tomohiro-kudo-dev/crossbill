"""Lists the sample invoices in samples/ (PDF plus optional pre-extracted JSON)."""
import json
import re
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _title(sample_id: str) -> str:
    # invoice_B_logo_copyright -> "B: logo copyright"
    parts = sample_id.split("_")
    if len(parts) >= 3 and parts[0] == "invoice":
        return f"{parts[1]}: {' '.join(parts[2:])}"
    return sample_id.replace("_", " ")


def pdf_path(sample_id: str) -> Path | None:
    if not _ID_RE.match(sample_id):
        return None
    path = SAMPLES_DIR / f"{sample_id}.pdf"
    return path if path.is_file() else None


def load_json(sample_id: str) -> dict | None:
    if not _ID_RE.match(sample_id):
        return None
    path = SAMPLES_DIR / f"{sample_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def list_samples() -> list[dict]:
    out = []
    for pdf in sorted(SAMPLES_DIR.glob("*.pdf")):
        data = load_json(pdf.stem)
        out.append({
            "id": pdf.stem,
            "title": _title(pdf.stem),
            "vendor_country": data["vendor_country"] if data else None,
            "currency": data["currency"] if data else None,
            "amount": data["total"] if data else None,
            "has_json": data is not None,
        })
    return out
