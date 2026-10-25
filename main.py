"""Crossbill web app (FastAPI). AI reads, rules decide, humans approve.

Run:
    uvicorn main:app --reload
"""
from dotenv import load_dotenv

load_dotenv()  # first, so every module below sees .env; values like PAYOUT_DRY_RUN are still read when used

import logging
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from services import decision as decision_service
from services import extraction
from services import payout as payout_service
from services import samples as sample_service

log = logging.getLogger("crossbill")

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Crossbill")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def check_config() -> None:
    """Validate settings once at startup; problems are shown on /healthz and on the page."""
    app.state.config_warnings = payout_service.config_warnings()
    for w in app.state.config_warnings:
        log.warning("Config: %s", w)


check_config()


# ---------- errors: always {"error": "..."}, never a stack trace ----------
@app.exception_handler(extraction.ExtractionError)
@app.exception_handler(payout_service.PayoutError)
async def service_error(_: Request, exc: extraction.ExtractionError | payout_service.PayoutError):
    return JSONResponse({"error": exc.message, **getattr(exc, "details", {})}, status_code=exc.status)


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


class Override(BaseModel):
    description: str
    from_: str = Field(alias="from")
    to: str

    def as_log(self) -> dict:
        return {"description": self.description, "from": self.from_, "to": self.to}


class ApproveRequest(DecideRequest):
    decision: dict
    approver: str
    overrides: list[Override] = []


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "dry_run": payout_service.is_dry_run(),
        "config_warnings": app.state.config_warnings,
        "categories": decision_service.CATEGORIES,
        "journal_header": decision_service.JOURNAL_HEADER,
    })


@app.get("/healthz")
def healthz():
    warnings = app.state.config_warnings
    return {"ok": True, "dry_run": payout_service.is_dry_run(),
            "receiver_email_ok": not warnings, "warnings": warnings}


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
        return decision_service.decide(req.invoice, req.settings.model_dump(mode="json"))
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


# ---------- live AI extraction (rate limited) ----------
@app.post("/api/samples/{sample_id}/extract")
async def sample_extract(sample_id: str, request: Request):
    path = sample_service.pdf_path(sample_id)
    if not path:
        raise HTTPException(404, f"Unknown sample: {sample_id}")
    ip = extraction.client_ip(request.headers, request.client.host if request.client else None)
    return await run_in_threadpool(extraction.extract_pdf, path.read_bytes(), ip)


@app.post("/api/extract")
async def extract_upload(request: Request, file: UploadFile = File(...)):
    data = await file.read(extraction.max_upload_bytes() + 1)  # never read more than the limit
    ip = extraction.client_ip(request.headers, request.client.host if request.client else None)
    return await run_in_threadpool(extraction.extract_pdf, data, ip)


# ---------- approval and payout ----------
@app.post("/api/approve")
def approve(req: ApproveRequest):
    try:
        return payout_service.approve(req.invoice, req.settings.model_dump(mode="json"), req.decision,
                                      req.approver, [o.as_log() for o in req.overrides])
    except (KeyError, TypeError, ValueError, ArithmeticError) as e:
        raise HTTPException(400, f"Could not apply the rules to this invoice: {type(e).__name__} {e}")


@app.get("/api/payout/{batch_id}")
def payout_status(batch_id: str):
    return payout_service.status(batch_id)
