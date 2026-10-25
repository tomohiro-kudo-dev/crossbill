# Rebuild brief: FastAPI version of Crossbill

Read `CLAUDE.md` first. Then work through the milestones below in order. After each milestone: run the
tests, show me a short summary of what changed and how to try it, commit, and wait for my go-ahead.

## Goal
Replace the Streamlit reference UI (`app.py`) with a FastAPI web app that judges can open in a browser
(deployed on Render) and that shows the full flow on one screen:

upload invoice → AI reading → human review (AG Grid) → Japan tax decision → journal entry → approve → PayPal payout.

Reuse the existing modules as they are: `extract_invoice.py` (`extract`, `check_totals`),
`japan_rules.py` (`apply_rules`), `paypal_client.py` (`create_payout`, `get_payout`, `format_amount`).

## Target layout
```
main.py                 FastAPI app, routes
services/
  extraction.py         wraps extract_invoice.extract + check_totals, handles upload temp files
  decision.py           wraps japan_rules.apply_rules, builds journal rows
  payout.py             wraps paypal_client, DRY_RUN switch, audit log writer
  samples.py            lists samples/*.pdf with matching *.json
templates/
  index.html            single page (Jinja2)
static/
  app.js                page logic (fetch calls, AG Grid, state)
  styles.css            small overrides only (Tailwind via CDN does the rest)
tests/
  test_decision.py      expected numbers for samples A and B
  test_api.py           API smoke tests with mocked Claude/PayPal
render.yaml             Render blueprint
```
Keep `app.py` (Streamlit) untouched as a fallback.

## Environment variables
| Name | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for live extraction |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | model for extraction |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | — | sandbox app of the paying (JP) business account |
| `PAYPAL_RECEIVER_EMAIL` | — | sandbox test account that receives demo payouts |
| `PAYOUT_DRY_RUN` | `true` | `true` = simulate, `false` = real sandbox payout |
| `MAX_UPLOAD_MB` | `5` | upload size limit |
| `EXTRACT_RATE_LIMIT_PER_HOUR` | `30` | per-IP limit for live AI extraction (protects API credits on the public demo) |

## API
| Method & path | Purpose |
|---|---|
| `GET /` | the page |
| `GET /healthz` | `{"ok": true, "dry_run": true/false}` |
| `GET /api/samples` | list of samples: id, title, vendor country, amount, has_json |
| `GET /api/samples/{id}` | the pre-extracted JSON (no AI call) + PDF URL |
| `GET /api/samples/{id}/pdf` | the sample PDF |
| `POST /api/samples/{id}/extract` | **live** AI extraction of the sample PDF |
| `POST /api/extract` | upload a PDF (multipart) → live AI extraction → invoice JSON (+ `check_warnings`) |
| `POST /api/decide` | body: `{invoice, settings}` → decision JSON from `apply_rules` + `journal_rows` |
| `POST /api/approve` | body: `{invoice, settings, decision, approver, overrides}` → payout (or dry run), writes audit log, returns `{payout_batch_id, batch_status, dry_run}` |
| `GET /api/payout/{batch_id}` | payout status (dry-run IDs start with `DRY-` and return `SUCCESS`) |
| `POST /api/journal.csv` | body: decision → CSV download (UTF-8 with BOM, Japanese headers as in `app.py`) |

`settings` = `{payment_date, pay_currency, ttm, ttb, pay_rate|null, treaty_form}`.

Validation: PDF only, size limit, reject empty files; return clear JSON errors (`{"error": "..."}`), never a stack trace.

## Page design (single page, English)
Top: a 5-step progress bar — **1 Read (AI) · 2 Review · 3 Decide · 4 Approve · 5 Pay** — the current step highlighted.

Left column (about 45%): the invoice PDF in an `<iframe>`/`<embed>`. Below it, "What the AI read" — a compact list of
the extracted header fields and, for each line item, the AI's quoted reason (`category_reason`).
(Stretch goal only if time allows: highlight the quoted words in the PDF with pdf.js text search.)

Right column:
1. **Amount to pay** card: `JPY 83,874` style, with total, withholding, and the FX rate used. Show the withholding
   basis in words: "Domestic 20.42%" / "Japan-US treaty 0% (form on file)" / "No withholding", taken from
   `withholding_basis` and `withholding_rate` on each line.
2. **Line items** (AG Grid Community): columns description, amount (FX), amount (JPY), category (editable
   dropdown with the 7 categories, cell tinted #F2A245 at 15% opacity), consumption tax, withholding (JPY),
   account, AI reason (wrapped). Editing a category re-calls `/api/decide` and refreshes everything.
   Changed cells get a small "edited by you" marker; overrides are sent with the approval.
3. **Settings** (collapsible): payment date, pay in (JPY / invoice currency), TTM, TTB, treaty form filed
   (checkbox), approver name. Default rates 150.00 / 149.00, with the note "Demo values — use your main bank's
   published rates".
4. **Check before approving**: the `review_flags` list, in an amber box.
5. **Journal entry**: table with the Japanese headers; "Download CSV" button.
6. **Approve and pay**: checkbox "I have checked every item above" + approver name required → button
   "Approve and pay with PayPal". In dry-run mode the button says "Approve (simulated payout)" and a grey banner
   explains that `PAYOUT_DRY_RUN=true`. After approval show the batch ID, status, and a "Refresh status" button.

Sample picker at the top: buttons for each sample ("Load sample" — uses the pre-extracted JSON instantly) and a
secondary "Run live AI extraction" button that calls Claude on the same PDF. Also the upload control.

Header line under the title: "AI reads · rules decide · humans approve". Footer: one-line disclaimer that tax
rules are a demonstration, not advice, and that amounts are scaled for the sandbox balance limit.

## Audit log
Append one line to `audit_log.jsonl` per approval:
```json
{"approved_at": "...", "approver": "...", "invoice_number": "...", "vendor_country": "US",
 "ai_categories": [...], "overrides": [{"description": "...", "from": "...", "to": "..."}],
 "settings": {...}, "decision": {...}, "dry_run": true, "payout_batch_id": "DRY-20261105-abc123"}
```
Real payouts use the PayPal batch ID. `tools/auto_refill.py` reads this file and skips `dry_run: true`.

## Acceptance criteria (test these)
With TTM 150.00, TTB 149.00, pay in JPY, payment date 2026-11-15:

| Sample | Treaty form | Total JPY | Withholding JPY | Pay JPY |
|---|---|---|---|---|
| A `invoice_A_service_only` | no / yes | 67,500 | 0 | 67,500 |
| B `invoice_B_logo_copyright` | no | 90,000 | 6,126 | 83,874 |
| B `invoice_B_logo_copyright` | yes | 90,000 | 0 | 90,000 |

- Changing B's second line from `copyright_assignment` to `service_performed_abroad` makes withholding 0.
- Payment date 2026-12-15 → withholding due date 2027-01-12 (shown in the flags).
- With `PAYOUT_DRY_RUN=true`, approving writes an audit line with a `DRY-` batch ID and never calls PayPal.
- `/api/extract` with a non-PDF or an oversized file returns a 4xx JSON error.
- The page works at 1280px width without horizontal scrolling; the PDF and the grid are both visible.

## Milestones
1. **M1 — backbone**: project layout, `main.py` with `/healthz`, `/api/samples*`, `/api/decide`, `/api/journal.csv`;
   `services/decision.py`; `tests/test_decision.py` passing with the table above. No UI yet.
2. **M2 — page**: `index.html` + `app.js` with sample picker, PDF panel, AG Grid, settings, flags, journal.
   Category edits re-run the decision. Everything works from the pre-extracted JSON (no API keys needed).
3. **M3 — AI and payout**: `/api/extract`, live sample extraction, rate limit, upload validation;
   `/api/approve` with DRY_RUN and real mode, audit log, `/api/payout/{id}`; `tests/test_api.py` with mocks.
4. **M4 — ship**: `render.yaml` (Python web service, `uvicorn main:app --host 0.0.0.0 --port $PORT`),
   `requirements.txt` updated (fastapi, uvicorn, jinja2, python-multipart, pytest, httpx), README "Run locally"
   and "Deploy on Render" sections, remove dead code. Final `pytest -q` green.

## Out of scope
Authentication, database, multi-user, currencies other than JPY/USD/EUR/GBP, real PayPal (live) mode,
editing the tax rules.
