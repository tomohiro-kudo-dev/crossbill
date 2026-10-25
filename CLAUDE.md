# Crossbill — project rules for Claude Code

Cross-border AP agent for the PayPal AI Hackathon 2026 (Devpost). Deadline: 2026-11-12 12:00 PST.
Solo project by Tomohiro Kudo. Keep every change small, working and committed.

## What this product is
Foreign vendor invoice (PDF) → Claude extracts facts → human reviews → Japan rule pack decides tax
(fixed rules, no AI) → human approves → PayPal Payouts (sandbox) → audit log + journal CSV.

Principle: **AI reads, rules decide, humans approve.** Never let the AI decide tax treatment.

## Do not change without asking
- `japan_rules.py` — the tax logic and its numbers are verified. Only add fields/flags; never change rates,
  rounding (round down to whole yen) or the treaty tables.
- `extract_invoice.py` — the tool schema and prompt. Claude must only describe facts.
- `paypal_client.py` — payout request shape. JPY amounts are integers.
- Sample invoices in `samples/` and their expected results (see docs/rebuild_brief.md).

## Conventions
- Python 3.11+, FastAPI + Jinja2 templates, vanilla JS, Tailwind CSS and AG Grid **Community** from CDN.
  No frontend build step. No database (JSON files only).
- UI language: English. Journal CSV headers and account names: Japanese (freee / Money Forward import).
- Colours: AI = #20B1CE, human action = #F2A245, PayPal = #404040, everything else black/white/grey.
  Fonts: system sans-serif. No emoji in the UI.
- Money: use `decimal.Decimal`; JPY has no decimals; display with thousands separators.
- Secrets only via environment variables (`.env` locally, never committed). Never print or log keys.
- `PAYOUT_DRY_RUN` defaults to **true**. A real payout happens only when it is explicitly `false`.
- Audit log is `audit_log.jsonl`, one JSON object per line, must contain `payout_batch_id` and `dry_run`
  (tools/auto_refill.py depends on this).
- Tests: `pytest`. Never call the Claude or PayPal APIs in tests — mock them.
- Commit after each milestone with a clear message. Run `git status` before committing and make sure `.env`
  and `audit_log.jsonl` are not staged.

## Commands
- Run locally: `uvicorn main:app --reload`
- Tests: `pytest -q`
- Reference UI (old): `streamlit run app.py` — keep it working but do not extend it.
