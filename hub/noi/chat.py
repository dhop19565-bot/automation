"""Ask-a-question over the NOI dataset.

The provider (Claude, or the offline simulator) receives figures already
computed by ``metrics.Portfolio`` and returns prose plus *suggestions*: chart
specs, filter changes or a property to open. Suggestions are validated here and
any chart is then drawn from the dataset by the same code as the rest of the
dashboard. The prose itself is model output: it is labelled as such in the UI
and is not independently checked.
"""
from __future__ import annotations

import re
from typing import Any

from .metrics import (BRIDGE_DIMENSIONS, COMPARE_LABEL, RANK_DIMENSIONS, RANK_METRICS, TREND_BASE_METRICS,
                      NoiError, Portfolio, View)

WORKFLOW = "noi_chat"
MAX_QUESTION_CHARS = 1000
MAX_HISTORY_TURNS = 4
MAX_SUGGESTIONS = 4
CLOSING_REQUEST = "Answer the question in `question`."

SYSTEM_PROMPT = """\
You are the analyst behind a net operating income (NOI) dashboard for a fictional UK property portfolio. \
A finance user is looking at the dashboard and asks you a question in a chat box beside it. Your answer \
appears in that box, and the user can pin it to the dashboard and print it, so it may end up in front of \
someone who never saw the question.

What you are given

The request data holds every figure you may use, already added up by the dashboard's own code: totals \
for what the user is looking at (`in_view`), every property for the selected period, sector and region \
totals, and monthly series for the full year. Amounts are whole pounds. Revenue lines are positive \
(vacancy and credit loss is negative), cost lines are positive, and NOI is revenue less operating \
expenses. `view` says which period, comparison basis (budget or prior year) and filters the user has on \
screen; answer for that period and basis unless the question asks for something else.

How to answer

- Take figures from the data rather than estimating them. Simple arithmetic on those figures (a \
difference, a share, a sum of a few properties) is fine; say what you combined. If the data cannot \
answer the question - a tenant name, a forecast, a lease term - say so plainly instead of guessing.
- Lead with the answer, then the two or three figures that support it. Name the period and whether a \
variance is against budget or prior year. A higher cost is unfavourable; say which way each variance cuts.
- Where a property's `note` explains a movement, use it, and attribute it as the note on file.
- Write plain prose for a finance reader: no markdown, no headings, no tables. Short paragraphs; a few \
lines starting with "- " are fine for a list. Format money like £1.24m or £386k, and percentages to one \
decimal place. Keep it under about 150 words unless the question needs more.

Suggestions

After the answer, offer up to three suggestions that would let the user see the evidence on the \
dashboard. Each becomes a button. Leave the list empty when nothing would help.
- kind "add_chart": a chart the dashboard draws from the data. Set `chart` and the fields it uses:
  - "trend": `metric` over the twelve months, actual against the comparison.
  - "rank": bars of `metric` by `dimension` (property, sector, region), or the lines themselves with \
dimension "opex_line" or "revenue_line". `order` "desc" or "asc"; `limit` 0 for all.
  - "bridge": a waterfall from comparison NOI to actual NOI, by `dimension` (line, property, sector, region).
  - "statement": the NOI statement, line by line, actual against comparison.
  - "table": the property table. "kpi": the headline tiles.
  Use `sector`, `region` or `property_id` to pin the chart to part of the portfolio; otherwise it follows \
the dashboard's filters.
- kind "set_view": change the dashboard's filters: `sector`, `region`, `compare`, `period_from`, `period_to`.
- kind "open_property": drill into `property_id`.
Fill fields a suggestion does not use with "none" (or "all" for sector and region, "keep" for compare, \
"" for property_id and the period fields, 0 for limit). `label` is the button text: a few words, such \
as "Chart utilities by month".
"""


def output_schema(portfolio: Portfolio) -> dict[str, Any]:
    suggestion = {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "label", "chart", "metric", "dimension", "order", "limit", "sector", "region",
                     "property_id", "compare", "period_from", "period_to"],
        "properties": {
            "kind": {"type": "string", "enum": ["add_chart", "set_view", "open_property"]},
            "label": {"type": "string"},
            "chart": {"type": "string", "enum": ["trend", "rank", "bridge", "statement", "table", "kpi", "none"]},
            "metric": {"type": "string",
                       "enum": sorted({*RANK_METRICS, *TREND_BASE_METRICS, *portfolio.line_label, "none"})},
            "dimension": {"type": "string", "enum": sorted({*RANK_DIMENSIONS, *BRIDGE_DIMENSIONS, "none"})},
            "order": {"type": "string", "enum": ["desc", "asc"]},
            "limit": {"type": "integer"},
            "sector": {"type": "string", "enum": [*portfolio.sectors, "all"]},
            "region": {"type": "string", "enum": [*portfolio.regions, "all"]},
            "property_id": {"type": "string"},
            "compare": {"type": "string", "enum": ["budget", "prior", "keep"]},
            "period_from": {"type": "string"},
            "period_to": {"type": "string"},
        },
    }
    return {"type": "object", "additionalProperties": False, "required": ["answer", "suggestions"],
            "properties": {"answer": {"type": "string"}, "suggestions": {"type": "array", "items": suggestion}}}


# --------------------------------------------------------------------------- request payload


def _figures(portfolio: Portfolio, props: list[dict], idx: range, bases: tuple[str, ...]) -> dict[str, Any]:
    out = {}
    for basis in bases:
        m = portfolio.measure(props, idx, basis)
        out[basis] = {"revenue": m["revenue"], "operating_expenses": m["opex"], "noi": m["noi"],
                      "noi_margin": m["margin"], "occupancy": m["occupancy"], "noi_per_sq_ft": m["noi_psf"],
                      "lines": m["lines"]}
    return out


def build_payload(portfolio: Portfolio, view: View, question: str,
                  history: list[dict[str, str]]) -> dict[str, Any]:
    bases = ("actual", "budget", "prior")
    in_view, in_view_label = portfolio.scope(view)
    months = range(len(portfolio.months))

    def monthly(props: list[dict], basis: str, metric: str) -> list[Any]:
        return [portfolio.measure(props, range(i, i + 1), basis)[metric] for i in months]

    return {
        "question": question,
        "earlier_in_this_conversation": history,
        "dataset": {"portfolio": portfolio.name, "fictional": True, "currency": portfolio.currency,
                    "amounts": "whole pounds", "months": portfolio.months,
                    "prior_year_basis": portfolio.raw.get("prior_basis"),
                    "lines": portfolio.lines, "sectors": portfolio.sectors, "regions": portfolio.regions},
        "view": {**portfolio.view_dict(view), "showing": in_view_label},
        "in_view": _figures(portfolio, in_view, view.idx, bases),
        "properties_for_period": [
            {"id": p["id"], "name": p["name"], "sector": p["sector"], "region": p["region"], "city": p["city"],
             "sq_ft": p["sqft"], "note": p["note"], "in_view": p in in_view,
             **_figures(portfolio, [p], view.idx, bases)}
            for p in portfolio.properties],
        "sectors_for_period": {s: _figures(portfolio, [p for p in portfolio.properties if p["sector"] == s],
                                           view.idx, bases) for s in portfolio.sectors},
        "regions_for_period": {r: _figures(portfolio, [p for p in portfolio.properties if p["region"] == r],
                                           view.idx, bases) for r in portfolio.regions},
        "monthly_full_year": {
            "in_view": {basis: {"revenue": monthly(in_view, basis, "revenue"),
                                "operating_expenses": monthly(in_view, basis, "opex"),
                                "noi": monthly(in_view, basis, "noi")} for basis in bases},
            "in_view_operating_expense_lines_actual": {
                k: [portfolio.measure(in_view, range(i, i + 1), "actual")["lines"][k] for i in months]
                for k in portfolio.opex_keys},
            "noi_by_property": {p["id"]: {"actual": monthly([p], "actual", "noi"),
                                          "budget": monthly([p], "budget", "noi")} for p in portfolio.properties},
            "occupancy_by_property_actual": {p["id"]: p["occupancy"]["actual"] for p in portfolio.properties},
        },
    }


def clean_history(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    turns = [{"question": str(t.get("question", ""))[:MAX_QUESTION_CHARS], "answer": str(t.get("answer", ""))[:3000]}
             for t in raw if isinstance(t, dict) and t.get("question") and t.get("answer")]
    return turns[-MAX_HISTORY_TURNS:]


# --------------------------------------------------------------------------- provider output


def normalise_answer(portfolio: Portfolio, view: View, output: Any) -> tuple[str, list[dict[str, Any]], list[str]]:
    """(answer, validated suggestions, notes about anything dropped). Raises ``NoiError`` if unusable."""
    if not isinstance(output, dict) or not isinstance(output.get("answer"), str) or not output["answer"].strip():
        raise NoiError("The assistant returned no answer.")
    suggestions, notes = [], []
    raw = output.get("suggestions")
    for item in (raw if isinstance(raw, list) else [])[:12]:
        if len(suggestions) == MAX_SUGGESTIONS:
            break
        try:
            suggestions.append(_normalise_suggestion(portfolio, view, item))
        except NoiError as exc:
            notes.append(f"A suggestion was dropped because it did not fit the data: {exc}")
    return output["answer"].strip(), suggestions, notes


def _normalise_suggestion(portfolio: Portfolio, view: View, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise NoiError("not an object")
    kind = item.get("kind")
    label = str(item.get("label") or "").strip()[:60]

    def given(name: str, empty: tuple[str, ...]) -> Any:
        value = item.get(name)
        return None if value in (None, "", 0, *empty) else value

    sector, region = given("sector", ("all", "none")), given("region", ("all", "none"))
    property_id = given("property_id", ("none",))
    if kind == "add_chart":
        spec = {"type": given("chart", ("none",)), "metric": given("metric", ("none",)),
                "dimension": given("dimension", ("none",)), "order": given("order", ()),
                "limit": given("limit", ()),
                "scope": {"property_id": property_id, "sector": None if property_id else sector,
                          "region": None if property_id else region}}
        widget = portfolio.normalise_widget({k: v for k, v in spec.items() if v is not None})
        if widget["type"] == "note":
            raise NoiError("a chart suggestion cannot be a note")
        data = portfolio.resolve(widget, view)
        return {"kind": kind, "label": label or f"Add: {data['title']}", "widget": widget, "data": data}
    if kind == "open_property":
        if property_id not in portfolio.by_id:
            raise NoiError(f"unknown property '{property_id}'")
        return {"kind": kind, "label": label or f"Open {portfolio.by_id[property_id]['name']}",
                "property_id": property_id}
    if kind == "set_view":
        change: dict[str, Any] = {}
        if sector:
            change["sectors"] = [sector]
        if region:
            change["regions"] = [region]
        if given("compare", ("keep", "none")):
            change["compare"] = item["compare"]
        if given("period_from", ("none",)):
            change["from"] = item["period_from"]
        if given("period_to", ("none",)):
            change["to"] = item["period_to"]
        if not change:
            raise NoiError("a filter suggestion that changes nothing")
        merged = {**portfolio.view_dict(view), "property_id": None, **change}
        portfolio.parse_view(merged)  # raises if the change is not valid
        return {"kind": kind, "label": label or "Apply these filters", "view": change}
    raise NoiError(f"unknown suggestion kind '{kind}'")


# --------------------------------------------------------------------------- offline simulator


def _money(value: float) -> str:
    sign, value = ("-" if value < 0 else ""), abs(value)
    if value >= 1_000_000:
        return f"{sign}£{value / 1_000_000:.2f}m"
    if value >= 1_000:
        return f"{sign}£{value / 1_000:.0f}k"
    return f"{sign}£{value:.0f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _suggest(kind: str, label: str, **fields: Any) -> dict[str, Any]:
    base = {"kind": kind, "label": label, "chart": "none", "metric": "none", "dimension": "none",
            "order": "desc", "limit": 0, "sector": "all", "region": "all", "property_id": "",
            "compare": "keep", "period_from": "", "period_to": ""}
    return {**base, **fields}


def simulate(payload: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Keyword-matched canned analysis. No model: it recognises a handful of question types and
    fills a template from the same figures a real model would be given."""
    q = payload["question"].lower()
    view = payload["view"]
    basis = view["compare"]
    basis_label = COMPARE_LABEL[basis].lower()
    span = f"{view['from']} to {view['to']}" if view["from"] != view["to"] else view["from"]
    props = [p for p in payload["properties_for_period"] if p["in_view"]] or payload["properties_for_period"]
    total = payload["in_view"]

    def var(p: dict[str, Any], metric: str = "noi") -> float:
        return p["actual"][metric] - p[basis][metric]

    def way(delta: float, cost: bool = False) -> str:
        return "favourable" if (delta < 0) == cost else "unfavourable"

    named = next((p for p in payload["properties_for_period"]
                  if p["name"].lower() in q or re.search(rf"\b{p['id'].lower()}\b", q)), None)
    if named is None:
        named = next((p for p in payload["properties_for_period"]
                      if any(len(w) > 4 and w in q for w in p["name"].lower().split()[:1])), None)
    opex_lines = [l for l in payload["dataset"]["lines"] if l["group"] == "opex"]
    line_hit = next((l for l in payload["dataset"]["lines"]
                     if any(w in q for w in l["label"].lower().replace(",", "").split() if len(w) > 5)), None)

    if named:
        p, d = named, var(named)
        def impact(line: dict[str, str]) -> float:  # effect of one line on NOI, actual less comparison
            move = p["actual"]["lines"][line["key"]] - p[basis]["lines"][line["key"]]
            return -move if line["group"] == "opex" else move

        driver = min(payload["dataset"]["lines"], key=impact) if d < 0 else max(payload["dataset"]["lines"], key=impact)
        answer = (f"{p['name']} ({p['sector'].lower()}, {p['city']}) made NOI of {_money(p['actual']['noi'])} "
                  f"for {span}, {_money(abs(d))} {way(d)} to {basis_label} of {_money(p[basis]['noi'])}. "
                  f"NOI margin was {_pct(p['actual']['noi_margin'])} and occupancy averaged "
                  f"{_pct(p['actual']['occupancy'])} against {_pct(p[basis]['occupancy'])}.\n\n"
                  f"The line that moved NOI most was {driver['label'].lower()}, worth {_money(abs(impact(driver)))} "
                  f"{way(impact(driver))}."
                  + (f"\n\nNote on file: {p['note']}" if p["note"] else ""))
        suggestions = [
            _suggest("open_property", f"Open {p['name']}", property_id=p["id"]),
            _suggest("add_chart", "Chart its NOI by month", chart="trend", metric="noi", property_id=p["id"]),
            _suggest("add_chart", "Bridge its NOI by line", chart="bridge", dimension="line", property_id=p["id"]),
        ]
    elif line_hit or re.search(r"expens|cost|opex|overspen|overrun", q):
        if line_hit and line_hit["group"] == "opex":
            key = line_hit["key"]
            d = total["actual"]["lines"][key] - total[basis]["lines"][key]
            by_prop = sorted(props, key=lambda p: p["actual"]["lines"][key] - p[basis]["lines"][key], reverse=True)
            top = by_prop[0]
            top_d = top["actual"]["lines"][key] - top[basis]["lines"][key]
            answer = (f"{line_hit['label']} came to {_money(total['actual']['lines'][key])} for {span}, "
                      f"{_money(abs(d))} {'over' if d > 0 else 'under'} {basis_label} ({way(d, cost=True)}). "
                      f"{top['name']} accounts for the largest share of that movement at {_money(abs(top_d))} "
                      f"{'over' if top_d > 0 else 'under'}."
                      + (f"\n\nNote on file for {top['name']}: {top['note']}" if top["note"] else ""))
            suggestions = [
                _suggest("add_chart", f"Chart {line_hit['label'].lower()} by month", chart="trend", metric=key),
                _suggest("open_property", f"Open {top['name']}", property_id=top["id"]),
            ]
        else:
            moves = sorted(((l, total["actual"]["lines"][l["key"]] - total[basis]["lines"][l["key"]])
                            for l in opex_lines), key=lambda x: x[1], reverse=True)
            d = total["actual"]["operating_expenses"] - total[basis]["operating_expenses"]
            answer = (f"Operating expenses were {_money(total['actual']['operating_expenses'])} for {span}, "
                      f"{_money(abs(d))} {'over' if d > 0 else 'under'} {basis_label} ({way(d, cost=True)}).\n\n"
                      "The biggest movements by line:\n"
                      + "\n".join(f"- {l['label']}: {_money(abs(m))} {'over' if m > 0 else 'under'}"
                                  for l, m in moves[:3]))
            suggestions = [
                _suggest("add_chart", "Chart expenses by line", chart="rank", dimension="opex_line"),
                _suggest("add_chart", "Bridge NOI by line", chart="bridge", dimension="line"),
            ]
    elif re.search(r"occupan|vacan|empty|let\b|lettings", q):
        ranked = sorted(props, key=lambda p: p["actual"]["occupancy"])
        answer = (f"Occupancy averaged {_pct(total['actual']['occupancy'])} for {span}, against "
                  f"{_pct(total[basis]['occupancy'])} in the {basis_label}.\n\nLowest occupancy:\n"
                  + "\n".join(f"- {p['name']}: {_pct(p['actual']['occupancy'])} "
                              f"({basis_label} {_pct(p[basis]['occupancy'])})" for p in ranked[:3])
                  + (f"\n\nNote on file for {ranked[0]['name']}: {ranked[0]['note']}" if ranked[0]["note"] else ""))

        suggestions = [
            _suggest("add_chart", "Rank occupancy by property", chart="rank", metric="occupancy",
                     dimension="property", order="asc"),
            _suggest("open_property", f"Open {ranked[0]['name']}", property_id=ranked[0]["id"]),
        ]
    elif re.search(r"trend|month|over time|season|run.?rate", q):
        series = payload["monthly_full_year"]["in_view"]["actual"]["noi"]
        months = payload["dataset"]["months"]
        hi, lo = max(range(12), key=series.__getitem__), min(range(12), key=series.__getitem__)
        answer = (f"Across the twelve months to {months[-1]}, monthly NOI for {view['showing'].lower()} ranged "
                  f"from {_money(series[lo])} in {months[lo]} to {_money(series[hi])} in {months[hi]}. "
                  f"The first three months averaged {_money(sum(series[:3]) / 3)} and the last three "
                  f"{_money(sum(series[-3:]) / 3)}.")
        suggestions = [
            _suggest("add_chart", "Chart NOI by month", chart="trend", metric="noi"),
            _suggest("add_chart", "Chart utilities by month", chart="trend", metric="utilities"),
        ]
    elif re.search(r"sector|region|segment|asset class", q):
        dim = "region" if "region" in q else "sector"
        groups = payload[f"{dim}s_for_period"]
        rows = sorted(groups.items(), key=lambda kv: kv[1]["actual"]["noi"], reverse=True)
        answer = (f"NOI by {dim} for {span}, whole portfolio, with the variance to {basis_label}:\n"
                  + "\n".join(f"- {name}: {_money(g['actual']['noi'])} "
                              f"({_money(abs(g['actual']['noi'] - g[basis]['noi']))} "
                              f"{way(g['actual']['noi'] - g[basis]['noi'])})" for name, g in rows))
        suggestions = [
            _suggest("add_chart", f"Rank NOI by {dim}", chart="rank", metric="noi", dimension=dim),
            _suggest("add_chart", f"Bridge NOI by {dim}", chart="bridge", dimension=dim),
        ]
    elif re.search(r"best|top|strong|highest|largest|outperform|ahead", q):
        ranked = sorted(props, key=var, reverse=True)
        answer = (f"Against {basis_label} for {span}, the properties furthest ahead on NOI:\n"
                  + "\n".join(f"- {p['name']}: {_money(p['actual']['noi'])}, {_money(abs(var(p)))} {way(var(p))}"
                              for p in ranked[:3])
                  + (f"\n\nNote on file for {ranked[0]['name']}: {ranked[0]['note']}" if ranked[0]["note"] else ""))
        suggestions = [
            _suggest("add_chart", "Rank NOI variance by property", chart="rank", metric="noi_variance",
                     dimension="property"),
            _suggest("open_property", f"Open {ranked[0]['name']}", property_id=ranked[0]["id"]),
        ]
    elif re.search(r"worst|under|below|behind|miss|weak|lowest|drag|problem|concern|attention|variance|budget", q):
        ranked = sorted(props, key=var)
        answer = (f"Against {basis_label} for {span}, the properties furthest behind on NOI:\n"
                  + "\n".join(f"- {p['name']}: {_money(p['actual']['noi'])}, {_money(abs(var(p)))} {way(var(p))}"
                              for p in ranked[:3])
                  + "".join(f"\n\nNote on file for {p['name']}: {p['note']}" for p in ranked[:2] if p["note"]))
        suggestions = [
            _suggest("add_chart", "Rank NOI variance by property", chart="rank", metric="noi_variance",
                     dimension="property", order="asc"),
            _suggest("add_chart", "Bridge NOI by property", chart="bridge", dimension="property"),
            _suggest("open_property", f"Open {ranked[0]['name']}", property_id=ranked[0]["id"]),
        ]
    else:
        d = total["actual"]["noi"] - total[basis]["noi"]
        ranked = sorted(props, key=var)
        answer = (f"NOI for {view['showing'].lower()} was {_money(total['actual']['noi'])} for {span}, "
                  f"{_money(abs(d))} {way(d)} to {basis_label} of {_money(total[basis]['noi'])}. Revenue was "
                  f"{_money(total['actual']['revenue'])} and operating expenses "
                  f"{_money(total['actual']['operating_expenses'])}, a margin of "
                  f"{_pct(total['actual']['noi_margin'])}.\n\n"
                  f"{ranked[0]['name']} is furthest behind ({_money(abs(var(ranked[0])))} {way(var(ranked[0]))}) "
                  f"and {ranked[-1]['name']} furthest ahead ({_money(abs(var(ranked[-1])))} "
                  f"{way(var(ranked[-1]))}).")
        suggestions = [
            _suggest("add_chart", "Bridge NOI by property", chart="bridge", dimension="property"),
            _suggest("add_chart", "Bridge NOI by line", chart="bridge", dimension="line"),
        ]
    return {"answer": answer, "suggestions": suggestions}
