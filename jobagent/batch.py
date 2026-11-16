"""Run many Claude requests through the Message Batches API: half price, results in minutes to hours.

Used for scoring, which runs unattended in the morning and doesn't need answers in seconds.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

import anthropic
from pydantic import BaseModel

from .llm import METER

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def run_batch(
    client: anthropic.Anthropic,
    *,
    model: str,
    schema: type[T],
    system: str,
    items: dict[str, str],
    max_tokens: int = 4000,
    effort: str = "low",
    step: str = "other",
    poll_seconds: int = 30,
    progress: Callable[[str], None] | None = None,
) -> dict[str, T | Exception]:
    """{custom_id: content} -> {custom_id: parsed answer or the error}. The system prompt is cached across requests."""
    if not items:
        return {}
    output_format = {"type": "json_schema", "schema": anthropic.transform_schema(schema.model_json_schema())}
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    batch = client.messages.batches.create(requests=[
        {"custom_id": cid, "params": {
            "model": model, "max_tokens": max_tokens, "system": system_blocks,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": effort, "format": output_format},
        }} for cid, content in items.items()
    ])
    started = time.time()
    while batch.processing_status != "ended":
        time.sleep(poll_seconds)
        batch = client.messages.batches.retrieve(batch.id)
        if progress:
            c = batch.request_counts
            progress(f"    batch {step}: {c.succeeded + c.errored + c.expired + c.canceled}/{len(items)} done "
                     f"({round((time.time() - started) / 60)} min)")
    out: dict[str, T | Exception] = {}
    for result in client.messages.batches.results(batch.id):
        r = result.result
        if r.type != "succeeded":
            out[result.custom_id] = RuntimeError(f"batch request {r.type}")
            continue
        msg = r.message
        METER.record(step, model, msg.usage, batch=True)
        if msg.stop_reason in ("refusal", "max_tokens"):
            out[result.custom_id] = RuntimeError(f"stopped: {msg.stop_reason}")
            continue
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            out[result.custom_id] = schema.model_validate_json(text)
        except ValueError as e:
            out[result.custom_id] = e
    return out
