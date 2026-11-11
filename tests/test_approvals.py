"""Approval controls: roles, segregation of duties, state, proposal binding."""
from __future__ import annotations

import asyncio

import pytest

from hub.engine import EngineError

from .conftest import ALEX, JO, SAM, Harness, ScriptedProvider, clean_output


async def denied(coroutine) -> EngineError:
    with pytest.raises(EngineError) as exc:
        await coroutine
    return exc.value


async def test_nothing_is_posted_before_approval(h: Harness):
    run = await h.submit_and_wait()
    assert run["status"] == "awaiting_approval"
    assert h.ledger.calls == [] and h.db.count_sim_batches() == 0 and run["posting"] is None


async def test_a_preparer_cannot_approve(h: Harness):
    run = await h.submit_and_wait(user=SAM)
    error = await denied(h.engine.approve(run["id"], ALEX, proposal_hash=run["proposal_hash"]))
    assert (error.code, error.status) == ("NOT_AN_APPROVER", 403)
    assert h.run(run["id"])["status"] == "awaiting_approval" and h.ledger.calls == []


async def test_requester_cannot_approve_their_own_run_even_with_the_approver_role(h: Harness):
    run = await h.submit_and_wait(user=SAM)
    error = await denied(h.engine.approve(run["id"], SAM, proposal_hash=run["proposal_hash"]))
    assert (error.code, error.status) == ("SEGREGATION_OF_DUTIES", 403)
    assert h.run(run["id"])["status"] == "awaiting_approval" and h.ledger.calls == []

    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])  # a second person can
    await h.engine.wait_idle()
    assert h.run(run["id"])["status"] == "posted"


@pytest.mark.parametrize("scenario", ["calc_error", "unbalanced"])
async def test_a_run_that_failed_validation_cannot_be_approved(h: Harness, scenario):
    run = await h.submit_and_wait(provider="demo", options={"demo_scenario": scenario})
    error = await denied(h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"]))
    assert (error.code, error.status) == ("VALIDATION_FAILED", 409)
    assert h.run(run["id"])["status"] == "validation_failed" and h.ledger.calls == []


async def test_validation_flag_is_checked_even_if_status_was_forced(h: Harness):
    """Defence in depth: status alone is not enough to approve."""
    run = await h.submit_and_wait(provider="demo", options={"demo_scenario": "calc_error"})
    h.db._conn.execute("UPDATE runs SET status = 'awaiting_approval' WHERE id = ?", (run["id"],))
    error = await denied(h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"]))
    assert error.code == "VALIDATION_FAILED" and h.ledger.calls == []


async def test_a_run_still_in_progress_cannot_be_approved():
    h = Harness(scripted=ScriptedProvider(output=clean_output(), hold=True))
    run = await h.submit()
    await asyncio.sleep(0.05)
    error = await denied(h.engine.approve(run["id"], JO, proposal_hash="x" * 64))
    assert error.code == "INVALID_STATE"
    h.scripted.gate.set()
    await h.engine.wait_idle()


async def test_approval_is_bound_to_the_proposal_that_was_reviewed(h: Harness):
    run = await h.submit_and_wait()
    error = await denied(h.engine.approve(run["id"], JO, proposal_hash="0" * 64))
    assert (error.code, error.status) == ("PROPOSAL_CHANGED", 409)
    assert h.run(run["id"])["status"] == "awaiting_approval"


async def test_a_proposal_altered_after_review_is_not_posted(h: Harness):
    run = await h.submit_and_wait()
    tampered = h.db._conn.execute("SELECT proposal_json FROM runs WHERE id = ?", (run["id"],)).fetchone()[0]
    h.db._conn.execute("UPDATE runs SET proposal_json = ? WHERE id = ?",
                       (tampered.replace("19500.00", "99500.00"), run["id"]))
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    after = h.run(run["id"])
    assert after["status"] == "posting_failed" and "approved hash" in after["error_message"]
    assert h.ledger.calls == [] and h.db.count_sim_batches() == 0


async def test_a_run_cannot_be_approved_twice(h: Harness):
    run = await h.submit_and_wait()
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    error = await denied(h.engine.approve(run["id"], SAM, proposal_hash=run["proposal_hash"]))
    assert error.code == "INVALID_STATE"
    await h.engine.wait_idle()
    assert len(h.ledger.calls) == 1 and h.db.count_sim_batches() == 1


async def test_concurrent_approvals_post_once(h: Harness):
    run = await h.submit_and_wait()
    results = await asyncio.gather(
        *(h.engine.approve(run["id"], user, proposal_hash=run["proposal_hash"]) for user in (JO, SAM, JO, SAM)),
        return_exceptions=True)
    await h.engine.wait_idle()
    assert sum(isinstance(r, dict) for r in results) == 1
    assert all(r.code == "INVALID_STATE" for r in results if isinstance(r, EngineError))
    assert len(h.ledger.calls) == 1 and h.db.count_sim_batches() == 1


async def test_rejection_needs_a_reason_and_stops_the_run(h: Harness):
    run = await h.submit_and_wait()
    error = await denied(h.engine.reject(run["id"], JO, reason="  no "))
    assert (error.code, error.status) == ("REASON_REQUIRED", 422)

    rejected = await h.engine.reject(run["id"], JO, reason="FXF-002 valuation disputed with counterparty")
    assert rejected["status"] == "rejected" and rejected["rejected_by"] == "jo.reviewer"
    assert rejected["rejection_reason"] == "FXF-002 valuation disputed with counterparty" and rejected["completed_at"]
    error = await denied(h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"]))
    assert error.code == "INVALID_STATE"
    assert h.ledger.calls == [] and h.checklist_item()["status"] == "not_started"


async def test_who_may_reject(h: Harness):
    run = await h.submit_and_wait(user=SAM)
    error = await denied(h.engine.reject(run["id"], ALEX, reason="Not mine to reject"))
    assert (error.code, error.status) == ("FORBIDDEN", 403)
    withdrawn = await h.engine.reject(run["id"], SAM, reason="Withdrawn - wrong statement uploaded")
    assert withdrawn["status"] == "rejected"  # the requester may withdraw their own run


async def test_refused_approval_attempts_are_audited(h: Harness):
    run = await h.submit_and_wait(user=SAM)
    await denied(h.engine.approve(run["id"], ALEX, proposal_hash=run["proposal_hash"]))
    await denied(h.engine.approve(run["id"], SAM, proposal_hash=run["proposal_hash"]))
    await denied(h.engine.approve(run["id"], JO, proposal_hash="f" * 64))
    refusals = [(e["actor"], e["detail"]["reason"]) for e in h.db.audit_for_run(run["id"]) if e["event"] == "approval.denied"]
    assert refusals == [("alex.preparer", "NOT_AN_APPROVER"), ("sam.controller", "SEGREGATION_OF_DUTIES"),
                        ("jo.reviewer", "PROPOSAL_CHANGED")]


async def test_approving_an_unknown_run_is_a_404(h: Harness):
    error = await denied(h.engine.approve("RUN-NOPE", JO, proposal_hash="0" * 64))
    assert (error.code, error.status) == ("RUN_NOT_FOUND", 404)
