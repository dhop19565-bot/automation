"""Data contracts for the derivatives revaluation workflow.

All monetary amounts are exact ``Decimal`` values with two decimal places.
They travel as strings in JSON so no float ever touches an accounting number.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator

CENT = Decimal("0.01")
_AMOUNT_RE = re.compile(r"^-?\d{1,15}(\.\d{1,2})?$")
_PERIOD_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def parse_money(value: object) -> Decimal:
    """Parse a monetary amount strictly: plain decimal notation, at most 2 dp.

    Floats are rejected on purpose - 0.1 + 0.2 has no place in a ledger.
    """
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, bool):
        raise ValueError("amount must be a decimal string, not a boolean")
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError("amount must be a decimal string such as '1250.00'")
    if not _AMOUNT_RE.match(text):
        raise ValueError(f"amount {text!r} is not plain decimal notation with at most 2 decimal places")
    try:
        return Decimal(text).quantize(CENT)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise ValueError(f"amount {text!r} is not a valid decimal") from exc


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    PlainSerializer(lambda v: format(v.quantize(CENT), "f"), return_type=str),
]

Designation = Literal["none", "cash_flow_hedge"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- inputs


class Instrument(_Strict):
    instrument_id: str = Field(min_length=1, max_length=40)
    instrument_type: str = Field(min_length=1, max_length=60)
    counterparty: str = Field(min_length=1, max_length=120)
    notional: Money
    notional_currency: str = Field(min_length=3, max_length=3)
    maturity_date: str
    hedge_designation: Designation
    prior_fair_value: Money  # carrying amount at the previous period end (asset +, liability -)
    current_fair_value: Money  # valuation at this period end (asset +, liability -)
    # Cash flow hedges only (cumulative since hedge inception, gains positive):
    cumulative_fv_change_instrument: Money | None = None
    cumulative_fv_change_hedged_item: Money | None = None
    cfh_reserve_opening: Money | None = None  # gain in reserve +, loss in reserve -
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _hedge_fields(self) -> "Instrument":
        hedge = (self.cumulative_fv_change_instrument, self.cumulative_fv_change_hedged_item, self.cfh_reserve_opening)
        if self.hedge_designation == "cash_flow_hedge" and any(v is None for v in hedge):
            raise ValueError(
                f"{self.instrument_id}: cash flow hedges need cumulative_fv_change_instrument, "
                "cumulative_fv_change_hedged_item and cfh_reserve_opening"
            )
        if self.hedge_designation == "none" and any(v is not None for v in hedge):
            raise ValueError(f"{self.instrument_id}: hedge fields supplied for an undesignated instrument")
        return self


class DerivativesReport(_Strict):
    report_id: str = Field(min_length=1, max_length=60)
    source: str = Field(min_length=1, max_length=120)
    entity: str = Field(min_length=1, max_length=20)
    period: str = Field(pattern=_PERIOD_RE)
    valuation_date: str
    currency: str = Field(min_length=3, max_length=3)
    fictional: bool = True
    instruments: list[Instrument] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_ids(self) -> "DerivativesReport":
        ids = [i.instrument_id for i in self.instruments]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate instrument ids in report: {', '.join(dupes)}")
        return self


class TrialBalanceLine(_Strict):
    account: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    category: Literal["asset", "liability", "equity", "income", "expense"]
    balance: Money  # debit positive, credit negative


class TrialBalance(_Strict):
    entity: str = Field(min_length=1, max_length=20)
    period: str = Field(pattern=_PERIOD_RE)
    currency: str = Field(min_length=3, max_length=3)
    fictional: bool = True
    lines: list[TrialBalanceLine] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _unique_accounts(self) -> "TrialBalance":
        accounts = [line.account for line in self.lines]
        dupes = sorted({a for a in accounts if accounts.count(a) > 1})
        if dupes:
            raise ValueError(f"duplicate accounts in trial balance: {', '.join(dupes)}")
        return self

    def balance_of(self, account: str) -> Decimal | None:
        for line in self.lines:
            if line.account == account:
                return line.balance
        return None


class DerivativesInput(_Strict):
    report: DerivativesReport
    trial_balance: TrialBalance

    @model_validator(mode="after")
    def _consistent(self) -> "DerivativesInput":
        for field in ("entity", "period", "currency"):
            if getattr(self.report, field) != getattr(self.trial_balance, field):
                raise ValueError(
                    f"report and trial balance disagree on {field}: "
                    f"{getattr(self.report, field)!r} vs {getattr(self.trial_balance, field)!r}"
                )
        return self


# --------------------------------------------------------------------------- AI output


class JournalLine(_Strict):
    account: str
    debit: str
    credit: str
    narrative: str


class ProposedJournal(_Strict):
    journal_ref: str
    instrument_id: str
    description: str
    rationale: str
    lines: list[JournalLine]


class ProposalException(_Strict):
    instrument_id: str
    severity: Literal["info", "warning", "error"]
    message: str


class JournalProposal(_Strict):
    """What a provider must return. Amounts stay as raw strings here so the
    validator - not the parser - decides whether they are acceptable."""

    summary: str
    journals: list[ProposedJournal]
    exceptions: list[ProposalException]


# JSON Schema sent to providers. Kept to the subset that structured outputs
# accept: every property required, additionalProperties false, no numeric bounds.
PROPOSAL_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "journals", "exceptions"],
    "properties": {
        "summary": {"type": "string", "description": "Two or three sentences for the reviewer."},
        "journals": {
            "type": "array",
            "description": "Exactly one journal per instrument whose balances change this period.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["journal_ref", "instrument_id", "description", "rationale", "lines"],
                "properties": {
                    "journal_ref": {"type": "string"},
                    "instrument_id": {"type": "string"},
                    "description": {"type": "string"},
                    "rationale": {"type": "string", "description": "Show the arithmetic behind each amount."},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["account", "debit", "credit", "narrative"],
                            "properties": {
                                "account": {"type": "string"},
                                "debit": {"type": "string", "description": "Decimal string, 2 dp, e.g. '19500.00'. '0.00' if none."},
                                "credit": {"type": "string", "description": "Decimal string, 2 dp, e.g. '19500.00'. '0.00' if none."},
                                "narrative": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "exceptions": {
            "type": "array",
            "description": "Anything the reviewer should look at. Empty if nothing.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["instrument_id", "severity", "message"],
                "properties": {
                    "instrument_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                    "message": {"type": "string"},
                },
            },
        },
    },
}
