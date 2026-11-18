"""Thin wrapper around the Anthropic API for structured (Pydantic-validated) responses."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# If the model declines a request, the API re-runs it on a fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class OutOfCredits(LLMError):
    """The Anthropic account has no credit left: every further call would fail the same way."""


CREDITS_HELP = ("Your Anthropic credit balance is used up, so Claude can't read jobs or write resumes. "
                "Add credits at console.anthropic.com (Billing), then run the command again.")


def is_out_of_credits(e: BaseException) -> bool:
    return isinstance(e, OutOfCredits) or "credit balance is too low" in str(e)


def guard_credits(call):
    """Run one API call; an out-of-credits error is flagged on the meter and raised as OutOfCredits."""
    try:
        return call()
    except anthropic.APIStatusError as e:
        if is_out_of_credits(e):
            METER.out_of_credits = True
            raise OutOfCredits(CREDITS_HELP) from e
        raise


# ---- cost meter ----------------------------------------------------------------
# Every response carries a usage "receipt" (tokens read and written). The meter prices those receipts per step
# so the daily summary can show what each part of the run cost.

# $ per million tokens: (input, output, cache read, 5-minute cache write). Haiku 5.5: prompts up to 100K tokens.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
WEB_SEARCH_PRICE = 0.01  # $ per search ($10 per 1,000)
BATCH_DISCOUNT = 0.5     # the Message Batches API bills tokens at half price


class CostMeter:
    def __init__(self):
        self._lock = threading.Lock()
        self.dollars: dict[str, float] = {}
        self.out_of_credits = False  # set by guard_credits; reported at the end of prepare and apply

    def record(self, step: str, model: str, usage, batch: bool = False) -> float:
        inp, out, read, write = PRICES.get(model, PRICES["claude-sonnet-5-5"])
        tokens = (
            (getattr(usage, "input_tokens", 0) or 0) * inp
            + (getattr(usage, "output_tokens", 0) or 0) * out
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * read
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * write
        ) / 1_000_000
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        cost = tokens * (BATCH_DISCOUNT if batch else 1) + searches * WEB_SEARCH_PRICE
        with self._lock:
            self.dollars[step] = self.dollars.get(step, 0.0) + cost
        return cost

    def flush(self, path: Path, day: str | None = None) -> dict[str, float]:
        """Add what was recorded to the day's totals in path (JSON) and start over. Returns the day's totals."""
        day = day or date.today().isoformat()
        with self._lock:
            data = json.loads(path.read_text()) if path.exists() else {}
            totals = data.setdefault(day, {})
            for step, cost in self.dollars.items():
                totals[step] = round(totals.get(step, 0.0) + cost, 4)
            self.dollars = {}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=1, sort_keys=True))
            return totals


METER = CostMeter()


def make_client(api_key: str | None) -> anthropic.Anthropic:
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set. Add it to .env.")
    return anthropic.Anthropic(api_key=api_key)


def has_credits(client: anthropic.Anthropic, model: str = "claude-haiku-5-5") -> bool:
    """A one-token call (a tiny fraction of a cent) to learn up front whether the account can be used."""
    try:
        guard_credits(lambda: client.messages.create(
            model=model, max_tokens=1, messages=[{"role": "user", "content": "ok"}]))
    except OutOfCredits:
        return False
    except anthropic.APIError:
        return True  # some other hiccup: let the real calls try (and report) as usual
    return True


def parse(
    client: anthropic.Anthropic,
    *,
    model: str,
    schema: type[T],
    system: str,
    content: list[dict] | str,
    max_tokens: int = 16000,
    effort: str = "medium",
    cache_system: bool = False,
    step: str = "other",
) -> T:
    # A cached system prompt is billed at a fraction of the price when the next call reuses it.
    sys_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache_system else system
    response = guard_credits(lambda: client.beta.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=sys_param,
        messages=[{"role": "user", "content": content}],
        output_format=schema,
        output_config={"effort": effort},
        **_model_kwargs(model),
    ))
    if response.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise LLMError("Response was cut off (max_tokens); raise the limit.")
    if response.parsed_output is None:
        raise LLMError("Response did not match the expected schema.")
    METER.record(step, model, response.usage)
    log.debug("llm usage: %s", response.usage)
    return response.parsed_output


WEB_SEARCH = "web_search_20260209"      # with dynamic filtering: Sonnet / Opus
WEB_SEARCH_BASIC = "web_search_20250305"  # Haiku


def _model_kwargs(model: str) -> dict:
    """Haiku has no server-side fallback model, so the fallback beta is only sent for other models."""
    return {} if "haiku" in model else {"betas": [FALLBACK_BETA], "fallbacks": "default"}


def parse_with_search(
    client: anthropic.Anthropic,
    *,
    model: str,
    schema: type[T],
    system: str,
    content: str,
    max_searches: int = 4,
    max_tokens: int = 16000,
    effort: str = "medium",
    step: str = "other",
) -> tuple[T, list[dict]]:
    """Like parse(), but Claude may search the web first (Anthropic's server-side web search tool).

    Returns (answer, results) where results are the pages the searches actually returned
    ({"url", "title"}). Callers check that any link in the answer came from these results."""
    messages: list[dict] = [{"role": "user", "content": content}]
    results: list[dict] = []
    for _ in range(5):  # the server pauses long search loops; resend to let it continue
        response = guard_credits(lambda: client.beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=[{"type": WEB_SEARCH_BASIC if "haiku" in model else WEB_SEARCH, "name": "web_search",
                    "max_uses": max_searches}],
            output_format=schema,
            output_config={"effort": effort},
            **_model_kwargs(model),
        ))
        METER.record(step, model, response.usage)
        # Search results can arrive as web_search_tool_result blocks or, when Claude filters pages with code
        # first, inside other tool-result blocks; collect every URL the tools returned, wherever it appears.
        for block in response.content:
            if block.type != "text" and block.type.endswith("tool_result"):
                results += [{"url": u, "title": ""} for u in _urls(block.model_dump())]
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    if response.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise LLMError("Response was cut off (max_tokens); raise the limit.")
    if response.parsed_output is None:
        raise LLMError("Response did not match the expected schema.")
    log.debug("llm usage (with search): %s", response.usage)
    return response.parsed_output, results


def _urls(value) -> list[str]:
    """Every http(s) URL inside a (nested) tool result."""
    if isinstance(value, dict):
        return [u for v in value.values() for u in _urls(v)]
    if isinstance(value, list):
        return [u for v in value for u in _urls(v)]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return [value]
        return re.findall(r"https?://[^\s\"'<>)\]]+", value) if "http" in value else []
    return []
