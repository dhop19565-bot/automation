"""One-shot proof run for the NOI dashboard's chat: ask real questions and report what came back.

    python scripts/noi_live_check.py                  # real AI; needs ANTHROPIC_API_KEY
    python scripts/noi_live_check.py --provider demo
    python scripts/noi_live_check.py "Why is Castlegate Arcade behind budget?"

Goes through the same HTTP endpoint the dashboard uses, so a pass covers the prompt,
the structured-output schema, suggestion validation and the audit entry. For each
answer it also lists the pound amounts in the prose that do not appear among the
figures the model was given. That is a prompt for a human to look, not a verdict:
a sum or a difference the model worked out correctly will show up there too.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.config import Settings  # noqa: E402
from hub.main import FIXTURES, create_app  # noqa: E402
from hub.noi import chat  # noqa: E402
from hub.noi.metrics import Portfolio  # noqa: E402

QUESTIONS = [
    "How are we doing against budget, and what are the two biggest reasons?",
    "Which property has the worst NOI margin, and is that a cost problem or a revenue problem?",
    "Compare utilities in the winter months with the summer months. Who is driving the overrun?",
    "What will NOI be next year?",  # the data cannot answer this; a good reply says so
]
_AMOUNT = re.compile(r"£\s?(\d[\d,]*(?:\.\d+)?)\s?(m|k|million|thousand)?", re.IGNORECASE)


def _numbers(value: object) -> set[float]:
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {abs(float(value))}
    if isinstance(value, dict):
        return set().union(*(_numbers(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_numbers(v) for v in value)) if value else set()
    return set()


def untraced_amounts(answer: str, known: set[float]) -> list[str]:
    """Pound amounts in the answer that match no given figure at the precision they were written."""
    out = []
    for match in _AMOUNT.finditer(answer):
        digits, unit = match.group(1).replace(",", ""), (match.group(2) or "").lower()
        scale = 1e6 if unit in ("m", "million") else 1e3 if unit in ("k", "thousand") else 1
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        value, tolerance = float(digits) * scale, 0.51 * scale / 10 ** decimals
        if not any(abs(k - value) <= tolerance for k in known):
            out.append(match.group(0))
    return out


async def main(provider: str, questions: list[str]) -> int:
    portfolio = Portfolio.load(FIXTURES / "noi_portfolio_2026-10.json")
    known = _numbers(chat.build_payload(portfolio, portfolio.parse_view({}), "", []))
    app = create_app(dataclasses.replace(Settings.from_env(), db_path=":memory:"))
    failures, history = 0, []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.local",
                                 timeout=None) as http:
        for question in questions:
            started = time.monotonic()
            response = await http.post("/api/noi/ask", json={"question": question, "provider": provider,
                                                             "view": {}, "history": history})
            body = response.json()
            print(f"\nQ: {question}")
            if response.status_code != 200:
                failures += 1
                print(f"   FAILED {response.status_code}: {body.get('error', body)}")
                continue
            p = body["provenance"]
            print(f"   {p['execution_mode']} · {p['model']} · {time.monotonic() - started:.1f}s")
            print("   " + body["answer"].replace("\n", "\n   "))
            for s in body["suggestions"]:
                detail = s.get("widget") or s.get("view") or s.get("property_id")
                print(f"   suggestion: {s['kind']} · {s['label']} · {detail}")
            for note in body["notes"]:
                print(f"   note: {note}")
            loose = untraced_amounts(body["answer"], known)
            print(f"   amounts not found among the given figures: {', '.join(loose) if loose else 'none'}")
            history.append({"question": question, "answer": body["answer"]})
        audit = (await http.get("/api/audit")).json()
    answered = [e for e in audit["entries"] if e["event"] == "noi.question_answered"]
    usage = [e["detail"].get("usage") for e in answered]
    print(f"\n{len(answered)} of {len(questions)} answered · audit chain intact: {audit['chain']['intact']}")
    print(f"token usage per question: {usage}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", default="claude", choices=["claude", "demo"])
    parser.add_argument("questions", nargs="*", help="questions to ask instead of the built-in set")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.provider, args.questions or QUESTIONS)))
