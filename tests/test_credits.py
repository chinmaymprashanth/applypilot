"""Running out of Anthropic credit is reported clearly instead of failing job by job."""

import anthropic
import httpx
import pytest

from jobagent import llm


def _credit_error() -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API."}}
    return anthropic.BadRequestError("Error code: 400 - " + str(body), response=httpx.Response(400, request=req),
                                     body=body)


def test_guard_turns_credit_errors_into_out_of_credits(monkeypatch):
    monkeypatch.setattr(llm.METER, "out_of_credits", False)

    def call():
        raise _credit_error()

    with pytest.raises(llm.OutOfCredits):
        llm.guard_credits(call)
    assert llm.METER.out_of_credits
    assert llm.is_out_of_credits(RuntimeError("... credit balance is too low ..."))
    assert not llm.is_out_of_credits(RuntimeError("rate limited"))


def test_other_api_errors_pass_through(monkeypatch):
    monkeypatch.setattr(llm.METER, "out_of_credits", False)
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def call():
        raise anthropic.BadRequestError("bad schema", response=httpx.Response(400, request=req), body=None)

    with pytest.raises(anthropic.BadRequestError):
        llm.guard_credits(call)
    assert not llm.METER.out_of_credits


class _Messages:
    def __init__(self, error=None):
        self.error = error

    def create(self, **kw):
        if self.error:
            raise self.error
        return object()


class _Client:
    def __init__(self, error=None):
        self.messages = _Messages(error)


def test_has_credits(monkeypatch):
    monkeypatch.setattr(llm.METER, "out_of_credits", False)
    assert llm.has_credits(_Client())
    assert not llm.has_credits(_Client(_credit_error()))


class _Usage:
    input_tokens = output_tokens = cache_read_input_tokens = cache_creation_input_tokens = 0
    server_tool_use = None


class _Resp:
    stop_reason = "end_turn"
    content = []
    usage = _Usage()

    def __init__(self, parsed):
        self.parsed_output = parsed


def test_haiku_searches_use_the_basic_tool_without_fallbacks():
    from pydantic import BaseModel

    class Out(BaseModel):
        ok: bool = True

    seen = {}

    class Beta:
        class messages:  # noqa: N801
            @staticmethod
            def parse(**kw):
                seen.update(kw)
                return _Resp(Out())

    client = type("C", (), {"beta": Beta})()
    llm.parse_with_search(client, model="claude-haiku-5-5", schema=Out, system="s", content="c")
    assert seen["tools"][0]["type"] == llm.WEB_SEARCH_BASIC and "fallbacks" not in seen
    llm.parse_with_search(client, model="claude-sonnet-5-5", schema=Out, system="s", content="c")
    assert seen["tools"][0]["type"] == llm.WEB_SEARCH and seen["fallbacks"] == "default"
