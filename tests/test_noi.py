"""NOI dashboard: the figures, the widget contract, and what the assistant may and may not do."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hub.config import Settings
from hub.main import create_app
from hub.noi import chat
from hub.noi.metrics import NoiError, Portfolio
from hub.providers.base import ProviderRateLimited

from .conftest import ScriptedProvider

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "noi_portfolio_2026-10.json"


@pytest.fixture(scope="module")
def pf() -> Portfolio:
    return Portfolio.load(FIXTURE)


def raw_total(pf: Portfolio, basis: str, months: range, props=None) -> int:
    """NOI added up straight from the fixture, independently of ``measure``."""
    total = 0
    for p in props or pf.properties:
        for line in pf.lines:
            amount = sum(p["lines"][line["key"]][basis][i] for i in months)
            total += amount if line["group"] == "revenue" else -amount
    return total


def suggestion(**fields) -> dict:
    return chat._suggest(fields.pop("kind", "add_chart"), fields.pop("label", "x"), **fields)


# --------------------------------------------------------------------------- figures


def test_noi_is_revenue_less_expenses_and_matches_the_raw_fixture(pf):
    view = pf.parse_view({})
    m = pf.measure(pf.properties, view.idx, "actual")
    assert m["noi"] == m["revenue"] - m["opex"] == raw_total(pf, "actual", range(12))
    assert m["margin"] == round(m["noi"] / m["revenue"], 4)


def test_properties_sum_to_the_portfolio_for_every_basis_and_period(pf):
    for months in (range(12), range(3, 7), range(11, 12)):
        for basis in ("actual", "budget", "prior"):
            whole = pf.measure(pf.properties, months, basis)["noi"]
            assert whole == sum(pf.measure([p], months, basis)["noi"] for p in pf.properties)


def test_period_and_filters_narrow_the_figures(pf):
    view = pf.parse_view({"from": "2026-08", "to": "2026-10", "sectors": ["Retail"], "compare": "prior"})
    props, label = pf.scope(view)
    assert {p["sector"] for p in props} == {"Retail"} and label == "Retail"
    data = pf.resolve(pf.normalise_widget({"type": "kpi"}), view)
    noi = next(t for t in data["tiles"] if t["metric"] == "noi")
    assert noi["value"] == raw_total(pf, "actual", range(9, 12), props)
    assert noi["compare"] == raw_total(pf, "prior", range(9, 12), props)
    assert len(noi["spark"]) == 3


def test_a_cost_overrun_is_unfavourable_and_a_revenue_gain_favourable(pf):
    assert pf.compare_values("opex", 110, 100)["favourable"] is False
    assert pf.compare_values("utilities", 90, 100)["favourable"] is True
    assert pf.compare_values("noi", 110, 100)["favourable"] is True
    assert pf.compare_values("vacancy", -120, -100)["favourable"] is False  # more vacancy loss
    assert pf.compare_values("noi", 100, 100)["favourable"] is None


@pytest.mark.parametrize("dimension", ["line", "property", "sector", "region"])
def test_bridge_steps_reconcile_start_to_end(pf, dimension):
    view = pf.parse_view({"from": "2026-02", "to": "2026-09"})
    data = pf.resolve(pf.normalise_widget({"type": "bridge", "dimension": dimension}), view)
    assert data["start"]["value"] + sum(s["delta"] for s in data["steps"]) == data["end"]["value"]
    assert len(data["steps"]) <= 8


def test_statement_subtotals_foot(pf):
    rows = {r["key"]: r for r in pf.resolve({"type": "statement"}, pf.parse_view({}))["rows"]}
    for column in ("value", "compare"):
        assert rows["revenue"][column] == sum(rows[k][column] for k in pf.revenue_keys)
        assert rows["opex"][column] == sum(rows[k][column] for k in pf.opex_keys)
        assert rows["noi"][column] == rows["revenue"][column] - rows["opex"][column]


def test_ranking_orders_limits_and_carries_drill_targets(pf):
    spec = pf.normalise_widget({"type": "rank", "metric": "noi_variance", "order": "asc", "limit": 3})
    data = pf.resolve(spec, pf.parse_view({}))
    values = [r["value"] for r in data["rows"]]
    assert values == sorted(values) and len(values) == 3 and data["total_rows"] == 12
    assert data["rows"][0]["property_id"] == "P11" and data["rows"][0]["favourable"] is False
    assert data["diverging"] is True


def test_property_drill_down_and_widget_scope_precedence(pf):
    view = pf.parse_view({"property_id": "P05"})
    table = pf.resolve({"type": "table"}, view)
    assert [r["property_id"] for r in table["rows"]] == ["P05"] and table["single_property"] == "P05"
    pinned = pf.resolve(pf.normalise_widget({"type": "trend", "scope": {"sector": "Office"}}), view)
    assert pinned["scope_label"] == "Office"
    assert len(pinned["series"][0]["values"]) == 12 and pinned["period"] == [0, 11]


@pytest.mark.parametrize("spec", [
    {"type": "pie"}, {"type": "trend", "metric": "ebitda"}, {"type": "rank", "dimension": "tenant"},
    {"type": "rank", "limit": 500}, {"type": "bridge", "scope": {"property_id": "P99"}},
    {"type": "rank", "dimension": "property", "scope": {"property_id": "P01"}}, {"type": "note", "text": "  "},
])
def test_bad_widget_specs_are_refused(pf, spec):
    with pytest.raises(NoiError):
        pf.normalise_widget(spec)


@pytest.mark.parametrize("view", [
    {"from": "2024-01"}, {"from": "2026-05", "to": "2026-02"}, {"compare": "forecast"},
    {"sectors": ["Hotels"]}, {"property_id": "P99"},
])
def test_bad_views_are_refused(pf, view):
    with pytest.raises(NoiError):
        pf.parse_view(view)


# --------------------------------------------------------------------------- assistant


def test_the_assistant_is_given_figures_computed_by_the_dashboard(pf):
    view = pf.parse_view({"sectors": ["Industrial"]})
    payload = chat.build_payload(pf, view, "How are we doing?", [])
    props, _ = pf.scope(view)
    assert payload["in_view"]["actual"]["noi"] == pf.measure(props, view.idx, "actual")["noi"]
    assert [p["id"] for p in payload["properties_for_period"] if p["in_view"]] == [p["id"] for p in props]
    assert sum(payload["monthly_full_year"]["in_view"]["budget"]["noi"]) == raw_total(pf, "budget", range(12), props)


def test_suggested_charts_are_validated_and_drawn_from_the_dataset(pf):
    view = pf.parse_view({})
    answer, suggestions, notes = chat.normalise_answer(pf, view, {"answer": " Fine. ", "suggestions": [
        suggestion(chart="rank", metric="occupancy", dimension="property", order="asc", limit=3),
        suggestion(chart="trend", metric="utilities", property_id="P01"),
        suggestion(chart="trend", metric="made_up_metric"),
        suggestion(kind="open_property", property_id="P99"),
        suggestion(kind="set_view", sector="Retail", compare="prior"),
    ]})
    assert answer == "Fine." and [s["kind"] for s in suggestions] == ["add_chart", "add_chart", "set_view"]
    assert suggestions[0]["widget"] == {"type": "rank", "dimension": "property", "metric": "occupancy",
                                        "order": "asc", "limit": 3}
    assert suggestions[0]["data"] == pf.resolve(suggestions[0]["widget"], view)
    assert suggestions[1]["widget"]["scope"] == {"property_id": "P01"}
    assert suggestions[2]["view"] == {"sectors": ["Retail"], "compare": "prior"}
    assert len(notes) == 2 and all("dropped" in n for n in notes)


def test_an_answer_with_no_text_is_refused(pf):
    with pytest.raises(NoiError):
        chat.normalise_answer(pf, pf.parse_view({}), {"answer": "  ", "suggestions": []})


def test_output_schema_is_strict_and_lists_only_real_names(pf):
    schema = chat.output_schema(pf)
    item = schema["properties"]["suggestions"]["items"]
    assert schema["additionalProperties"] is False and item["additionalProperties"] is False
    assert sorted(item["required"]) == sorted(item["properties"])
    assert item["properties"]["sector"]["enum"] == [*pf.sectors, "all"]


@pytest.mark.parametrize("question", [
    "How are we doing?", "Which properties are behind budget?", "Tell me about Castlegate Arcade",
    "Why are utilities over?", "What is driving operating expenses?", "Where is occupancy weakest?",
    "NOI by sector", "Show the monthly trend", "Who are the top performers?",
])
def test_the_offline_simulator_always_returns_a_usable_answer(pf, question):
    for raw_view in ({}, {"compare": "prior", "from": "2026-05", "to": "2026-07"}, {"property_id": "P11"}):
        view = pf.parse_view(raw_view)
        output = chat.simulate(chat.build_payload(pf, view, question, []), {})
        answer, suggestions, notes = chat.normalise_answer(pf, view, output)
        assert answer and suggestions and notes == []


# --------------------------------------------------------------------------- API


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "noi.sqlite3"), demo_latency_seconds=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.test") as http:
        http.app = app
        yield http


async def test_meta_and_page_are_served(client):
    meta = (await client.get("/api/noi/meta")).json()
    assert meta["fictional"] is True and len(meta["properties"]) == 12 and len(meta["months"]) == 12
    assert {p["id"]: p["available"] for p in meta["providers"]} == {"claude": False, "demo": True, "cowork": False}
    page = await client.get("/noi")
    assert page.status_code == 200 and "/static/noi.js" in page.text


async def test_render_isolates_a_bad_widget_and_rejects_a_bad_view(client):
    ok = await client.post("/api/noi/render", json={"view": {}, "widgets": [{"type": "kpi"}, {"type": "pie"}]})
    widgets = ok.json()["widgets"]
    assert ok.status_code == 200 and "data" in widgets[0] and "error" in widgets[1]
    bad = await client.post("/api/noi/render", json={"view": {"compare": "forecast"}, "widgets": []})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "NOI_BAD_REQUEST"


async def test_ask_with_the_demo_provider_is_labelled_simulated_and_audited(client):
    response = await client.post("/api/noi/ask", json={"question": "Which properties are behind budget?",
                                                       "provider": "demo", "view": {}})
    body = response.json()
    assert response.status_code == 200 and "Mill Race Apartments" in body["answer"]
    assert body["provenance"]["execution_mode"] == "simulated"
    assert any(s["kind"] == "add_chart" and s["data"]["rows"] for s in body["suggestions"])
    audit = (await client.get("/api/audit")).json()
    events = [e["event"] for e in audit["entries"]]
    assert "noi.question_asked" in events and "noi.question_answered" in events and audit["chain"]["intact"]


async def test_ask_refuses_unavailable_and_unknown_providers(client):
    for provider, status in (("claude", 409), ("cowork", 409), ("nope", 400)):
        response = await client.post("/api/noi/ask", json={"question": "hi", "provider": provider, "view": {}})
        assert response.status_code == status


async def test_ask_sends_the_real_provider_the_schema_and_figures_and_maps_failures(tmp_path):
    good = ScriptedProvider(output={"answer": "NOI was fine.", "suggestions": [
        suggestion(chart="bridge", dimension="sector")]})
    app = create_app(Settings(db_path=str(tmp_path / "a.sqlite3")), providers=[good])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.test") as http:
        body = (await http.post("/api/noi/ask", json={
            "question": "And retail?", "provider": "scripted", "view": {"sectors": ["Retail"]},
            "history": [{"question": "How are we doing?", "answer": "Behind budget."}]})).json()
    task = good.calls[0]
    assert task.workflow == "noi_chat" and task.request == chat.CLOSING_REQUEST and task.options == {"effort": "medium"}
    assert task.payload["question"] == "And retail?" and task.payload["view"]["sectors"] == ["Retail"]
    assert task.payload["earlier_in_this_conversation"] == [{"question": "How are we doing?", "answer": "Behind budget."}]
    assert task.output_schema["required"] == ["answer", "suggestions"]
    assert body["provenance"] == {"provider": "scripted", "label": "Scripted", "execution_mode": "real_ai",
                                  "model": "scripted-1"}
    assert body["suggestions"][0]["data"]["scope_label"] == "Retail"

    for provider, status, code in (
            (ScriptedProvider(error=ProviderRateLimited("Slow down.")), 502, "PROVIDER_RATE_LIMITED"),
            (ScriptedProvider(output={"answer": ""}), 502, "PROVIDER_OUTPUT_INVALID")):
        app = create_app(Settings(db_path=str(tmp_path / f"{code}.sqlite3")), providers=[provider])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.test") as http:
            response = await http.post("/api/noi/ask", json={"question": "x", "provider": "scripted", "view": {}})
        assert response.status_code == status and response.json()["error"]["code"] == code


def test_the_committed_fixture_matches_its_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_noi_fixture", FIXTURE.parent.parent / "scripts" / "build_noi_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == json.loads(FIXTURE.read_text())
