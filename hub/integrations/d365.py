"""Ledger posting.

``SimulatedD365Client`` is the only implementation. It does NOT contact
Microsoft Dynamics 365 or any other Microsoft service: it writes the batch to a
local table and hands back made-up batch and voucher numbers prefixed ``SIM-``.
The payload it stores is an illustrative shape, not the D365 data-entity contract.

A real integration implements ``LedgerPostingClient`` and is passed to the
engine in place of the simulator; nothing else changes.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Protocol

from ..db import Database

INTEGRATION_SIMULATED = "simulated"
SIMULATED_SYSTEM_LABEL = "Microsoft Dynamics 365 Finance - SIMULATED (no Microsoft system was contacted)"


class DuplicatePostingError(Exception):
    """The ledger already holds a batch for this workflow, entity and period."""


class LedgerPostingClient(Protocol):
    integration_mode: str
    system_label: str

    async def post_journal_batch(self, batch: dict[str, Any]) -> dict[str, Any]: ...


class SimulatedD365Client:
    integration_mode = INTEGRATION_SIMULATED
    system_label = SIMULATED_SYSTEM_LABEL

    def __init__(self, db: Database, clock: Callable[[], str]) -> None:
        self._db = db
        self._clock = clock

    async def post_journal_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Idempotent on ``run_id``: posting the same run again returns the
        original result instead of creating a second batch."""
        existing = self._db.get_sim_batch_for_run(batch["run_id"])
        if existing is not None:
            return {**existing, "replayed": True}

        sequence = self._db.count_sim_batches() + 1
        batch_number = f"SIM-GJB-{sequence:05d}"
        posted_at = self._clock()
        result = {
            "integration_mode": self.integration_mode,
            "system": self.system_label,
            "simulated": True,
            "batch_number": batch_number,
            "legal_entity": batch["entity"],
            "period": batch["period"],
            "posting_date": batch["posting_date"],
            "currency": batch["currency"],
            "posted_at": posted_at,
            "vouchers": [
                {
                    "voucher": f"SIM-GNJL-{sequence:05d}-{index:03d}",
                    "journal_ref": journal["journal_ref"],
                    "description": journal["description"],
                    "lines": len(journal["lines"]),
                    "total_debit": journal["total_debit"],
                    "total_credit": journal["total_credit"],
                }
                for index, journal in enumerate(batch["journals"], start=1)
            ],
            "replayed": False,
        }
        try:
            self._db.insert_sim_batch(
                batch_number=batch_number, run_id=batch["run_id"], workflow=batch["workflow"],
                entity=batch["entity"], period=batch["period"], result=result, posted_at=posted_at)
        except sqlite3.IntegrityError as exc:
            raise DuplicatePostingError(
                f"A batch for {batch['workflow']} / {batch['entity']} / {batch['period']} is already in the ledger."
            ) from exc
        return result
