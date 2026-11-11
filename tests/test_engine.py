"""Workflow engine: async execution, persistence, statuses, timestamps, errors, audit."""
from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

from hub.engine import EngineError
from hub.providers.base import MODE_NOT_CONNECTED, ProviderNotConfigured, ProviderRateLimited, ProviderTask
from hub.providers.cowork import CoworkProvider

from .conftest import ALEX, JO, SAM, WORKFLOW, Harness, ScriptedProvider, clean_output, load_input


async def test_happy_path_runs_to_posted(h: Harness):
    run = await h.submit_and_wait()
    assert run["status"] == "awaiting_approval" and run["validation_passed"] is True
    assert h.checklist_item()["status"] == "awaiting_review"

    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"], comment="Agreed to valuation statement")
    await h.engine.wait_idle()
    done = h.run(run["id"])
    assert done["status"] == "posted"
    assert done["approved_by"] == "jo.reviewer" and done["approval_comment"] == "Agreed to valuation statement"
    assert done["posting"]["simulated"] is True and done["posting"]["batch_number"].startswith("SIM-")
    assert len(done["posting"]["vouchers"]) == 5

    item = h.checklist_item()
    assert item["status"] == "complete" and item["run_id"] == run["id"] and "simulated" in item["note"].lower()


async def test_run_record_has_id_timestamps_and_provenance(h: Harness):
    run = await h.submit_and_wait()
    assert re.fullmatch(r"RUN-\d{8}-[0-9A-F]{8}", run["id"])
    assert run["created_at"] <= run["started_at"] <= run["updated_at"]
    assert run["completed_at"] is None  # still waiting for a human
    assert run["requested_by"] == "alex.preparer" and run["execution_mode"] == "real_ai"
    assert run["provider_meta"] == {"execution_mode": "real_ai", "model": "scripted-1", "notes": [],
                                    "provider_request_id": "req_scripted",
                                    "usage": {"input_tokens": 10, "output_tokens": 20}}
    assert len(run["input_hash"]) == 64 and len(run["proposal_hash"]) == 64


async def test_submit_returns_before_the_provider_answers():
    h = Harness(scripted=ScriptedProvider(output=clean_output(), hold=True))
    run = await h.submit()
    assert run["status"] == "queued"  # returned immediately; nothing has run yet
    await asyncio.sleep(0.05)
    assert h.run(run["id"])["status"] == "running"
    assert h.checklist_item()["status"] == "in_progress"
    h.scripted.gate.set()
    await h.engine.wait_idle()
    assert h.run(run["id"])["status"] == "awaiting_approval"


async def test_audit_trail_records_the_whole_lifecycle_in_order(h: Harness):
    run = await h.submit_and_wait()
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    events = [e for e in h.events(run["id"]) if e != "checklist.updated"]
    assert events == ["run.created", "run.started", "provider.requested", "provider.responded", "validation.passed",
                      "approval.granted", "posting.requested", "posting.completed"]
    entries = {e["event"]: e for e in h.db.audit_for_run(run["id"])}
    assert entries["run.created"]["actor"] == "alex.preparer"
    assert entries["approval.granted"]["actor"] == "jo.reviewer"
    assert entries["provider.requested"]["detail"]["execution_mode"] == "real_ai"
    assert entries["posting.completed"]["detail"]["simulated"] is True


async def test_only_derivative_accounts_are_sent_to_the_provider(h: Harness):
    await h.submit_and_wait()
    task: ProviderTask = h.scripted.calls[0]
    assert {b["account"] for b in task.payload["ledger_balances"]} == {"1500", "2500", "3500", "7100", "7110"}
    assert "Retained earnings" not in str(task.payload)
    assert task.output_schema["required"] == ["summary", "journals", "exceptions"]


async def test_provider_error_fails_the_run_with_code_and_message():
    h = Harness(scripted=ScriptedProvider(error=ProviderRateLimited("Rate limit reached.", provider_request_id="req_9")))
    run = await h.submit_and_wait()
    assert (run["status"], run["error_code"], run["error_message"]) == ("failed", "PROVIDER_RATE_LIMITED", "Rate limit reached.")
    assert run["completed_at"] is not None and run["proposal"] is None
    failure = next(e for e in h.db.audit_for_run(run["id"]) if e["event"] == "run.failed")
    assert failure["detail"]["provider_request_id"] == "req_9"
    assert h.checklist_item()["status"] == "blocked"
    assert h.ledger.calls == []


async def test_provider_timeout_fails_the_run():
    h = Harness(scripted=ScriptedProvider(output=clean_output(), hold=True), timeout=0.05)
    run = await h.submit_and_wait()
    assert (run["status"], run["error_code"]) == ("failed", "PROVIDER_TIMEOUT")


async def test_unexpected_exception_is_recorded_without_leaking_details():
    h = Harness(scripted=ScriptedProvider(error=RuntimeError("secret connection string")))
    run = await h.submit_and_wait()
    assert (run["status"], run["error_code"]) == ("failed", "INTERNAL_ERROR")
    assert "secret" not in run["error_message"]


@pytest.mark.parametrize("output", [{"journals": "nope"}, {"summary": "x", "journals": [], "exceptions": [], "extra": 1}, {}])
async def test_answer_that_does_not_match_the_schema_fails_the_run(output):
    h = Harness(scripted=ScriptedProvider(output=output))
    run = await h.submit_and_wait()
    assert (run["status"], run["error_code"]) == ("failed", "PROVIDER_OUTPUT_INVALID")


async def test_failed_validation_is_terminal_and_keeps_the_evidence(h: Harness):
    run = await h.submit_and_wait(provider="demo", options={"demo_scenario": "calc_error"})
    assert run["status"] == "validation_failed" and run["validation_passed"] is False
    assert run["execution_mode"] == "simulated"
    assert run["proposal"]["journals"] and run["validation"]["summary"]["failed"] >= 1
    entry = next(e for e in h.db.audit_for_run(run["id"]) if e["event"] == "validation.failed")
    assert "CALC-01 IRS-002" in entry["detail"]["failed_checks"]
    assert h.checklist_item()["status"] == "blocked"


async def test_invalid_input_creates_no_run(h: Harness):
    raw = load_input()
    raw["report"]["instruments"][0]["current_fair_value"] = "sixty thousand"
    with pytest.raises(EngineError) as exc:
        await h.submit(raw_input=raw)
    assert (exc.value.code, exc.value.status) == ("INVALID_INPUT", 422) and exc.value.extra["details"]
    assert h.db.list_runs() == []


async def test_unknown_workflow_and_provider_are_refused(h: Harness):
    with pytest.raises(EngineError) as exc:
        await h.engine.submit(workflow="payroll", provider="demo", raw_input=load_input(), user=ALEX)
    assert exc.value.code == "UNKNOWN_WORKFLOW"
    with pytest.raises(EngineError) as exc:
        await h.submit(provider="gpt")
    assert exc.value.code == "UNKNOWN_PROVIDER"


async def test_only_preparers_can_start_runs(h: Harness):
    with pytest.raises(EngineError) as exc:
        await h.submit(user=JO)
    assert (exc.value.code, exc.value.status) == ("FORBIDDEN", 403)


# ------------------------------------------------------------------ Cowork placeholder


async def test_cowork_placeholder_cannot_be_launched(h: Harness):
    info = CoworkProvider().info()
    assert info.available is False and info.execution_mode == MODE_NOT_CONNECTED
    with pytest.raises(EngineError) as exc:
        await h.submit(provider="cowork")
    assert (exc.value.code, exc.value.status) == ("PROVIDER_NOT_AVAILABLE", 409)
    assert h.db.list_runs() == []
    with pytest.raises(ProviderNotConfigured):
        await CoworkProvider().run(ProviderTask("RUN-X", WORKFLOW, "", {}, {}))


async def test_cowork_works_end_to_end_once_a_transport_is_supplied():
    """The seam the future integration plugs into: no engine or API change needed."""

    class FakeTransport:
        label = "fake Copilot Studio agent"

        async def invoke(self, task: ProviderTask) -> dict:
            return clean_output()

    h = Harness()
    h.engine.providers._providers["cowork"] = CoworkProvider(FakeTransport())
    run = await h.submit_and_wait(provider="cowork")
    assert run["status"] == "awaiting_approval" and run["provider"] == "cowork"


# ------------------------------------------------------------------ persistence and recovery


async def test_records_survive_a_restart(tmp_path):
    path = str(tmp_path / "hub.sqlite3")
    first = Harness(db_path=path)
    run = await first.submit_and_wait()
    first.db.close()

    second = Harness(db_path=path)
    restored = second.run(run["id"])
    assert restored["status"] == "awaiting_approval" and restored["proposal"] == run["proposal"]
    assert second.events(run["id"])[0] == "run.created"
    await second.engine.approve(run["id"], JO, proposal_hash=restored["proposal_hash"])
    await second.engine.wait_idle()
    assert second.run(run["id"])["status"] == "posted"


async def test_recovery_after_a_crash(tmp_path):
    path = str(tmp_path / "hub.sqlite3")
    first = Harness(db_path=path, scripted=ScriptedProvider(output=clean_output(), hold=True))
    interrupted = await first.submit()
    await asyncio.sleep(0.05)  # now 'running', blocked inside the provider
    for task in list(first.engine._tasks):
        task.cancel()  # the process dies
    await asyncio.gather(*first.engine._tasks, return_exceptions=True)

    queued = await first.submit(provider="demo")  # recorded, never started
    for task in list(first.engine._tasks):
        task.cancel()
    await asyncio.gather(*first.engine._tasks, return_exceptions=True)
    assert first.run(interrupted["id"])["status"] == "running" and first.run(queued["id"])["status"] == "queued"
    first.db.close()

    second = Harness(db_path=path)
    counts = await second.engine.recover()
    await second.engine.wait_idle()
    assert counts == {"requeued": 1, "failed": 1, "posting_resumed": 0}
    dead = second.run(interrupted["id"])
    assert (dead["status"], dead["error_code"]) == ("failed", "INTERRUPTED")
    assert second.scripted.calls == []  # the interrupted AI call is not silently repeated
    assert second.run(queued["id"])["status"] == "awaiting_approval"


async def test_recovery_resumes_posting_without_posting_twice(tmp_path):
    path = str(tmp_path / "hub.sqlite3")
    first = Harness(db_path=path)
    run = await first.submit_and_wait()
    await first.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await first.engine.wait_idle()
    # Pretend the process died after the ledger accepted the batch but before the run was marked posted.
    first.db._conn.execute("UPDATE runs SET status = 'posting', posting_json = NULL WHERE id = ?", (run["id"],))
    first.db.close()

    second = Harness(db_path=path)
    assert (await second.engine.recover())["posting_resumed"] == 1
    await second.engine.wait_idle()
    done = second.run(run["id"])
    assert done["status"] == "posted" and done["posting"]["replayed"] is True
    assert second.db.count_sim_batches() == 1


# ------------------------------------------------------------------ audit log integrity


async def test_audit_log_is_append_only_and_hash_chained(h: Harness):
    run = await h.submit_and_wait()
    assert h.db.verify_audit_chain()["intact"] is True
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        h.db._conn.execute("UPDATE audit_log SET actor = 'nobody' WHERE run_id = ?", (run["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        h.db._conn.execute("DELETE FROM audit_log")
    assert h.db.verify_audit_chain()["intact"] is True


async def test_tampering_behind_the_triggers_breaks_the_chain(h: Harness):
    await h.submit_and_wait()
    h.db._conn.execute("DROP TRIGGER audit_log_no_update")
    h.db._conn.execute("UPDATE audit_log SET actor = 'someone.else' WHERE seq = 2")
    assert h.db.verify_audit_chain() == {"intact": False, "entries": h.db.verify_audit_chain()["entries"], "first_broken_seq": 2}
