"""Independent validation of an AI journal proposal.

Every figure is recomputed from the source report by ``calc`` and compared with
what the provider proposed. A proposal is approvable only if no blocking check
fails. Nothing here trusts, or is derived from, provider output.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from . import calc
from .schema import DerivativesInput, JournalProposal, ProposedJournal, parse_money

ZERO = calc.ZERO


def _fmt(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


@dataclass
class Check:
    check_id: str
    name: str
    status: str  # "pass" | "fail" | "warn"
    blocking: bool
    detail: str
    expected: str | None = None
    actual: str | None = None
    scope: str | None = None  # instrument id or account, when the check is specific


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, check_id: str, name: str, ok: bool, detail: str, *, blocking: bool = True,
            expected: str | None = None, actual: str | None = None, scope: str | None = None) -> None:
        status = "pass" if ok else ("fail" if blocking else "warn")
        self.checks.append(Check(check_id, name, status, blocking, detail, expected, actual, scope))

    @property
    def passed(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def to_dict(self) -> dict:
        failed = [c for c in self.checks if c.status == "fail"]
        return {
            "passed": self.passed,
            "summary": {
                "total": len(self.checks),
                "passed": sum(c.status == "pass" for c in self.checks),
                "failed": len(failed),
                "warnings": sum(c.status == "warn" for c in self.checks),
            },
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass
class _ParsedJournal:
    journal: ProposedJournal
    net_by_account: dict[str, Decimal]
    total_debit: Decimal
    total_credit: Decimal
    line_errors: list[str]


def _parse_journal(journal: ProposedJournal) -> _ParsedJournal:
    net: dict[str, Decimal] = {}
    total_debit = total_credit = ZERO
    errors: list[str] = []
    if not journal.lines:
        errors.append("journal has no lines")
    for index, line in enumerate(journal.lines, start=1):
        try:
            debit, credit = parse_money(line.debit), parse_money(line.credit)
        except ValueError as exc:
            errors.append(f"line {index}: {exc}")
            continue
        if debit < ZERO or credit < ZERO:
            errors.append(f"line {index}: negative amounts are not allowed")
            continue
        if (debit > ZERO) == (credit > ZERO):
            errors.append(f"line {index}: exactly one of debit/credit must be non-zero")
            continue
        total_debit += debit
        total_credit += credit
        net[line.account] = net.get(line.account, ZERO) + debit - credit
    return _ParsedJournal(journal, net, total_debit, total_credit, errors)


def validate(data: DerivativesInput, proposal: JournalProposal) -> ValidationReport:
    report = ValidationReport()
    expectation = calc.expected_portfolio(data)
    tb = data.trial_balance

    # ---------------------------------------------------------------- source data
    tb_total = sum((line.balance for line in tb.lines), ZERO)
    report.add("IN-01", "Trial balance sums to zero", tb_total == ZERO,
               "Debits and credits in the supplied trial balance must net to nil.",
               expected="0.00", actual=_fmt(tb_total))

    openings = [
        ("IN-02", calc.ACCOUNT_DERIVATIVE_ASSET, expectation.opening_asset,
         "Opening derivative assets agree to the report's prior fair values"),
        ("IN-03", calc.ACCOUNT_DERIVATIVE_LIABILITY, -expectation.opening_liability,
         "Opening derivative liabilities agree to the report's prior fair values"),
        ("IN-04", calc.ACCOUNT_CFH_RESERVE, -expectation.opening_reserve,
         "Opening cash flow hedge reserve agrees to the report"),
    ]
    for check_id, account, expected_balance, name in openings:
        actual = tb.balance_of(account)
        report.add(check_id, name, actual == expected_balance,
                   "A break here means the ledger and the valuation report start from different "
                   "positions, so no revaluation journal can be relied on.",
                   expected=_fmt(expected_balance),
                   actual=_fmt(actual) if actual is not None else "account missing", scope=account)

    missing_accounts = [a for a in calc.POLICY_ACCOUNTS if tb.balance_of(a) is None]
    report.add("IN-05", "Policy accounts exist in the trial balance", not missing_accounts,
               "Every account the policy can post to must be on the ledger.",
               expected="all present", actual=", ".join(missing_accounts) or "all present")

    # ---------------------------------------------------------------- journal structure
    parsed = [_parse_journal(j) for j in proposal.journals]
    known_ids = set(expectation.postings)

    refs = [p.journal.journal_ref for p in parsed]
    dupe_refs = sorted({r for r in refs if refs.count(r) > 1})
    report.add("JE-01", "Journal references are unique", not dupe_refs,
               "A repeated reference would make the batch ambiguous in the ledger.",
               expected="no duplicates", actual=", ".join(dupe_refs) or "no duplicates")

    journal_ids = [p.journal.instrument_id for p in parsed]
    unknown = sorted({i for i in journal_ids if i not in known_ids})
    repeated = sorted({i for i in journal_ids if journal_ids.count(i) > 1})
    required = {i for i, posting in expectation.postings.items() if posting.requires_journal}
    missing = sorted(required - set(journal_ids))
    coverage_ok = not (unknown or repeated or missing)
    problems = []
    if unknown:
        problems.append(f"unknown instruments: {', '.join(unknown)}")
    if repeated:
        problems.append(f"more than one journal for: {', '.join(repeated)}")
    if missing:
        problems.append(f"no journal for: {', '.join(missing)}")
    report.add("JE-02", "Exactly one journal per instrument that moved", coverage_ok,
               "Every instrument in the report with a balance change is journalled once; nothing else is.",
               expected=f"{len(required)} journals", actual="; ".join(problems) or f"{len(journal_ids)} journals")

    by_instrument: dict[str, _ParsedJournal] = {}
    for item in parsed:
        ref, instrument_id = item.journal.journal_ref, item.journal.instrument_id
        report.add("JE-03", "Line amounts are well-formed", not item.line_errors,
                   "Each line carries a single non-negative amount with at most two decimal places.",
                   expected="no errors", actual="; ".join(item.line_errors) or "no errors", scope=ref)
        report.add("JE-04", "Journal balances", item.total_debit == item.total_credit,
                   "Total debits must equal total credits.",
                   expected=_fmt(item.total_debit), actual=_fmt(item.total_credit), scope=ref)
        off_policy = sorted(set(item.net_by_account) - set(calc.POLICY_ACCOUNTS))
        report.add("JE-05", "Only policy accounts are used", not off_policy,
                   "This workflow may post to derivative, hedge reserve and derivative P&L accounts only.",
                   expected=", ".join(calc.POLICY_ACCOUNTS), actual=", ".join(off_policy) or "policy accounts only",
                   scope=ref)
        by_instrument.setdefault(instrument_id, item)

    # ---------------------------------------------------------------- recalculation
    for instrument_id, posting in expectation.postings.items():
        item = by_instrument.get(instrument_id)
        if item is None:
            if posting.requires_journal:
                report.add("CALC-01", "Proposed amounts equal the independent recalculation", False,
                           "No journal was proposed for this instrument.",
                           expected=_net_text(posting.net_by_account), actual="no journal", scope=instrument_id)
            continue
        proposed = {a: v for a, v in item.net_by_account.items() if v != ZERO}
        report.add("CALC-01", "Proposed amounts equal the independent recalculation",
                   proposed == posting.net_by_account and not item.line_errors,
                   "Net debit/(credit) per account, recomputed from the report without reference to the AI output.",
                   expected=_net_text(posting.net_by_account), actual=_net_text(proposed), scope=instrument_id)

    # ---------------------------------------------------------------- ledger tie-out
    batch_net: dict[str, Decimal] = {}
    for item in parsed:
        for account, amount in item.net_by_account.items():
            batch_net[account] = batch_net.get(account, ZERO) + amount

    tie_outs = [
        ("TIE-01", calc.ACCOUNT_DERIVATIVE_ASSET, expectation.closing_asset,
         "Pro-forma derivative assets equal total positive fair values"),
        ("TIE-02", calc.ACCOUNT_DERIVATIVE_LIABILITY, -expectation.closing_liability,
         "Pro-forma derivative liabilities equal total negative fair values"),
        ("TIE-03", calc.ACCOUNT_CFH_RESERVE, -expectation.closing_reserve,
         "Pro-forma cash flow hedge reserve equals the effective portion"),
    ]
    for check_id, account, expected_closing, name in tie_outs:
        opening = tb.balance_of(account)
        actual = (opening if opening is not None else ZERO) + batch_net.get(account, ZERO)
        report.add(check_id, name, opening is not None and actual == expected_closing,
                   "Opening ledger balance plus the proposed batch must land on the valuation report.",
                   expected=_fmt(expected_closing), actual=_fmt(actual), scope=account)

    pnl_net = batch_net.get(calc.ACCOUNT_PNL_FAIR_VALUE, ZERO) + batch_net.get(calc.ACCOUNT_PNL_INEFFECTIVENESS, ZERO)
    report.add("TIE-04", "P&L impact equals the independent recalculation", -pnl_net == expectation.pnl_total,
               "Net gain/(loss) taken to profit or loss by the batch.",
               expected=_fmt(expectation.pnl_total), actual=_fmt(-pnl_net))

    total_debit = sum((p.total_debit for p in parsed), ZERO)
    total_credit = sum((p.total_credit for p in parsed), ZERO)
    report.add("TIE-05", "Batch balances", total_debit == total_credit,
               "Total debits equal total credits across the whole batch.",
               expected=_fmt(total_debit), actual=_fmt(total_credit))

    # ---------------------------------------------------------------- reviewer attention
    flagged = [e for e in proposal.exceptions if e.severity in ("warning", "error")]
    report.add("REV-01", "Exceptions raised by the AI need reviewer attention", not flagged,
               "Non-blocking. The AI flagged items for the reviewer to read before approving.",
               blocking=False, expected="none",
               actual="; ".join(f"{e.instrument_id}: {e.message}" for e in flagged) or "none")
    return report


def _net_text(net: dict[str, Decimal]) -> str:
    if not net:
        return "no posting"
    parts = []
    for account in sorted(net):
        amount = net[account]
        parts.append(f"{account} {'Dr' if amount > ZERO else 'Cr'} {_fmt(abs(amount))}")
    return ", ".join(parts)
