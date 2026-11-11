"""Regenerate the fictional NOI portfolio fixture.

    python scripts/build_noi_fixture.py

Deterministic (fixed seed), so re-running it reproduces the committed file.
Every property, tenant event and number is invented. A few properties carry a
deliberate story (a lost anchor tenant, a roof repair, a rent review, a winter
utilities overrun) so the dashboard has something real to find.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "noi_portfolio_2026-10.json"
MONTHS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04",
          "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]

LINES = [
    {"key": "base_rent", "label": "Gross potential rent", "group": "revenue"},
    {"key": "vacancy", "label": "Vacancy and credit loss", "group": "revenue"},
    {"key": "recoveries", "label": "Service charge recoveries", "group": "revenue"},
    {"key": "other_income", "label": "Other income", "group": "revenue"},
    {"key": "management", "label": "Property management", "group": "opex"},
    {"key": "repairs", "label": "Repairs and maintenance", "group": "opex"},
    {"key": "utilities", "label": "Utilities", "group": "opex"},
    {"key": "insurance", "label": "Insurance", "group": "opex"},
    {"key": "taxes", "label": "Property taxes and rates", "group": "opex"},
    {"key": "site_services", "label": "Cleaning, security and grounds", "group": "opex"},
]

# sector: rent psf/yr, recoveries psf, other income psf, then opex psf by line
SECTORS = {
    "Office": dict(rent=46.0, recoveries=7.5, other=1.6, management=1.9, repairs=2.6, utilities=3.4,
                   insurance=0.7, taxes=4.2, site_services=3.1),
    "Retail": dict(rent=31.0, recoveries=5.2, other=1.9, management=1.4, repairs=1.9, utilities=1.8,
                   insurance=0.6, taxes=3.3, site_services=2.6),
    "Industrial": dict(rent=9.8, recoveries=1.1, other=0.2, management=0.35, repairs=0.5, utilities=0.3,
                       insurance=0.25, taxes=0.7, site_services=0.35),
    "Residential": dict(rent=27.0, recoveries=0.0, other=1.3, management=2.3, repairs=2.9, utilities=1.5,
                        insurance=0.6, taxes=1.0, site_services=1.9),
}
REGION_RENT = {"London": 1.55, "South West": 0.95, "North West": 0.9, "Midlands": 0.88, "Scotland": 0.92}
# share of annual utilities cost by month, Nov..Oct (heating-led)
UTILITY_SEASON = [0.105, 0.125, 0.13, 0.115, 0.095, 0.075, 0.06, 0.05, 0.05, 0.055, 0.065, 0.075]

PROPERTIES = [
    ("P01", "Harbourgate House", "Office", "London", "London", 142_000, 0.94),
    ("P02", "Kestrel Court", "Office", "North West", "Manchester", 86_000, 0.91),
    ("P03", "Alder Wharf", "Office", "South West", "Bristol", 64_000, 0.89),
    ("P04", "Meridian Retail Park", "Retail", "Midlands", "Birmingham", 210_000, 0.95),
    ("P05", "Castlegate Arcade", "Retail", "Scotland", "Edinburgh", 58_000, 0.93),
    ("P06", "Quayside Shopping", "Retail", "North West", "Liverpool", 95_000, 0.90),
    ("P07", "Northfield Logistics Hub", "Industrial", "Midlands", "Coventry", 420_000, 0.98),
    ("P08", "Tamar Distribution Centre", "Industrial", "South West", "Plymouth", 310_000, 0.97),
    ("P09", "Thornbury Trade Park", "Industrial", "North West", "Warrington", 150_000, 0.94),
    ("P10", "Linden Gardens", "Residential", "London", "London", 132_000, 0.96),
    ("P11", "Mill Race Apartments", "Residential", "Scotland", "Glasgow", 120_000, 0.95),
    ("P12", "Orchard Place", "Residential", "South West", "Exeter", 98_000, 0.94),
]


def build() -> dict:
    rng = random.Random(20261031)
    properties = []
    for pid, name, sector, region, city, sqft, occ_budget in PROPERTIES:
        s = SECTORS[sector]
        rent_year = sqft * s["rent"] * REGION_RENT[region]
        cost_scale = 0.75 + 0.25 * REGION_RENT[region]

        def jitter(width: float) -> float:
            return 1 + rng.uniform(-width, width)

        occ_actual = [min(0.995, max(0.5, occ_budget + rng.uniform(-0.012, 0.012))) for _ in MONTHS]
        occ_prior = [min(0.995, occ_budget - 0.012 + rng.uniform(-0.01, 0.01)) for _ in MONTHS]
        rent_factor = [1.0] * 12
        repairs_extra = [0] * 12
        utilities_factor = [jitter(0.05) for _ in MONTHS]
        note = None

        if pid == "P05":  # anchor tenant leaves at the end of March
            for i in range(5, 12):
                occ_actual[i] = 0.71 + rng.uniform(-0.01, 0.01)
            note = "Anchor tenant vacated at the end of March 2026; the unit was still empty in October."
        if pid == "P11":  # unbudgeted roof repair
            repairs_extra[6], repairs_extra[7] = 96_000, 142_000
            note = "Unbudgeted roof repair in May and June 2026."
        if pid == "P09":  # rent review settled above budget from February
            for i in range(3, 12):
                rent_factor[i] = 1.085
            occ_actual = [min(0.99, o + 0.03) for o in occ_actual]
            note = "Rent reviews settled about 8.5% above budget from February 2026."
        if pid == "P01":  # winter energy contract overrun
            for i in range(0, 5):
                utilities_factor[i] = 1.27 + rng.uniform(-0.03, 0.03)
            note = "Energy contract renewed at higher rates; winter utilities ran well over budget."
        if pid == "P06":  # slow lettings
            occ_actual = [o - 0.045 for o in occ_actual]
            note = "Lettings ran slower than the budget assumed all year."
        if pid == "P07":
            note = "Fully let on long leases; costs held under budget."

        lines: dict[str, dict[str, list[int]]] = {l["key"]: {"actual": [], "budget": [], "prior": []} for l in LINES}
        for i in range(12):
            month_rent = rent_year / 12
            b_rent, a_rent, p_rent = month_rent, month_rent * rent_factor[i], month_rent * 0.965
            values = {
                "base_rent": (a_rent, b_rent, p_rent),
                "vacancy": (-a_rent * (1 - occ_actual[i]), -b_rent * (1 - occ_budget), -p_rent * (1 - occ_prior[i])),
            }
            rec = sqft * s["recoveries"] / 12
            values["recoveries"] = (rec * occ_actual[i] / occ_budget * jitter(0.03), rec, rec * 0.95 * jitter(0.03))
            other = sqft * s["other"] / 12
            values["other_income"] = (other * jitter(0.12), other, other * 0.93 * jitter(0.1))
            for key, width in (("management", 0.02), ("repairs", 0.22), ("insurance", 0.0),
                               ("taxes", 0.01), ("site_services", 0.05)):
                base = sqft * s[key] * cost_scale / 12
                under = 0.94 if pid == "P07" and key in ("repairs", "site_services") else 1.0
                actual = base * jitter(width) * under + (repairs_extra[i] if key == "repairs" else 0)
                if key == "insurance":
                    actual = base * 1.04  # renewal came in 4% over budget across the portfolio
                values[key] = (actual, base, base * 0.955 * jitter(width / 2))
            util = sqft * s["utilities"] * cost_scale * UTILITY_SEASON[i]
            values["utilities"] = (util * utilities_factor[i], util, util * 0.9 * jitter(0.05))
            for key, (a, b, p) in values.items():
                lines[key]["actual"].append(round(a))
                lines[key]["budget"].append(round(b))
                lines[key]["prior"].append(round(p))

        properties.append({
            "id": pid, "name": name, "sector": sector, "region": region, "city": city, "sqft": sqft,
            "note": note,
            "occupancy": {"actual": [round(o, 4) for o in occ_actual], "budget": [occ_budget] * 12,
                          "prior": [round(o, 4) for o in occ_prior]},
            "lines": lines,
        })

    return {
        "portfolio": "Fictional Property Holdings",
        "fictional": True,
        "currency": "GBP",
        "unit": "whole pounds; revenue lines positive (vacancy negative), cost lines positive",
        "area_unit": "sq ft",
        "months": MONTHS,
        "prior_basis": "same month one year earlier",
        "lines": LINES,
        "properties": properties,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=1) + "\n")
    print(f"wrote {OUT}")
