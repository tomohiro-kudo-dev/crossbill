# Crossbill — Cross-border AP Agent

An AI agent that turns foreign vendor invoices into Japan-compliant payments and journal entries,
with a human approving every decision, and pays through PayPal Payouts.

**AI reads · rules decide · humans approve.** The AI only describes what is on the invoice; fixed,
auditable rules decide the tax treatment; nothing is paid until a person approves it.

Built for the PayPal AI Hackathon 2026.

![Flow](docs/crossbill_flow.png)

## How it works
1. **AI reads** (Claude): extracts vendor, amounts and line items, and classifies each line with a quoted reason.
2. **Human reviews** (AG Grid): checks the AI's classification and fixes it if wrong.
3. **Japan rule pack** (fixed rules, no AI): FX conversion, consumption tax, non-resident withholding
   (20.42% or treaty rate), account and journal entry, tax due date.
4. **Human approves**: reviews flags, amounts and the journal entry.
5. **PayPal Payouts** (sandbox): pays the vendor in JPY. `PAYOUT_DRY_RUN=true` simulates the payout.
6. **Records**: audit log and a journal CSV for freee / Money Forward.

All of this happens on one page: the invoice PDF on the left, the review grid, tax decision, journal
entry and approval on the right.

## Run locally
Requires Python 3.11 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env  — then fill in your keys
uvicorn main:app --reload
```

Open http://localhost:8000 and click **Load sample B**. Samples use pre-extracted JSON, so the page
works without any API keys. **Run live AI extraction** sends the same PDF to Claude (needs
`ANTHROPIC_API_KEY`). You can also upload your own PDF invoice.

Payouts are simulated until you set `PAYOUT_DRY_RUN=false` **and** configure the PayPal sandbox
variables. Each approval appends one line to `audit_log.jsonl`.

Tests (Claude and PayPal are mocked, no keys needed):
```bash
pytest -q
```

The original Streamlit UI is kept as a reference: `streamlit run app.py`.

## Deploy on Render
1. Push this repository to GitHub.
2. In Render, choose **New → Blueprint** and select the repository. Render reads `render.yaml`
   (Python web service, `uvicorn main:app --host 0.0.0.0 --port $PORT`, health check `/healthz`).
3. Render asks for the secret values: `ANTHROPIC_API_KEY`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
   `PAYPAL_RECEIVER_EMAIL`. Leave any of them empty to disable that feature.
4. After the deploy, open `https://<your-service>.onrender.com/healthz`. It should return
   `{"ok": true, "dry_run": true, ...}`. If `receiver_email_ok` is `false`, fix `PAYPAL_RECEIVER_EMAIL`.

The public demo keeps `PAYOUT_DRY_RUN=true`. Live AI extraction is limited per visitor
(`EXTRACT_RATE_LIMIT_PER_HOUR`) to protect API credits. On Render's free plan the disk is temporary,
so `audit_log.jsonl` is reset when the service restarts or redeploys.

## Configuration
| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for live extraction |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | model for extraction |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | — | sandbox app of the paying (JP) business account |
| `PAYPAL_RECEIVER_EMAIL` | — | sandbox test account that receives demo payouts (checked at startup) |
| `PAYOUT_DRY_RUN` | `true` | only the exact value `false` sends a real sandbox payout |
| `MAX_UPLOAD_MB` | `5` | upload size limit |
| `EXTRACT_RATE_LIMIT_PER_HOUR` | `30` | live AI extractions per visitor IP per hour |
| `AUDIT_LOG` | `audit_log.jsonl` | audit log path (also read by `tools/auto_refill.py`) |

Secrets go in `.env` locally (never committed) or in Render's environment settings.

## Demo amounts
The sample invoices are fictional, and their amounts are scaled down to fit the PayPal sandbox
account's balance limit (for example, USD 600 → JPY 83,874 after withholding). Real invoices
would usually be larger; the rules work the same way at any amount.

## Repository layout
| Path | Purpose |
|---|---|
| `main.py` | FastAPI app: page and JSON API |
| `services/` | Thin wrappers: extraction (upload checks, rate limit), decision (rules + journal), payout (dry run, audit log), samples |
| `templates/`, `static/` | The single page (Jinja2, vanilla JS, Tailwind and AG Grid Community from CDN) |
| `extract_invoice.py` | Invoice reading with Claude (facts only, no tax decisions) |
| `japan_rules.py` | Japan rule pack: FX, consumption tax, withholding, treaty rates, due date |
| `paypal_client.py` | PayPal Payouts (sandbox) |
| `app.py` | Streamlit UI (reference implementation) |
| `samples/` | Fictional invoices and their pre-extracted JSON |
| `tools/` | Developer utilities (sample generator, sandbox balance refill) |
| `tests/` | `pytest` suite |
| `render.yaml` | Render blueprint |

## Disclaimer
The tax rules in this repository are a demonstration of the approach. **They are not tax advice.**
Confirm every treatment with a qualified tax professional before using it for real payments.
Demo FX rates (TTM 150.00 / TTB 149.00) are placeholders; use your main bank's published rates.

## License
MIT
