"""Asynchronous workflow engine.

Lifecycle of a run::

    queued -> running -> validating -> awaiting_approval -> approved -> posting -> posted
                 |            |               |                            |
               failed   validation_failed   rejected                 posting_failed (retryable)

Every transition is an atomic compare-and-swap in the database and is written
to the append-only audit log. ``submit`` returns as soon as the run is recorded;
the work happens on a background task and callers poll the run by id.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from .db import COMMITTED_STATUSES, Database, canonical_json, sha256_hex
from .identity import ROLE_APPROVER, ROLE_PREPARER, User
from .integrations.d365 import LedgerPostingClient
from .providers.base import ProviderError, ProviderRegistry, ProviderTask
from .workflows.base import InvalidInput, Workflow

log = logging.getLogger("hub.engine")
SYSTEM_ACTOR = "system"

MONTH_END_TASKS: list[dict[str, Any]] = [
    {"task_key": "bank_reconciliations", "title": "Bank reconciliations", "kind": "manual"},
    {"task_key": "ap_accruals", "title": "Accounts payable accruals", "kind": "manual"},
    {"task_key": "payroll_journals", "title": "Payroll journals", "kind": "manual"},
    {"task_key": "fixed_asset_depreciation", "title": "Fixed asset depreciation", "kind": "manual"},
    {"task_key": "derivatives_revaluation", "title": "Derivatives revaluation", "kind": "automated",
     "workflow": "derivatives_revaluation"},
    {"task_key": "intercompany_reconciliation", "title": "Intercompany reconciliation", "kind": "manual"},
    {"task_key": "trial_balance_review", "title": "Trial balance review", "kind": "manual"},
    {"task_key": "close_sign_off", "title": "Period close sign-off", "kind": "manual"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EngineError(Exception):
    """A refusal the caller can act on. ``status`` is the HTTP status to return."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


class WorkflowEngine:
    def __init__(self, *, db: Database, providers: ProviderRegistry, workflows: list[Workflow],
                 ledger: LedgerPostingClient, clock: Callable[[], str] = utc_now,
                 provider_timeout_seconds: float = 300.0) -> None:
        self.db = db
        self.providers = providers
        self.workflows = {w.name: w for w in workflows}
        self.ledger = ledger
        self._clock = clock
        self._provider_timeout = provider_timeout_seconds
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ helpers

    def _audit(self, run_id: str | None, actor: str, event: str, **detail: Any) -> None:
        self.db.append_audit(run_id=run_id, ts=self._clock(), actor=actor, event=event, detail=detail)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for all background work to finish. Used by tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run is None:
            raise EngineError("RUN_NOT_FOUND", f"No run with id {run_id}.", 404)
        return run

    # ------------------------------------------------------------------ checklist

    def ensure_checklist(self, entity: str, period: str) -> None:
        self.db.seed_checklist(entity, period, MONTH_END_TASKS, self._clock())

    def _refresh_checklist(self, workflow: Workflow, entity: str, period: str) -> None:
        """Recompute the checklist line from the runs on record, so the central
        checklist can never drift from what actually happened."""
        self.ensure_checklist(entity, period)
        runs = self.db.runs_for_scope(workflow.name, entity, period)  # newest first

        def newest(*statuses: str) -> dict[str, Any] | None:
            return next((r for r in runs if r["status"] in statuses), None)

        if run := newest("posted"):
            batch = (run.get("posting") or {}).get("batch_number", "")
            status, note = "complete", f"Posted as {batch} (simulated D365 posting). Approved by {run['approved_by']}."
        elif run := newest("posting_failed"):
            status, note = "blocked", "Approved, but posting failed. Retry posting."
        elif run := newest("approved", "posting"):
            status, note = "in_progress", f"Approved by {run['approved_by']}; posting."
        elif run := newest("awaiting_approval"):
            status, note = "awaiting_review", "Journals proposed and validated. Waiting for an approver."
        elif run := newest("queued", "running", "validating"):
            status, note = "in_progress", "Workflow running."
        elif runs and runs[0]["status"] in ("failed", "validation_failed"):
            run = runs[0]
            status = "blocked"
            note = ("Latest run failed independent validation." if run["status"] == "validation_failed"
                    else f"Latest run failed: {run['error_message']}")
        elif runs:
            run, status, note = runs[0], "not_started", "Latest run was rejected. Start a new run."
        else:
            run, status, note = None, "not_started", None

        run_id = run["id"] if run else None
        if self.db.set_checklist_item(entity, period, workflow.checklist_task_key,
                                      status=status, run_id=run_id, note=note, now=self._clock()):
            self._audit(run_id, SYSTEM_ACTOR, "checklist.updated", entity=entity, period=period,
                        task=workflow.checklist_task_key, status=status)

    # ------------------------------------------------------------------ submit

    async def submit(self, *, workflow: str, provider: str, raw_input: dict[str, Any], user: User,
                     options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        if not user.has(ROLE_PREPARER):
            raise EngineError("FORBIDDEN", "Only a preparer can start a workflow run.", 403)
        wf = self.workflows.get(workflow)
        if wf is None:
            raise EngineError("UNKNOWN_WORKFLOW", f"Unknown workflow '{workflow}'.", 404)
        prov = self.providers.get(provider)
        if prov is None:
            raise EngineError("UNKNOWN_PROVIDER", f"Unknown provider '{provider}'.", 400)
        info = prov.info()
        if not info.available:
            raise EngineError("PROVIDER_NOT_AVAILABLE",
                              f"{info.label} is not available: {info.unavailable_reason}", 409)
        try:
            data = wf.parse_input(raw_input)
        except InvalidInput as exc:
            raise EngineError("INVALID_INPUT", exc.message, 422, details=exc.details) from exc

        entity, period = wf.scope(data)
        stored_input = wf.dump_input(data)
        input_hash = sha256_hex(stored_input)
        dedupe_key = sha256_hex([workflow, provider, entity, period, input_hash, options])
        now = self._clock()
        run_id = f"RUN-{now[:10].replace('-', '')}-{secrets.token_hex(4).upper()}"
        try:
            self.db.insert_run({
                "id": run_id, "workflow": workflow, "provider": provider,
                "execution_mode": info.execution_mode, "status": "queued",
                "entity": entity, "period": period, "dedupe_key": dedupe_key, "input_hash": input_hash,
                "requested_by": user.user_id, "options_json": _json(options), "input_json": _json(stored_input),
                "created_at": now, "updated_at": now,
            })
        except sqlite3.IntegrityError:
            existing = self.db.find_live_duplicate(dedupe_key)
            existing_id = existing["id"] if existing else None
            self._audit(existing_id, user.user_id, "run.duplicate_blocked",
                        workflow=workflow, provider=provider, entity=entity, period=period)
            raise EngineError(
                "DUPLICATE_RUN",
                f"An identical run already exists ({existing_id}, status {existing['status'] if existing else 'unknown'}). "
                "Open that run instead of starting another.", 409, existing_run_id=existing_id) from None

        self._audit(run_id, user.user_id, "run.created", workflow=workflow, provider=provider,
                    execution_mode=info.execution_mode, entity=entity, period=period, input_sha256=input_hash)
        self._refresh_checklist(wf, entity, period)
        self._spawn(self._execute(run_id))
        return self._require_run(run_id)

    # ------------------------------------------------------------------ execute

    async def _execute(self, run_id: str) -> None:
        now = self._clock()
        if not self.db.transition(run_id, ["queued"], "running", now, started_at=now):
            return
        run = self._require_run(run_id)
        wf = self.workflows[run["workflow"]]
        self._audit(run_id, SYSTEM_ACTOR, "run.started")
        try:
            data = wf.load_input(run["input"])
            provider = self.providers.get(run["provider"])
            if provider is None:
                raise ProviderError(f"Provider '{run['provider']}' is no longer registered.")
            instructions, payload, schema = wf.provider_task(data)
            info = provider.info()
            self._audit(run_id, SYSTEM_ACTOR, "provider.requested", provider=run["provider"],
                        execution_mode=info.execution_mode, model=info.model,
                        instructions_sha256=sha256_hex(instructions), payload_sha256=sha256_hex(payload))
            task = ProviderTask(run_id=run_id, workflow=wf.name, instructions=instructions,
                                payload=payload, output_schema=schema, options=run["options"] or {})
            result = await asyncio.wait_for(provider.run(task), timeout=self._provider_timeout)
            meta = {"execution_mode": result.execution_mode, "model": result.model,
                    "provider_request_id": result.provider_request_id, "usage": result.usage,
                    "notes": result.notes}
            self._audit(run_id, SYSTEM_ACTOR, "provider.responded", provider=run["provider"],
                        output_sha256=sha256_hex(result.output), **meta)

            self.db.transition(run_id, ["running"], "validating", self._clock(),
                               provider_meta_json=_json(meta), execution_mode=result.execution_mode)
            proposal, validation = wf.review(run_id, data, result.output)
            proposal_hash = sha256_hex(proposal)
            passed = bool(validation["passed"])
            now = self._clock()
            fields = {"proposal_json": _json(proposal), "proposal_hash": proposal_hash,
                      "validation_json": _json(validation), "validation_passed": int(passed)}
            if passed:
                self.db.transition(run_id, ["validating"], "awaiting_approval", now, **fields)
                self._audit(run_id, SYSTEM_ACTOR, "validation.passed", proposal_sha256=proposal_hash,
                            **validation["summary"])
            else:
                self.db.transition(run_id, ["validating"], "validation_failed", now, completed_at=now,
                                   error_code="VALIDATION_FAILED",
                                   error_message="The proposal failed independent validation and cannot be approved.",
                                   **fields)
                failed = [f"{c['check_id']}{' ' + c['scope'] if c['scope'] else ''}"
                          for c in validation["checks"] if c["status"] == "fail"]
                self._audit(run_id, SYSTEM_ACTOR, "validation.failed", proposal_sha256=proposal_hash,
                            failed_checks=failed, **validation["summary"])
        except ProviderError as exc:
            self._fail(run_id, exc.code, exc.message, provider_request_id=exc.provider_request_id)
        except asyncio.TimeoutError:
            self._fail(run_id, "PROVIDER_TIMEOUT",
                       f"The provider did not answer within {self._provider_timeout:.0f} seconds.")
        except Exception as exc:  # noqa: BLE001 - last line of defence; details go to the server log only
            log.exception("run %s failed unexpectedly", run_id)
            self._fail(run_id, "INTERNAL_ERROR", f"Unexpected error ({type(exc).__name__}). See the server log.")
        finally:
            self._refresh_checklist(wf, run["entity"], run["period"])

    def _fail(self, run_id: str, code: str, message: str, **detail: Any) -> None:
        now = self._clock()
        if self.db.transition(run_id, ["queued", "running", "validating"], "failed", now,
                              completed_at=now, error_code=code, error_message=message):
            self._audit(run_id, SYSTEM_ACTOR, "run.failed", error_code=code, error_message=message,
                        **{k: v for k, v in detail.items() if v is not None})

    # ------------------------------------------------------------------ approval

    def _deny(self, run_id: str, user: User, code: str, message: str, status: int) -> EngineError:
        self._audit(run_id, user.user_id, "approval.denied", reason=code)
        return EngineError(code, message, status)

    async def approve(self, run_id: str, user: User, *, proposal_hash: str, comment: str = "") -> dict[str, Any]:
        run = self._require_run(run_id)
        wf = self.workflows[run["workflow"]]
        if not user.has(ROLE_APPROVER):
            raise self._deny(run_id, user, "NOT_AN_APPROVER", "You do not have the approver role.", 403)
        if run["status"] == "validation_failed":
            raise self._deny(run_id, user, "VALIDATION_FAILED",
                             "This run failed independent validation. It cannot be approved or overridden.", 409)
        if run["status"] != "awaiting_approval":
            raise self._deny(run_id, user, "INVALID_STATE",
                             f"Only a run awaiting approval can be approved (this one is {run['status']}).", 409)
        if not run["validation_passed"]:
            raise self._deny(run_id, user, "VALIDATION_FAILED",
                             "This run has not passed independent validation.", 409)
        if user.user_id == run["requested_by"]:
            raise self._deny(run_id, user, "SEGREGATION_OF_DUTIES",
                             "You started this run, so someone else has to approve it.", 403)
        if proposal_hash != run["proposal_hash"]:
            raise self._deny(run_id, user, "PROPOSAL_CHANGED",
                             "The proposal you reviewed is not the one on record. Reload and review again.", 409)

        now = self._clock()
        try:
            won = self.db.transition(run_id, ["awaiting_approval"], "approved", now, approved_by=user.user_id,
                                     approved_at=now, approval_comment=comment.strip() or None)
        except sqlite3.IntegrityError:
            other = next((r for r in self.db.runs_for_scope(run["workflow"], run["entity"], run["period"])
                          if r["status"] in COMMITTED_STATUSES), None)
            raise self._deny(
                run_id, user, "PERIOD_ALREADY_APPROVED",
                f"{wf.title} for {run['entity']} {run['period']} has already been approved"
                f"{' in ' + other['id'] if other else ''}. Reject this run instead.", 409) from None
        if not won:
            raise self._deny(run_id, user, "INVALID_STATE", "This run was just actioned by someone else.", 409)

        self._audit(run_id, user.user_id, "approval.granted", proposal_sha256=proposal_hash,
                    requested_by=run["requested_by"], comment=comment.strip() or None)
        self._refresh_checklist(wf, run["entity"], run["period"])
        self._spawn(self._post(run_id))
        return self._require_run(run_id)

    async def reject(self, run_id: str, user: User, *, reason: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        wf = self.workflows[run["workflow"]]
        if not (user.has(ROLE_APPROVER) or user.user_id == run["requested_by"]):
            raise EngineError("FORBIDDEN", "Only an approver, or the person who started the run, can reject it.", 403)
        reason = reason.strip()
        if len(reason) < 5:
            raise EngineError("REASON_REQUIRED", "Give a reason for rejecting (at least 5 characters).", 422)
        now = self._clock()
        if not self.db.transition(run_id, ["awaiting_approval"], "rejected", now, completed_at=now,
                                  rejected_by=user.user_id, rejected_at=now, rejection_reason=reason):
            raise EngineError("INVALID_STATE",
                              f"Only a run awaiting approval can be rejected (this one is {run['status']}).", 409)
        self._audit(run_id, user.user_id, "approval.rejected", reason=reason)
        self._refresh_checklist(wf, run["entity"], run["period"])
        return self._require_run(run_id)

    # ------------------------------------------------------------------ posting

    async def retry_posting(self, run_id: str, user: User) -> dict[str, Any]:
        run = self._require_run(run_id)
        if not user.has(ROLE_APPROVER):
            raise EngineError("NOT_AN_APPROVER", "You do not have the approver role.", 403)
        if run["status"] != "posting_failed":
            raise EngineError("INVALID_STATE", f"Posting can only be retried after it failed (status {run['status']}).", 409)
        self._audit(run_id, user.user_id, "posting.retry_requested")
        self._spawn(self._post(run_id))
        return run

    async def _post(self, run_id: str) -> None:
        if not self.db.transition(run_id, ["approved", "posting_failed", "posting"], "posting", self._clock(),
                                  error_code=None, error_message=None):
            return
        run = self._require_run(run_id)
        wf = self.workflows[run["workflow"]]
        try:
            # Post exactly what was approved: re-hash the stored proposal first.
            if not run["approved_by"] or sha256_hex(run["proposal"]) != run["proposal_hash"]:
                raise RuntimeError("stored proposal does not match the approved hash")
            batch = wf.posting_batch(run)
            self._audit(run_id, SYSTEM_ACTOR, "posting.requested", integration_mode=self.ledger.integration_mode,
                        system=self.ledger.system_label, journals=len(batch["journals"]))
            result = await self.ledger.post_journal_batch(batch)
            now = self._clock()
            self.db.transition(run_id, ["posting"], "posted", now, completed_at=now, posting_json=_json(result))
            self._audit(run_id, SYSTEM_ACTOR, "posting.completed", integration_mode=result.get("integration_mode"),
                        simulated=result.get("simulated"), batch_number=result.get("batch_number"),
                        vouchers=[v["voucher"] for v in result.get("vouchers", [])],
                        replayed=result.get("replayed", False))
        except Exception as exc:  # noqa: BLE001
            log.exception("posting for run %s failed", run_id)
            message = f"Posting failed: {exc}"
            self.db.transition(run_id, ["posting"], "posting_failed", self._clock(),
                               error_code="POSTING_FAILED", error_message=message)
            self._audit(run_id, SYSTEM_ACTOR, "posting.failed", error_message=message)
        finally:
            self._refresh_checklist(wf, run["entity"], run["period"])

    # ------------------------------------------------------------------ recovery

    async def recover(self) -> dict[str, int]:
        """Called at startup. Records survive a restart; in-flight work does not,
        so decide what to do with each run the last process left behind."""
        counts = {"requeued": 0, "failed": 0, "posting_resumed": 0}
        for run in self.db.runs_in_status(["queued"]):
            self._audit(run["id"], SYSTEM_ACTOR, "run.recovered", action="requeued")
            self._spawn(self._execute(run["id"]))
            counts["requeued"] += 1
        for run in self.db.runs_in_status(["running", "validating"]):
            # The provider call may or may not have happened. Do not silently repeat it.
            self._fail(run["id"], "INTERRUPTED", "The server stopped while this run was in progress. Start a new run.")
            self._refresh_checklist(self.workflows[run["workflow"]], run["entity"], run["period"])
            counts["failed"] += 1
        for run in self.db.runs_in_status(["approved", "posting"]):
            # Posting is idempotent on run id, so it is safe to resume.
            self._audit(run["id"], SYSTEM_ACTOR, "run.recovered", action="posting_resumed")
            self._spawn(self._post(run["id"]))
            counts["posting_resumed"] += 1
        return counts


def _json(value: Any) -> str:
    return canonical_json(value)
