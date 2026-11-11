# Finance Automation Hub

A small month-end close hub that launches AI-powered accounting workflows, checks
the AI's arithmetic independently, puts the result in front of a reviewer, and only
then posts it. **All data is fictional.**

The one workflow implemented is **derivatives revaluation**: a valuation report and a
trial balance go in, journal proposals come out, every amount is recomputed without
reference to the AI, a second person approves, the batch is posted to a *simulated*
D365 ledger, and the central month-end checklist is updated.

## What is real and what is simulated

| Part | Status | Detail |
| --- | --- | --- |
| `ClaudeProvider` | **Real AI** | Official `anthropic` SDK, Messages API, structured JSON output. Needs `ANTHROPIC_API_KEY` on the server. |
| `DemoProvider` | **Simulated** | Scripted answer generated on the server. No model, no network. |
| `CoworkProvider` | **Not connected** | Placeholder. No Microsoft endpoint is called or assumed. See [docs/cowork-integration.md](docs/cowork-integration.md). |
| D365 posting | **Simulated** | Writes to a local table and returns made-up `SIM-` batch and voucher numbers. Nothing is sent to Microsoft. |
| Independent validation | **Real** | Deterministic `Decimal` recalculation in `hub/workflows/derivatives/calc.py`. |
| Sign-in | **Demo only** | Users are picked from a list, not authenticated. See *Limits* below. |

The UI carries the same labels: solid green badge for real AI, hatched amber for
simulated, dashed grey for not connected. Every run records which it was.

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# offline, DemoProvider only
uvicorn hub.main:app_factory --factory --port 8000

# with real AI execution
export ANTHROPIC_API_KEY=...          # server-side only; see .env.example
uvicorn hub.main:app_factory --factory --port 8000
```

Open <http://localhost:8000>, choose *Run a workflow*, start a run as Alex, switch
the *Acting as* selector to Jo, approve. The two extra demo scenarios produce a wrong
proposal on purpose so you can watch validation block it.

```bash
pytest          # 139 tests, no network, no API key needed
```

## How it fits together

```
browser ──► /api (FastAPI, hub/main.py)
              │
              ▼
        WorkflowEngine (hub/engine.py) ── audit log, checklist, run records (SQLite, hub/db.py)
          │        │            │
          ▼        ▼            ▼
      Provider   Workflow    LedgerPostingClient
      (Claude /  (prompt,    (SimulatedD365Client)
       Demo /    schema,
       Cowork)   validation)
```

- **Provider contract** (`hub/providers/base.py`): a provider gets instructions, a JSON
  payload and the JSON Schema its answer must satisfy, and returns a JSON object. Nothing
  else in the system knows which provider answered.
- **Engine**: `submit` records the run and returns at once; the work happens on a
  background task and the client polls by run ID.

  ```
  queued → running → validating → awaiting_approval → approved → posting → posted
              │            │               │                         │
            failed   validation_failed   rejected              posting_failed (retryable)
  ```

  Every transition is an atomic compare-and-swap in SQLite and is written to the audit
  log. On restart, queued runs are re-queued, runs interrupted mid-AI-call are failed
  rather than silently repeated, and approved runs resume posting (which is idempotent).
- **API keys stay on the server.** The SDK reads the key from the environment. It is
  never stored, logged, put in an error message or returned by any endpoint; a test
  sets a key and scans every endpoint, every static file and the database for it.

## Controls

**Independent validation.** `calc.py` recomputes, from the source report only, the net
debit or credit each instrument must produce on each account, including the IFRS 9
lower-of test for cash flow hedges. `validation.py` compares the proposal with that:

| Checks | What they catch |
| --- | --- |
| `IN-01`…`IN-05` | Trial balance out of balance; ledger opening balances that disagree with the report |
| `JE-01`…`JE-05` | Duplicate or missing journals, malformed amounts, unbalanced journals, off-policy accounts |
| `CALC-01` | Any instrument whose proposed amounts differ from the recalculation, to the cent |
| `TIE-01`…`TIE-05` | Pro-forma ledger balances that do not land on the valuation report |
| `REV-01` | Non-blocking: items the AI flagged for the reviewer |

A run with any failed blocking check ends in `validation_failed`. It cannot be approved
and there is no override. Entity, period, currency and posting date are stamped from the
source report, never taken from the provider.

**Duplicate prevention**, enforced by database indexes rather than application code:
an identical submission is refused while an earlier one is live; only one run per
workflow, entity and period can ever be approved; the ledger is idempotent per run and
refuses a second batch for the same period.

**Approval.** Approver role required; the person who started a run cannot approve it;
approval is bound to the hash of the proposal that was reviewed and the stored proposal
is re-hashed before posting; rejection needs a reason; refused attempts are audited.

**Audit log.** Append-only (triggers reject `UPDATE`/`DELETE`) and hash-chained;
`GET /api/audit` reports whether the chain is intact.

## Layout

```
hub/
  main.py                    API + static frontend
  engine.py                  async workflow engine
  db.py                      SQLite: runs, audit log, checklist, simulated ledger
  identity.py                demo users and roles (not authentication)
  providers/                 base contract, claude, demo, cowork
  workflows/derivatives/     schema, prompt, calc (the control), validation, demo script
  integrations/d365.py       simulated ledger posting
  static/                    frontend (no build step)
fixtures/                    fictional valuation report and trial balance
tests/                       engine, accounting, duplicates, approvals, Claude provider, API
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables `ClaudeProvider`. |
| `HUB_CLAUDE_MODEL` | `claude-opus-5-5` | Model used for real AI execution. |
| `HUB_CLAUDE_EFFORT` | `high` | Reasoning effort. |
| `HUB_DB_PATH` | `data/hub.sqlite3` | SQLite file. |
| `HUB_PROVIDER_TIMEOUT_SECONDS` | `300` | A provider that takes longer fails the run. |

`ClaudeProvider` opts into Anthropic's server-side refusal fallback
(`fallbacks="default"`), so a request declined by a safety classifier is retried on
Anthropic's recommended model inside the same call. Delete the two lines marked in
`hub/providers/claude.py` to opt out.

## Limits

- **No authentication.** The `X-Hub-User` header is believed. Replace
  `hub/identity.py` with verified sign-in before any real data is involved.
- **The Claude path has been tested against a mocked HTTP transport, not a live key**,
  unless you have run it yourself with `ANTHROPIC_API_KEY` set.
- The accounting policy is simplified: no tax, no CVA/DVA, no settlements, no fair value
  hedges, no reclassification out of the hedge reserve.
- One process, one SQLite file. Fine for a demo; not a production topology.
- For this workflow the validator can already compute the correct journals. The AI's
  contribution is the draft, the rationale and the exceptions it raises; the control is
  the deterministic recalculation.
