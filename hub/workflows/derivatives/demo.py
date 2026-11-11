"""Scripted stand-in for an AI answer, used by the DemoProvider.

It works from the same payload a real model receives and returns the same JSON
shape. It deliberately does not import ``calc``: the validator must be checking
somebody else's arithmetic, even in demo mode. The two failing scenarios exist
to show the validator catching a wrong proposal.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

SCENARIOS = ["clean", "calc_error", "unbalanced"]
SCENARIO_LABELS = {
    "clean": "Correct proposal",
    "calc_error": "Wrong hedge accounting (skips the lower-of test)",
    "unbalanced": "Journal that does not balance",
}

_D0 = Decimal("0.00")


def _line(account: str, amount: Decimal, narrative: str) -> dict[str, str]:
    """amount > 0 is a debit, < 0 a credit."""
    return {
        "account": account,
        "debit": format(amount if amount > 0 else _D0, "f"),
        "credit": format(-amount if amount < 0 else _D0, "f"),
        "narrative": narrative,
    }


def simulate(payload: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    scenario = options.get("demo_scenario", "clean")
    period = payload["period"]
    journals: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []

    for inst in payload["valuation_report"]["instruments"]:
        iid = inst["instrument_id"]
        prior, current = Decimal(inst["prior_fair_value"]), Decimal(inst["current_fair_value"])
        movement = current - prior
        lines: list[dict[str, str]] = []

        asset_delta = max(current, _D0) - max(prior, _D0)
        liability_delta = max(-current, _D0) - max(-prior, _D0)
        if asset_delta:
            lines.append(_line("1500", asset_delta, f"{iid} derivative asset to {max(current, _D0)}"))
        if liability_delta:
            lines.append(_line("2500", -liability_delta, f"{iid} derivative liability to {max(-current, _D0)}"))
        if (prior > 0 > current) or (prior < 0 < current):
            exceptions.append({"instrument_id": iid, "severity": "info",
                               "message": "Fair value changed sign; carrying amount moved between asset and liability."})
        if prior == 0:
            exceptions.append({"instrument_id": iid, "severity": "info",
                               "message": "No prior fair value - treated as a trade entered into this period."})

        if inst["hedge_designation"] == "cash_flow_hedge":
            cum_inst = Decimal(inst["cumulative_fv_change_instrument"])
            cum_item = Decimal(inst["cumulative_fv_change_hedged_item"])
            opening = Decimal(inst["cfh_reserve_opening"])
            if scenario == "calc_error":
                oci = movement  # wrong on purpose: treats the whole movement as effective
            else:
                offsetting = cum_inst * cum_item < 0
                closing = min(abs(cum_inst), abs(cum_item)) if offsetting else _D0
                closing = closing if cum_inst > 0 else -closing
                oci = closing - opening
            ineffective = movement - oci
            if oci:
                lines.append(_line("3500", -oci, f"{iid} effective portion to cash flow hedge reserve"))
            if ineffective:
                lines.append(_line("7110", -ineffective, f"{iid} hedge ineffectiveness"))
                exceptions.append({"instrument_id": iid, "severity": "warning",
                                   "message": f"Hedge ineffectiveness of {abs(ineffective)} recognised in P&L."})
            rationale = (f"Movement {current} - ({prior}) = {movement}. OCI movement {oci}; "
                         f"ineffectiveness {ineffective}.")
        else:
            if movement:
                lines.append(_line("7100", -movement, f"{iid} fair value {'gain' if movement > 0 else 'loss'}"))
            rationale = f"Not designated. Movement {current} - ({prior}) = {movement} taken to P&L."

        if lines:
            journals.append({
                "journal_ref": f"DRV-{period}-{iid}",
                "instrument_id": iid,
                "description": f"Derivative revaluation {period} - {iid} ({inst['instrument_type']})",
                "rationale": rationale,
                "lines": lines,
            })

    if scenario == "unbalanced" and journals:
        first = journals[0]["lines"][0]
        side = "debit" if Decimal(first["debit"]) > 0 else "credit"
        first[side] = format(Decimal(first[side]) + Decimal("100.00"), "f")

    return {
        "summary": (f"Simulated proposal for {payload['entity']} {period}: {len(journals)} revaluation journals "
                    f"from {len(payload['valuation_report']['instruments'])} instruments."),
        "journals": journals,
        "exceptions": exceptions,
    }
