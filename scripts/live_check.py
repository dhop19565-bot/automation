"""One-shot proof run: send the sample documents to a provider and report what came back.

    python scripts/live_check.py                # real AI; needs ANTHROPIC_API_KEY
    python scripts/live_check.py --provider demo

Uses a throwaway in-memory database. Approves as a second user and posts to the
simulated ledger, so a pass means the whole path worked end to end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.config import Settings  # noqa: E402
from hub.db import Database  # noqa: E402
from hub.engine import EngineError, WorkflowEngine, utc_now  # noqa: E402
from hub.identity import DEMO_USERS  # noqa: E402
from hub.integrations.d365 import SimulatedD365Client  # noqa: E402
from hub.main import FIXTURES, build_providers  # noqa: E402
from hub.providers.base import ProviderRegistry  # noqa: E402
from hub.workflows.derivatives.workflow import DerivativesWorkflow  # noqa: E402


async def main(provider: str) -> int:
    settings = Settings.from_env()
    db = Database(":memory:")
    engine = WorkflowEngine(db=db, providers=ProviderRegistry(build_providers(settings)),
                            workflows=[DerivativesWorkflow()], ledger=SimulatedD365Client(db, utc_now),
                            provider_timeout_seconds=settings.provider_timeout_seconds)
    raw = {"report": json.loads((FIXTURES / "derivatives_report_2026-10.json").read_text()),
           "trial_balance": json.loads((FIXTURES / "trial_balance_2026-10.json").read_text())}
    try:
        run = await engine.submit(workflow=DerivativesWorkflow.name, provider=provider, raw_input=raw,
                                  user=DEMO_USERS["alex.preparer"])
    except EngineError as exc:
        print(f"Could not start: {exc.code} - {exc.message}")
        return 2
    print(f"{run['id']} started with provider '{provider}' ...")
    await engine.wait_idle()
    run = db.get_run(run["id"])
    meta = run["provider_meta"] or {}
    print(f"execution mode : {run['execution_mode']}")
    print(f"model          : {meta.get('model')}")
    print(f"request id     : {meta.get('provider_request_id')}")
    print(f"token usage    : {meta.get('usage')}")
    print(f"status         : {run['status']}")
    if run["error_message"]:
        print(f"error          : {run['error_code']} - {run['error_message']}")
    if run["validation"]:
        print(f"validation     : {run['validation']['summary']}")
        for check in run["validation"]["checks"]:
            if check["status"] == "fail":
                print(f"  FAIL {check['check_id']} {check['scope'] or ''}: expected {check['expected']}, got {check['actual']}")
    if run["status"] != "awaiting_approval":
        return 1
    await engine.approve(run["id"], DEMO_USERS["jo.reviewer"], proposal_hash=run["proposal_hash"])
    await engine.wait_idle()
    run = db.get_run(run["id"])
    print(f"after approval : {run['status']} - batch {run['posting']['batch_number']} (simulated D365 posting)")
    return 0 if run["status"] == "posted" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", default="claude", choices=["claude", "demo", "cowork"])
    sys.exit(asyncio.run(main(parser.parse_args().provider)))
