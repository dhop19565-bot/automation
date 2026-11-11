"""Duplicate prevention: runs, approvals per period, and ledger postings."""
from __future__ import annotations

import asyncio

import pytest

from hub.engine import EngineError, utc_now
from hub.integrations.d365 import DuplicatePostingError, SimulatedD365Client

from .conftest import ALEX, JO, SAM, Harness, ScriptedProvider, clean_output, load_input


async def test_identical_submission_is_blocked_while_the_first_is_live(h: Harness):
    first = await h.submit_and_wait()
    with pytest.raises(EngineError) as exc:
        await h.submit()
    assert (exc.value.code, exc.value.status) == ("DUPLICATE_RUN", 409)
    assert exc.value.extra["existing_run_id"] == first["id"]
    assert len(h.db.list_runs()) == 1 and len(h.scripted.calls) == 1
    assert "run.duplicate_blocked" in h.events(first["id"])


async def test_a_different_user_cannot_get_around_the_duplicate_check(h: Harness):
    await h.submit_and_wait(user=ALEX)
    with pytest.raises(EngineError) as exc:
        await h.submit(user=SAM)
    assert exc.value.code == "DUPLICATE_RUN"


async def test_concurrent_identical_submissions_produce_one_run():
    h = Harness(scripted=ScriptedProvider(output=clean_output(), hold=True))
    results = await asyncio.gather(*(h.submit() for _ in range(8)), return_exceptions=True)
    created = [r for r in results if isinstance(r, dict)]
    blocked = [r for r in results if isinstance(r, EngineError)]
    assert len(created) == 1 and len(blocked) == 7 and all(e.code == "DUPLICATE_RUN" for e in blocked)
    h.scripted.gate.set()
    await h.engine.wait_idle()
    assert len(h.db.list_runs()) == 1 and len(h.scripted.calls) == 1


async def test_a_posted_run_still_blocks_an_identical_submission(h: Harness):
    run = await h.submit_and_wait()
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    with pytest.raises(EngineError) as exc:
        await h.submit()
    assert exc.value.code == "DUPLICATE_RUN"


async def test_resubmission_is_allowed_after_failure_or_rejection(h: Harness):
    failed = await h.submit_and_wait(provider="demo", options={"demo_scenario": "unbalanced"})
    assert failed["status"] == "validation_failed"
    again = await h.submit_and_wait(provider="demo", options={"demo_scenario": "unbalanced"})
    assert again["id"] != failed["id"]

    first = await h.submit_and_wait()
    await h.engine.reject(first["id"], JO, reason="Valuation statement superseded")
    second = await h.submit_and_wait()
    assert second["id"] != first["id"] and second["status"] == "awaiting_approval"


async def test_changed_source_data_is_a_new_run_not_a_duplicate(h: Harness):
    await h.submit_and_wait()
    raw = load_input()
    raw["report"]["instruments"][0]["notes"] = "Revised statement"
    other = await h.submit_and_wait(raw_input=raw)
    assert other["status"] == "awaiting_approval" and len(h.db.list_runs()) == 2


async def test_only_one_run_per_period_can_be_approved(h: Harness):
    """Two proposals for the same period may coexist, but only one can ever be approved."""
    a = await h.submit_and_wait(provider="scripted")
    b = await h.submit_and_wait(provider="demo")
    await h.engine.approve(a["id"], JO, proposal_hash=a["proposal_hash"])
    await h.engine.wait_idle()

    with pytest.raises(EngineError) as exc:
        await h.engine.approve(b["id"], JO, proposal_hash=b["proposal_hash"])
    assert (exc.value.code, exc.value.status) == ("PERIOD_ALREADY_APPROVED", 409) and a["id"] in exc.value.message
    assert h.run(b["id"])["status"] == "awaiting_approval"
    assert h.db.count_sim_batches() == 1 and len(h.ledger.calls) == 1
    assert h.checklist_item()["status"] == "complete" and h.checklist_item()["run_id"] == a["id"]


async def test_the_database_itself_refuses_a_second_committed_run(h: Harness):
    """The rule holds even if application code is bypassed."""
    import sqlite3

    a = await h.submit_and_wait(provider="scripted")
    b = await h.submit_and_wait(provider="demo")
    assert h.db.transition(a["id"], ["awaiting_approval"], "approved", utc_now())
    with pytest.raises(sqlite3.IntegrityError):
        h.db.transition(b["id"], ["awaiting_approval"], "approved", utc_now())


async def test_posting_the_same_run_twice_returns_the_original_batch(h: Harness):
    run = await h.submit_and_wait()
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    posted = h.run(run["id"])
    batch = h.engine.workflows[run["workflow"]].posting_batch(posted)

    replay = await h.ledger.inner.post_journal_batch(batch)
    assert replay["replayed"] is True and replay["batch_number"] == posted["posting"]["batch_number"]
    assert [v["voucher"] for v in replay["vouchers"]] == [v["voucher"] for v in posted["posting"]["vouchers"]]
    assert h.db.count_sim_batches() == 1


async def test_ledger_refuses_a_second_batch_for_the_same_period(h: Harness):
    run = await h.submit_and_wait()
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    batch = h.engine.workflows[run["workflow"]].posting_batch(h.run(run["id"]))
    with pytest.raises(DuplicatePostingError):
        await SimulatedD365Client(h.db, utc_now).post_journal_batch({**batch, "run_id": "RUN-SOMEONE-ELSE"})
    assert h.db.count_sim_batches() == 1


async def test_retrying_a_failed_posting_posts_exactly_once(h: Harness):
    h.ledger.fail_next = 1
    run = await h.submit_and_wait()
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    stuck = h.run(run["id"])
    assert (stuck["status"], stuck["error_code"]) == ("posting_failed", "POSTING_FAILED")
    assert h.checklist_item()["status"] == "blocked" and h.db.count_sim_batches() == 0

    with pytest.raises(EngineError):  # a preparer cannot push it through
        await h.engine.retry_posting(run["id"], ALEX)
    await h.engine.retry_posting(run["id"], JO)
    await h.engine.wait_idle()
    done = h.run(run["id"])
    assert done["status"] == "posted" and done["error_code"] is None
    assert h.db.count_sim_batches() == 1 and h.checklist_item()["status"] == "complete"
    with pytest.raises(EngineError):
        await h.engine.retry_posting(run["id"], JO)
