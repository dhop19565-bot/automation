"""ClaudeProvider against the real Anthropic SDK with a mocked HTTP transport.

No network and no real key: the SDK builds and sends a genuine request, and a
mock transport plays the API. This checks the wire format and error mapping.
It does not prove a live call works - only a real key can do that.
"""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from hub.providers.base import (
    MODE_REAL_AI,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderNotConfigured,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTask,
    ProviderUnavailable,
)
from hub.providers.claude import ClaudeProvider
from hub.workflows.derivatives.workflow import DerivativesWorkflow

from .conftest import JO, Harness, clean_output, load_input

FAKE_KEY = "sk-ant-test-NOT-A-REAL-KEY-0000"


def message(text: str, stop_reason: str = "end_turn", **extra) -> dict:
    return {"id": "msg_test_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}] if text is not None else [],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 2100, "output_tokens": 900}, **extra}


def provider_with(handler) -> tuple[ClaudeProvider, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key=FAKE_KEY, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recording)))
    return ClaudeProvider(client=client), seen


def task() -> ProviderTask:
    wf = DerivativesWorkflow()
    instructions, payload, schema = wf.provider_task(wf.parse_input(load_input()))
    return ProviderTask("RUN-TEST", wf.name, instructions, payload, schema)


def ok(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=message(json.dumps(clean_output())), headers={"request-id": "req_test_123"})


def api_error(status: int, kind: str):
    body = {"type": "error", "error": {"type": kind, "message": f"{kind} from mock"}}
    return lambda _: httpx2.Response(status, json=body, headers={"request-id": "req_err_1"})


async def test_request_uses_the_messages_api_with_structured_output():
    provider, seen = provider_with(ok)
    result = await provider.run(task())

    request = seen[0]
    assert request.method == "POST" and request.url.path == "/v1/messages"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"  # how SDK 2.x sends an API key
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["summary", "journals", "exceptions"]
    assert "lower-of test" in body["system"]
    assert "thinking" not in body and "temperature" not in body
    user_text = body["messages"][0]["content"]
    assert "IRS-002" in user_text and "77250.00" in user_text
    assert "Retained earnings" not in user_text  # unrelated ledger balances stay on the server

    assert result.execution_mode == MODE_REAL_AI and result.model == "claude-opus-5-5"
    assert result.provider_request_id == "req_test_123"
    assert result.usage == {"input_tokens": 2100, "output_tokens": 900}
    assert len(result.output["journals"]) == 5


@pytest.mark.parametrize("handler, expected", [
    (api_error(401, "authentication_error"), ProviderAuthError),
    (api_error(403, "permission_error"), ProviderAuthError),
    (api_error(429, "rate_limit_error"), ProviderRateLimited),
    (api_error(400, "invalid_request_error"), ProviderBadRequest),
    (api_error(404, "not_found_error"), ProviderBadRequest),
    (api_error(500, "api_error"), ProviderUnavailable),
    (api_error(529, "overloaded_error"), ProviderUnavailable),
])
async def test_api_errors_map_to_stable_codes_without_leaking_the_key(handler, expected):
    provider, _ = provider_with(handler)
    with pytest.raises(expected) as exc:
        await provider.run(task())
    assert FAKE_KEY not in exc.value.message and exc.value.provider_request_id == "req_err_1"


async def test_connection_failure_is_reported_as_unavailable():
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    provider, _ = provider_with(boom)
    with pytest.raises(ProviderUnavailable):
        await provider.run(task())


async def test_refusal_is_not_read_as_an_answer():
    body = message(None, "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})
    provider, _ = provider_with(lambda _: httpx2.Response(200, json=body))
    with pytest.raises(ProviderRefused, match="cyber"):
        await provider.run(task())


async def test_truncated_answer_is_rejected():
    provider, _ = provider_with(lambda _: httpx2.Response(200, json=message('{"summary": "cut', "max_tokens")))
    with pytest.raises(ProviderOutputTruncated):
        await provider.run(task())


@pytest.mark.parametrize("text", ["not json at all", "[1, 2, 3]", None])
async def test_unusable_answer_is_rejected(text):
    provider, _ = provider_with(lambda _: httpx2.Response(200, json=message(text)))
    with pytest.raises(ProviderOutputInvalid):
        await provider.run(task())


async def test_without_a_credential_the_provider_is_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    provider = ClaudeProvider()
    info = provider.info()
    assert info.available is False and "ANTHROPIC_API_KEY" in info.unavailable_reason
    assert info.execution_mode == MODE_REAL_AI
    with pytest.raises(ProviderNotConfigured):
        await provider.run(task())


async def test_key_in_the_environment_makes_it_available_without_exposing_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    info = ClaudeProvider().info().to_dict()
    assert info["available"] is True and FAKE_KEY not in json.dumps(info)


async def test_claude_run_goes_through_the_engine_to_posting():
    provider, _ = provider_with(ok)
    h = Harness(extra_providers=[provider])
    run = await h.submit_and_wait(provider="claude")
    assert run["status"] == "awaiting_approval" and run["execution_mode"] == "real_ai"
    assert run["provider_meta"]["model"] == "claude-opus-5-5"
    assert run["provider_meta"]["provider_request_id"] == "req_test_123"
    await h.engine.approve(run["id"], JO, proposal_hash=run["proposal_hash"])
    await h.engine.wait_idle()
    done = h.run(run["id"])
    assert done["status"] == "posted" and done["posting"]["simulated"] is True


async def test_a_wrong_answer_from_claude_is_stopped_by_validation():
    wrong = clean_output()
    irs = next(j for j in wrong["journals"] if j["instrument_id"] == "IRS-002")
    irs["lines"] = [l for l in irs["lines"] if l["account"] != "7110"]
    next(l for l in irs["lines"] if l["account"] == "3500")["credit"] = "23250.00"
    provider, _ = provider_with(lambda _: httpx2.Response(200, json=message(json.dumps(wrong))))
    h = Harness(extra_providers=[provider])
    run = await h.submit_and_wait(provider="claude")
    assert run["status"] == "validation_failed" and h.ledger.calls == []
