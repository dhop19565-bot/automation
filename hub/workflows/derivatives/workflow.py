"""Derivatives revaluation workflow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from ...providers.base import ProviderOutputInvalid
from ..base import InvalidInput, Workflow
from . import calc
from .prompt import SYSTEM_PROMPT, build_payload
from .schema import PROPOSAL_JSON_SCHEMA, DerivativesInput, JournalProposal, parse_money
from .validation import validate

ZERO = calc.ZERO


def _errors(exc: ValidationError) -> list[str]:
    out = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        out.append(f"{location}: {error['msg']}" if location else error["msg"])
    return out


def _safe_money(text: str) -> Decimal | None:
    try:
        return parse_money(text)
    except ValueError:
        return None


class DerivativesWorkflow(Workflow):
    name = "derivatives_revaluation"
    title = "Derivatives revaluation"
    checklist_task_key = "derivatives_revaluation"

    def parse_input(self, raw: dict[str, Any]) -> DerivativesInput:
        try:
            return DerivativesInput.model_validate(raw)
        except ValidationError as exc:
            raise InvalidInput("The report or trial balance is not valid.", _errors(exc)) from exc

    def scope(self, data: DerivativesInput) -> tuple[str, str]:
        return data.report.entity, data.report.period

    def dump_input(self, data: DerivativesInput) -> dict[str, Any]:
        return data.model_dump(mode="json")

    def load_input(self, stored: dict[str, Any]) -> DerivativesInput:
        return DerivativesInput.model_validate(stored)

    def provider_task(self, data: DerivativesInput) -> tuple[str, dict[str, Any], dict[str, Any]]:
        return SYSTEM_PROMPT, build_payload(data), PROPOSAL_JSON_SCHEMA

    def review(self, run_id: str, data: DerivativesInput,
               provider_output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            proposal = JournalProposal.model_validate(provider_output)
        except ValidationError as exc:
            raise ProviderOutputInvalid(
                "The provider's answer does not match the journal proposal schema: " + "; ".join(_errors(exc)[:5])
            ) from exc

        report = validate(data, proposal)
        expectation = calc.expected_portfolio(data)
        instruments = {i.instrument_id: i for i in data.report.instruments}

        journals = []
        batch_debit = batch_credit = ZERO
        for journal in proposal.journals:
            lines = []
            total_debit = total_credit = ZERO
            for number, line in enumerate(journal.lines, start=1):
                debit, credit = _safe_money(line.debit), _safe_money(line.credit)
                total_debit += debit or ZERO
                total_credit += credit or ZERO
                lines.append({
                    "line_no": number,
                    "account": line.account,
                    "account_name": calc.POLICY_ACCOUNTS.get(line.account, "Not a policy account"),
                    # Unparseable amounts are shown as the provider wrote them; validation has already failed.
                    "debit": format(debit, "f") if debit is not None else line.debit,
                    "credit": format(credit, "f") if credit is not None else line.credit,
                    "narrative": line.narrative,
                })
            batch_debit += total_debit
            batch_credit += total_credit
            instrument = instruments.get(journal.instrument_id)
            expected = expectation.postings.get(journal.instrument_id)
            journals.append({
                "journal_ref": journal.journal_ref,
                "instrument_id": journal.instrument_id,
                "description": journal.description,
                "rationale": journal.rationale,
                "lines": lines,
                "total_debit": format(total_debit, "f"),
                "total_credit": format(total_credit, "f"),
                "source": None if instrument is None else {
                    "instrument_type": instrument.instrument_type,
                    "hedge_designation": instrument.hedge_designation,
                    "prior_fair_value": format(instrument.prior_fair_value, "f"),
                    "current_fair_value": format(instrument.current_fair_value, "f"),
                    "fair_value_movement": format(expected.fair_value_movement, "f") if expected else None,
                },
            })

        # Entity, period, currency and posting date come from the source report,
        # never from the provider - a model cannot move a journal into another period.
        proposal_out = {
            "run_id": run_id,
            "workflow": self.name,
            "entity": data.report.entity,
            "period": data.report.period,
            "currency": data.report.currency,
            "posting_date": data.report.valuation_date,
            "source_report_id": data.report.report_id,
            "summary": proposal.summary,
            "exceptions": [e.model_dump() for e in proposal.exceptions],
            "journals": journals,
            "totals": {"debit": format(batch_debit, "f"), "credit": format(batch_credit, "f")},
        }
        return proposal_out, report.to_dict()

    def posting_batch(self, run: dict[str, Any]) -> dict[str, Any]:
        proposal = run["proposal"]
        return {
            "run_id": run["id"],
            "workflow": self.name,
            "entity": proposal["entity"],
            "period": proposal["period"],
            "currency": proposal["currency"],
            "posting_date": proposal["posting_date"],
            "approved_by": run["approved_by"],
            "proposal_hash": run["proposal_hash"],
            "journals": [
                {
                    "journal_ref": j["journal_ref"],
                    "description": j["description"],
                    "total_debit": j["total_debit"],
                    "total_credit": j["total_credit"],
                    "lines": [
                        {"account": l["account"], "debit": l["debit"], "credit": l["credit"], "text": l["narrative"]}
                        for l in j["lines"]
                    ],
                }
                for j in proposal["journals"]
            ],
        }
