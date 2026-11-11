"""Independent accounting calculations for derivative revaluation.

This module is the control. It never reads anything a provider produced: it
recomputes, from the source report alone, what must hit each ledger account.
Everything is exact ``Decimal`` arithmetic.

Policy implemented (deliberately simplified, fictional entity, no tax, no CVA/DVA):

* Presentation - an instrument with positive fair value sits in derivative
  assets, negative fair value in derivative liabilities. A sign flip moves the
  carrying amount from one account to the other.
* Not designated - the whole fair value movement goes to P&L.
* Cash flow hedge - the reserve is reset to the *lower of* (absolute) cumulative
  gain/loss on the instrument and cumulative change in value of the hedged item
  (the IFRS 9 6.5.11 test). If the two do not offset (same sign, or either is
  zero) nothing is effective. The period OCI movement is closing reserve less
  opening reserve; the rest of the fair value movement is ineffectiveness in P&L.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .schema import DerivativesInput, Instrument

ZERO = Decimal("0.00")

ACCOUNT_DERIVATIVE_ASSET = "1500"
ACCOUNT_DERIVATIVE_LIABILITY = "2500"
ACCOUNT_CFH_RESERVE = "3500"
ACCOUNT_PNL_FAIR_VALUE = "7100"
ACCOUNT_PNL_INEFFECTIVENESS = "7110"

POLICY_ACCOUNTS: dict[str, str] = {
    ACCOUNT_DERIVATIVE_ASSET: "Derivative financial assets",
    ACCOUNT_DERIVATIVE_LIABILITY: "Derivative financial liabilities",
    ACCOUNT_CFH_RESERVE: "Cash flow hedge reserve (OCI)",
    ACCOUNT_PNL_FAIR_VALUE: "Fair value gains/losses on derivatives (P&L)",
    ACCOUNT_PNL_INEFFECTIVENESS: "Hedge ineffectiveness (P&L)",
}


def _pos(value: Decimal) -> Decimal:
    return value if value > ZERO else ZERO


@dataclass(frozen=True)
class ExpectedPosting:
    """Expected net effect of one instrument, by account, debit-positive."""

    instrument_id: str
    fair_value_movement: Decimal  # gain positive
    asset_change: Decimal  # increase in derivative asset
    liability_change: Decimal  # increase in derivative liability
    oci_movement: Decimal  # gain recognised in OCI positive
    pnl_fair_value: Decimal  # gain positive (undesignated instruments)
    pnl_ineffectiveness: Decimal  # gain positive (cash flow hedges)
    cfh_reserve_closing: Decimal | None

    @property
    def net_by_account(self) -> dict[str, Decimal]:
        """Debit-positive net amount per account; zero entries omitted."""
        raw = {
            ACCOUNT_DERIVATIVE_ASSET: self.asset_change,
            ACCOUNT_DERIVATIVE_LIABILITY: -self.liability_change,
            ACCOUNT_CFH_RESERVE: -self.oci_movement,
            ACCOUNT_PNL_FAIR_VALUE: -self.pnl_fair_value,
            ACCOUNT_PNL_INEFFECTIVENESS: -self.pnl_ineffectiveness,
        }
        return {account: amount for account, amount in raw.items() if amount != ZERO}

    @property
    def requires_journal(self) -> bool:
        return bool(self.net_by_account)


def cfh_reserve_closing(cumulative_instrument: Decimal, cumulative_hedged_item: Decimal) -> Decimal:
    """Lower-of test. Result carries the sign of the instrument's cumulative gain/loss."""
    offsetting = (cumulative_instrument > ZERO > cumulative_hedged_item) or (
        cumulative_instrument < ZERO < cumulative_hedged_item
    )
    if not offsetting:
        return ZERO
    effective = min(abs(cumulative_instrument), abs(cumulative_hedged_item))
    return effective if cumulative_instrument > ZERO else -effective


def expected_posting(instrument: Instrument) -> ExpectedPosting:
    prior, current = instrument.prior_fair_value, instrument.current_fair_value
    movement = current - prior
    asset_change = _pos(current) - _pos(prior)
    liability_change = _pos(-current) - _pos(-prior)

    if instrument.hedge_designation == "cash_flow_hedge":
        closing = cfh_reserve_closing(
            instrument.cumulative_fv_change_instrument,  # type: ignore[arg-type]
            instrument.cumulative_fv_change_hedged_item,  # type: ignore[arg-type]
        )
        oci = closing - instrument.cfh_reserve_opening  # type: ignore[operator]
        return ExpectedPosting(
            instrument_id=instrument.instrument_id,
            fair_value_movement=movement,
            asset_change=asset_change,
            liability_change=liability_change,
            oci_movement=oci,
            pnl_fair_value=ZERO,
            pnl_ineffectiveness=movement - oci,
            cfh_reserve_closing=closing,
        )

    return ExpectedPosting(
        instrument_id=instrument.instrument_id,
        fair_value_movement=movement,
        asset_change=asset_change,
        liability_change=liability_change,
        oci_movement=ZERO,
        pnl_fair_value=movement,
        pnl_ineffectiveness=ZERO,
        cfh_reserve_closing=None,
    )


@dataclass(frozen=True)
class PortfolioExpectation:
    postings: dict[str, ExpectedPosting]
    opening_asset: Decimal
    opening_liability: Decimal
    opening_reserve: Decimal  # gain positive
    closing_asset: Decimal
    closing_liability: Decimal
    closing_reserve: Decimal  # gain positive
    pnl_total: Decimal  # gain positive


def expected_portfolio(data: DerivativesInput) -> PortfolioExpectation:
    instruments = data.report.instruments
    postings = {i.instrument_id: expected_posting(i) for i in instruments}
    hedges = [i for i in instruments if i.hedge_designation == "cash_flow_hedge"]
    return PortfolioExpectation(
        postings=postings,
        opening_asset=sum((_pos(i.prior_fair_value) for i in instruments), ZERO),
        opening_liability=sum((_pos(-i.prior_fair_value) for i in instruments), ZERO),
        opening_reserve=sum((i.cfh_reserve_opening for i in hedges), ZERO),  # type: ignore[misc]
        closing_asset=sum((_pos(i.current_fair_value) for i in instruments), ZERO),
        closing_liability=sum((_pos(-i.current_fair_value) for i in instruments), ZERO),
        closing_reserve=sum((postings[i.instrument_id].cfh_reserve_closing for i in hedges), ZERO),  # type: ignore[misc]
        pnl_total=sum((p.pnl_fair_value + p.pnl_ineffectiveness for p in postings.values()), ZERO),
    )
