"""NOI figures: one place where every number on the dashboard is computed.

The browser never adds anything up. It sends a *view* (period, comparison
basis, filters, optional property) and a list of *widget specs*; this module
turns each spec into render-ready data. The AI assistant is given figures
produced by the same functions and can only *propose* widget specs, so a chart
it suggests is drawn from the dataset, not from numbers the model wrote.

Amounts are whole pounds (ints). Revenue lines are positive (vacancy is
negative), cost lines are positive, NOI = revenue - operating expenses.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASES = ("actual", "budget", "prior")
COMPARE_LABEL = {"budget": "Budget", "prior": "Prior year"}
# metric -> (label, number format, True when lower is better)
BASE_METRICS: dict[str, tuple[str, str, bool]] = {
    "noi": ("Net operating income", "money", False),
    "revenue": ("Revenue", "money", False),
    "opex": ("Operating expenses", "money", True),
    "margin": ("NOI margin", "pct", False),
    "occupancy": ("Occupancy", "pct", False),
    "noi_psf": ("NOI per sq ft", "psf", False),
}
RANK_METRICS = (*BASE_METRICS, "noi_variance")
TREND_BASE_METRICS = ("noi", "revenue", "opex", "margin", "occupancy")
RANK_DIMENSIONS = ("property", "sector", "region", "opex_line", "revenue_line")
BRIDGE_DIMENSIONS = ("line", "property", "sector", "region")
WIDGET_TYPES = ("kpi", "trend", "rank", "bridge", "statement", "table", "note")
DIMENSION_LABEL = {"property": "property", "sector": "sector", "region": "region",
                   "opex_line": "expense line", "revenue_line": "revenue line", "line": "line item"}
MAX_BRIDGE_STEPS = 8


class NoiError(ValueError):
    """A view or widget spec that does not fit the dataset. The message is safe to show."""


@dataclass(frozen=True)
class View:
    start: int
    end: int
    compare: str
    sectors: tuple[str, ...]
    regions: tuple[str, ...]
    property_id: str | None

    @property
    def idx(self) -> range:
        return range(self.start, self.end + 1)


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


class Portfolio:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.name: str = raw["portfolio"]
        self.currency: str = raw["currency"]
        self.months: list[str] = raw["months"]
        self.lines: list[dict[str, str]] = raw["lines"]
        self.line_label = {l["key"]: l["label"] for l in self.lines}
        self.revenue_keys = [l["key"] for l in self.lines if l["group"] == "revenue"]
        self.opex_keys = [l["key"] for l in self.lines if l["group"] == "opex"]
        self.properties: list[dict[str, Any]] = raw["properties"]
        self.by_id = {p["id"]: p for p in self.properties}
        self.sectors = sorted({p["sector"] for p in self.properties})
        self.regions = sorted({p["region"] for p in self.properties})
        n = len(self.months)
        for p in self.properties:
            for key in self.line_label:
                for basis in BASES:
                    series = p["lines"][key][basis]
                    if len(series) != n or not all(isinstance(v, int) and not isinstance(v, bool) for v in series):
                        raise ValueError(f"{p['id']}.{key}.{basis} must be {n} whole-pound integers")
            for basis in BASES:
                if len(p["occupancy"][basis]) != n:
                    raise ValueError(f"{p['id']}.occupancy.{basis} must have {n} values")

    @classmethod
    def load(cls, path: Path) -> "Portfolio":
        return cls(json.loads(path.read_text()))

    # ------------------------------------------------------------------ views and scope

    def parse_view(self, raw: dict[str, Any] | None) -> View:
        raw = raw or {}
        if not isinstance(raw, dict):
            raise NoiError("The view must be an object.")
        start_m, end_m = raw.get("from") or self.months[0], raw.get("to") or self.months[-1]
        if start_m not in self.months or end_m not in self.months:
            raise NoiError(f"The period must fall between {self.months[0]} and {self.months[-1]}.")
        start, end = self.months.index(start_m), self.months.index(end_m)
        if start > end:
            raise NoiError("The period start is after its end.")
        compare = raw.get("compare") or "budget"
        if compare not in COMPARE_LABEL:
            raise NoiError("Compare against 'budget' or 'prior'.")
        sectors, regions = self._names(raw.get("sectors"), self.sectors, "sector"), \
            self._names(raw.get("regions"), self.regions, "region")
        property_id = raw.get("property_id") or None
        if property_id is not None and property_id not in self.by_id:
            raise NoiError(f"Unknown property '{property_id}'.")
        return View(start, end, compare, sectors, regions, property_id)

    @staticmethod
    def _names(value: Any, allowed: list[str], what: str) -> tuple[str, ...]:
        if value in (None, "", []):
            return ()
        if not isinstance(value, list) or any(v not in allowed for v in value):
            raise NoiError(f"Unknown {what} in {value!r}. Known: {', '.join(allowed)}.")
        return tuple(v for v in allowed if v in value)

    def view_dict(self, view: View) -> dict[str, Any]:
        return {"from": self.months[view.start], "to": self.months[view.end], "compare": view.compare,
                "sectors": list(view.sectors), "regions": list(view.regions), "property_id": view.property_id,
                "compare_label": COMPARE_LABEL[view.compare], "months_in_period": view.end - view.start + 1}

    def scope(self, view: View, scope: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str]:
        """Properties a widget covers, and a label for them. A widget's own scope wins over the view."""
        scope = scope or {}
        property_id = scope.get("property_id") or (None if scope.get("sector") or scope.get("region")
                                                    else view.property_id)
        if property_id:
            return [self.by_id[property_id]], self.by_id[property_id]["name"]
        sectors = (scope["sector"],) if scope.get("sector") else view.sectors
        regions = (scope["region"],) if scope.get("region") else view.regions
        props = [p for p in self.properties
                 if (not sectors or p["sector"] in sectors) and (not regions or p["region"] in regions)]
        parts = [", ".join(sectors)] if sectors else []
        parts += [", ".join(regions)] if regions else []
        return props, " · ".join(parts) if parts else "All properties"

    # ------------------------------------------------------------------ measures

    def measure(self, props: list[dict[str, Any]], idx: range, basis: str) -> dict[str, Any]:
        lines = {k: sum(p["lines"][k][basis][i] for p in props for i in idx) for k in self.line_label}
        revenue = sum(lines[k] for k in self.revenue_keys)
        opex = sum(lines[k] for k in self.opex_keys)
        sqft = sum(p["sqft"] for p in props)
        let = sum(p["occupancy"][basis][i] * p["sqft"] for p in props for i in idx)
        noi = revenue - opex
        return {"revenue": revenue, "opex": opex, "noi": noi, "margin": _ratio(noi, revenue),
                "occupancy": _ratio(let, sqft * len(idx)), "sqft": sqft,
                "noi_psf": round(noi / sqft, 2) if sqft else None, "lines": lines}

    def metric_info(self, metric: str) -> tuple[str, str, bool]:
        if metric in BASE_METRICS:
            return BASE_METRICS[metric]
        if metric in self.line_label:
            return self.line_label[metric], "money", metric in self.opex_keys
        raise NoiError(f"Unknown metric '{metric}'.")

    @staticmethod
    def _value(measure: dict[str, Any], metric: str) -> Any:
        return measure[metric] if metric in measure else measure["lines"][metric]

    def compare_values(self, metric: str, actual: Any, compare: Any) -> dict[str, Any]:
        """Actual against comparison for one metric, with the direction that counts as good."""
        _, fmt, lower_is_better = self.metric_info(metric)
        if actual is None or compare is None:
            return {"value": actual, "compare": compare, "delta": None, "delta_pct": None, "favourable": None}
        delta = round(actual - compare, 4) if fmt != "money" else actual - compare
        favourable = None if delta == 0 else (delta < 0) == lower_is_better
        return {"value": actual, "compare": compare, "delta": delta,
                "delta_pct": _ratio(delta, abs(compare)) if fmt != "pct" else None, "favourable": favourable}

    def _pair(self, props: list[dict[str, Any]], view: View, idx: range | None = None) -> tuple[dict, dict]:
        idx = view.idx if idx is None else idx
        return self.measure(props, idx, "actual"), self.measure(props, idx, view.compare)

    # ------------------------------------------------------------------ widget specs

    def normalise_widget(self, spec: Any) -> dict[str, Any]:
        """Validate a widget spec and fill defaults. Raises ``NoiError``."""
        if not isinstance(spec, dict) or spec.get("type") not in WIDGET_TYPES:
            raise NoiError(f"A widget needs a type: one of {', '.join(WIDGET_TYPES)}.")
        kind = spec["type"]
        out: dict[str, Any] = {"type": kind}
        title = spec.get("title")
        if isinstance(title, str) and title.strip():
            out["title"] = title.strip()[:90]
        raw_scope = spec.get("scope") or {}
        if not isinstance(raw_scope, dict):
            raise NoiError("A widget scope must be an object.")
        scope = {}
        if raw_scope.get("property_id"):
            if raw_scope["property_id"] not in self.by_id:
                raise NoiError(f"Unknown property '{raw_scope['property_id']}'.")
            scope["property_id"] = raw_scope["property_id"]
        if raw_scope.get("sector"):
            if raw_scope["sector"] not in self.sectors:
                raise NoiError(f"Unknown sector '{raw_scope['sector']}'.")
            scope["sector"] = raw_scope["sector"]
        if raw_scope.get("region"):
            if raw_scope["region"] not in self.regions:
                raise NoiError(f"Unknown region '{raw_scope['region']}'.")
            scope["region"] = raw_scope["region"]
        if scope:
            out["scope"] = scope

        if kind == "note":
            text = spec.get("text")
            if not isinstance(text, str) or not text.strip():
                raise NoiError("A note needs some text.")
            out["text"] = text.strip()[:4000]
        elif kind == "trend":
            metric = spec.get("metric") or "noi"
            if metric not in TREND_BASE_METRICS and metric not in self.line_label:
                raise NoiError(f"A trend cannot plot '{metric}'.")
            out["metric"] = metric
        elif kind == "rank":
            dimension = spec.get("dimension") or "property"
            if dimension not in RANK_DIMENSIONS:
                raise NoiError(f"Cannot rank by '{dimension}'.")
            if dimension == "property" and scope.get("property_id"):
                raise NoiError("A ranking by property cannot be limited to one property.")
            out["dimension"] = dimension
            if dimension not in ("opex_line", "revenue_line"):
                metric = spec.get("metric") or "noi"
                if metric not in RANK_METRICS:
                    raise NoiError(f"Cannot rank on '{metric}'.")
                out["metric"] = metric
            order = spec.get("order") or "desc"
            if order not in ("desc", "asc"):
                raise NoiError("Order is 'desc' or 'asc'.")
            out["order"] = order
            limit = spec.get("limit") or 0
            if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 50:
                raise NoiError("Limit must be a whole number from 0 (all) to 50.")
            out["limit"] = limit
        elif kind == "bridge":
            dimension = spec.get("dimension") or "line"
            if dimension not in BRIDGE_DIMENSIONS:
                raise NoiError(f"Cannot bridge by '{dimension}'.")
            if dimension == "property" and scope.get("property_id"):
                raise NoiError("A bridge by property cannot be limited to one property.")
            out["dimension"] = dimension
        return out

    def resolve(self, spec: dict[str, Any], view: View) -> dict[str, Any]:
        """Render-ready data for one normalised widget spec."""
        if spec["type"] == "note":
            return {"title": spec.get("title") or "Note", "text": spec["text"]}
        props, label = self.scope(view, spec.get("scope"))
        if not props:
            raise NoiError("No properties match the current filters.")
        data = getattr(self, f"_resolve_{spec['type']}")(spec, view, props)
        data["scope_label"] = label
        data["compare_label"] = COMPARE_LABEL[view.compare]
        data["single_property"] = props[0]["id"] if len(props) == 1 else None
        return data

    def _resolve_kpi(self, spec: dict, view: View, props: list[dict]) -> dict[str, Any]:
        actual, compare = self._pair(props, view)
        monthly = [self.measure(props, range(i, i + 1), "actual") for i in view.idx]
        tiles = []
        for metric, (label, fmt, _) in BASE_METRICS.items():
            tiles.append({"metric": metric, "label": label, "format": fmt,
                          **self.compare_values(metric, actual[metric], compare[metric]),
                          "spark": [m[metric] for m in monthly]})
        return {"title": "Headline figures", "tiles": tiles}

    def _resolve_trend(self, spec: dict, view: View, props: list[dict]) -> dict[str, Any]:
        metric = spec["metric"]
        label, fmt, _ = self.metric_info(metric)
        series = {basis: [self._value(self.measure(props, range(i, i + 1), basis), metric)
                          for i in range(len(self.months))] for basis in ("actual", view.compare)}
        return {"title": f"{label} by month", "metric": metric, "format": fmt, "months": self.months,
                "period": [view.start, view.end],
                "series": [{"key": "actual", "name": "Actual", "values": series["actual"]},
                           {"key": "compare", "name": COMPARE_LABEL[view.compare], "values": series[view.compare]}]}

    def _groups(self, dimension: str, props: list[dict]) -> list[tuple[str, str, list[dict]]]:
        if dimension == "property":
            return [(p["id"], p["name"], [p]) for p in props]
        names = sorted({p[dimension] for p in props})
        return [(n, n, [p for p in props if p[dimension] == n]) for n in names]

    def _resolve_rank(self, spec: dict, view: View, props: list[dict]) -> dict[str, Any]:
        dimension = spec["dimension"]
        rows: list[dict[str, Any]] = []
        if dimension in ("opex_line", "revenue_line"):
            actual, compare = self._pair(props, view)
            keys = self.opex_keys if dimension == "opex_line" else self.revenue_keys
            fmt, title = "money", ("Operating expenses by line" if dimension == "opex_line" else "Revenue by line")
            for key in keys:
                rows.append({"key": key, "label": self.line_label[key],
                             **self.compare_values(key, actual["lines"][key], compare["lines"][key])})
        else:
            metric = spec["metric"]
            variance = metric == "noi_variance"
            label, fmt, _ = ("NOI variance", "money", False) if variance else self.metric_info(metric)
            title = f"{label} by {DIMENSION_LABEL[dimension]}"
            for key, name, members in self._groups(dimension, props):
                actual, compare = self._pair(members, view)
                row = {"key": key, "label": name}
                if variance:
                    cv = self.compare_values("noi", actual["noi"], compare["noi"])
                    row.update({"value": cv["delta"], "compare": None, "delta": None,
                                "delta_pct": cv["delta_pct"], "favourable": cv["favourable"]})
                else:
                    row.update(self.compare_values(metric, actual[metric], compare[metric]))
                if dimension == "property":
                    row.update({"property_id": key, "detail": f"{members[0]['sector']} · {members[0]['city']}"})
                rows.append(row)
        rows = [r for r in rows if r["value"] is not None]
        rows.sort(key=lambda r: r["value"], reverse=spec["order"] == "desc")
        total = len(rows)
        if spec["limit"]:
            rows = rows[: spec["limit"]]
        return {"title": title, "format": fmt, "dimension": dimension, "rows": rows, "total_rows": total,
                "diverging": dimension not in ("opex_line", "revenue_line") and spec["metric"] == "noi_variance"}

    def _resolve_bridge(self, spec: dict, view: View, props: list[dict]) -> dict[str, Any]:
        dimension = spec["dimension"]
        actual, compare = self._pair(props, view)
        steps: list[dict[str, Any]] = []
        if dimension == "line":
            for key in self.line_label:
                delta = actual["lines"][key] - compare["lines"][key]
                steps.append({"key": key, "label": self.line_label[key],
                              "delta": -delta if key in self.opex_keys else delta})
        else:
            for key, name, members in self._groups(dimension, props):
                a, c = self._pair(members, view)
                step = {"key": key, "label": name, "delta": a["noi"] - c["noi"]}
                if dimension == "property":
                    step["property_id"] = key
                steps.append(step)
        steps = [s for s in steps if s["delta"] != 0]
        if len(steps) > MAX_BRIDGE_STEPS:
            steps.sort(key=lambda s: abs(s["delta"]), reverse=True)
            rest = steps[MAX_BRIDGE_STEPS - 1:]
            steps = steps[: MAX_BRIDGE_STEPS - 1]
            steps.append({"key": "other", "label": f"Other ({len(rest)})", "delta": sum(s["delta"] for s in rest)})
        if dimension != "line":
            steps[: MAX_BRIDGE_STEPS - 1] = sorted(steps[: MAX_BRIDGE_STEPS - 1], key=lambda s: s["delta"])
        for step in steps:
            step["favourable"] = step["delta"] > 0
        return {"title": f"NOI: {COMPARE_LABEL[view.compare].lower()} to actual, by {DIMENSION_LABEL[dimension]}",
                "format": "money", "dimension": dimension,
                "start": {"label": COMPARE_LABEL[view.compare], "value": compare["noi"]},
                "steps": steps, "end": {"label": "Actual", "value": actual["noi"]}}

    def _resolve_statement(self, spec: dict, view: View, props: list[dict]) -> dict[str, Any]:
        actual, compare = self._pair(props, view)
        rows: list[dict[str, Any]] = []

        def add(kind: str, key: str, label: str, a: Any, c: Any) -> None:
            rows.append({"kind": kind, "key": key, "label": label, **self.compare_values(key, a, c),
                         "format": self.metric_info(key)[1]})

        for key in self.revenue_keys:
            add("line", key, self.line_label[key], actual["lines"][key], compare["lines"][key])
        add("subtotal", "revenue", "Effective gross income", actual["revenue"], compare["revenue"])
        for key in self.opex_keys:
            add("line", key, self.line_label[key], actual["lines"][key], compare["lines"][key])
        add("subtotal", "opex", "Total operating expenses", actual["opex"], compare["opex"])
        add("total", "noi", "Net operating income", actual["noi"], compare["noi"])
        add("ratio", "margin", "NOI margin", actual["margin"], compare["margin"])
        add("ratio", "occupancy", "Occupancy", actual["occupancy"], compare["occupancy"])
        return {"title": "NOI statement", "rows": rows}

    def _resolve_table(self, spec: dict, view: View, props: list[dict]) -> dict[str, Any]:
        rows = []
        for p in props:
            actual, compare = self._pair([p], view)
            noi = self.compare_values("noi", actual["noi"], compare["noi"])
            rows.append({"property_id": p["id"], "name": p["name"], "sector": p["sector"], "region": p["region"],
                         "city": p["city"], "sqft": p["sqft"], "occupancy": actual["occupancy"],
                         "revenue": actual["revenue"], "opex": actual["opex"], "noi": actual["noi"],
                         "noi_compare": compare["noi"], "noi_delta": noi["delta"],
                         "noi_delta_pct": noi["delta_pct"], "favourable": noi["favourable"],
                         "margin": actual["margin"], "noi_psf": actual["noi_psf"],
                         "spark": [self.measure([p], range(i, i + 1), "actual")["noi"] for i in view.idx]})
        rows.sort(key=lambda r: r["noi"], reverse=True)
        actual, compare = self._pair(props, view)
        noi = self.compare_values("noi", actual["noi"], compare["noi"])
        total = {"name": "Total", "sqft": actual["sqft"], "occupancy": actual["occupancy"],
                 "revenue": actual["revenue"], "opex": actual["opex"], "noi": actual["noi"],
                 "noi_compare": compare["noi"], "noi_delta": noi["delta"], "noi_delta_pct": noi["delta_pct"],
                 "favourable": noi["favourable"], "margin": actual["margin"], "noi_psf": actual["noi_psf"]}
        return {"title": "Properties", "rows": rows, "total": total}

    # ------------------------------------------------------------------ page metadata

    def meta(self) -> dict[str, Any]:
        return {
            "portfolio": self.name, "fictional": bool(self.raw.get("fictional")), "currency": self.currency,
            "months": self.months, "sectors": self.sectors, "regions": self.regions,
            "lines": self.lines, "prior_basis": self.raw.get("prior_basis"),
            "properties": [{k: p[k] for k in ("id", "name", "sector", "region", "city", "sqft", "note")}
                           for p in self.properties],
            "catalogue": {
                "trend_metrics": [{"key": k, "label": self.metric_info(k)[0]}
                                  for k in (*TREND_BASE_METRICS, *self.line_label)],
                "rank_metrics": [{"key": k, "label": "NOI variance" if k == "noi_variance" else BASE_METRICS[k][0]}
                                 for k in RANK_METRICS],
                "rank_dimensions": [{"key": k, "label": DIMENSION_LABEL[k]} for k in RANK_DIMENSIONS],
                "bridge_dimensions": [{"key": k, "label": DIMENSION_LABEL[k]} for k in BRIDGE_DIMENSIONS],
            },
        }
