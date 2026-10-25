"""Crossbill web app (FastAPI). AI reads, rules decide, humans approve.

Run:
    uvicorn main:app --reload
"""
import logging
import os
from datetime import date
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from services import decision as decision_service
from services import samples as sample_service

load_dotenv()
log = logging.getLogger("crossbill")

app = FastAPI(title="Crossbill")


def dry_run() -> bool:
    """A real payout happens only when PAYOUT_DRY_RUN is explicitly false."""
    return os.getenv("PAYOUT_DRY_RUN", "true").strip().lower() != "false"


# ---------- errors: always {"error": "..."}, never a stack trace ----------
@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    return JSONResponse({"error": f"Invalid request: {where} {first.get('msg', '')}".strip()}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse({"error": "Internal error. Please try again."}, status_code=500)


# ---------- request models ----------
class Settings(BaseModel):
    payment_date: date
    pay_currency: str = "JPY"
    ttm: float
    ttb: float
    pay_rate: float | None = None
    treaty_form: bool = False


class DecideRequest(BaseModel):
    invoice: dict
    settings: Settings


# ---------- routes ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": dry_run()}


@app.get("/api/samples")
def samples():
    return sample_service.list_samples()


@app.get("/api/samples/{sample_id}")
def sample(sample_id: str):
    if not sample_service.pdf_path(sample_id):
        raise HTTPException(404, f"Unknown sample: {sample_id}")
    data = sample_service.load_json(sample_id)
    if data is None:
        raise HTTPException(404, f"No pre-extracted JSON for sample {sample_id}; run live extraction instead.")
    return {"id": sample_id, "invoice": data, "pdf_url": f"/api/samples/{sample_id}/pdf"}


@app.get("/api/samples/{sample_id}/pdf")
def sample_pdf(sample_id: str):
    path = sample_service.pdf_path(sample_id)
    if not path:
        raise HTTPException(404, f"Unknown sample: {sample_id}")
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{path.name}"'})


@app.post("/api/decide")
def decide(req: DecideRequest):
    try:
        return decision_service.decide(req.invoice, req.settings.model_dump())
    except (KeyError, TypeError, ValueError, ArithmeticError) as e:
        raise HTTPException(400, f"Could not apply the rules to this invoice: {type(e).__name__} {e}")


@app.post("/api/journal.csv")
def journal_csv(d: dict):
    try:
        body = decision_service.journal_csv(d)
    except (KeyError, TypeError) as e:
        raise HTTPException(400, f"Not a decision object (missing {e}).")
    name = f"journal_{d.get('invoice_number', 'invoice')}.csv"
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
