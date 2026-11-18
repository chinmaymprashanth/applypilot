"""Run many Claude requests through the Message Batches API: half price, results in minutes to hours.

Used for scoring, which runs unattended in the morning and doesn't need answers in seconds.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar

import anthropic
from pydantic import BaseModel

from .llm import METER, guard_credits, is_out_of_credits

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Where submitted batches are remembered (set by the CLI to data/batches.json). If a run is stopped while
# Anthropic is still working on a batch (in AWS, a Spot interruption), the rerun collects that batch instead of
# paying for the same requests twice. None: don't remember (tests, one-off calls).
STATE: Path | None = None


def _load_state() -> dict:
    if STATE is None or not STATE.exists():
        return {}
    data = json.loads(STATE.read_text())
    today = date.today().isoformat()
    return {step: [b for b in batches if b.get("date") == today] for step, batches in data.items()}


def _save_state(data: dict) -> None:
    if STATE is not None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({k: v for k, v in data.items() if v}, indent=1))


def _wait(client, batch, step: str, total: int, poll_seconds: int, progress):
    started = time.time()
    while batch.processing_status != "ended":
        time.sleep(poll_seconds)
        batch = client.messages.batches.retrieve(batch.id)
        if progress:
            c = batch.request_counts
            progress(f"    batch {step}: {c.succeeded + c.errored + c.expired + c.canceled}/{total} done "
                     f"({round((time.time() - started) / 60)} min)")
    return batch


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
    state = _load_state()
    earlier = state.get(step, [])
    out: dict[str, T | Exception] = {}
    # 1. Batches sent earlier today for this step (a run that was stopped): collect what they cover.
    for saved in earlier:
        wanted = [cid for cid in saved["ids"] if cid in items]
        if not wanted:
            continue
        log.info("resuming batch %s for %s (%d requests)", saved["id"], step, len(wanted))
        batch = _wait(client, client.messages.batches.retrieve(saved["id"]), step, len(saved["ids"]),
                      poll_seconds, progress)
        out.update({cid: r for cid, r in _results(client, batch.id, schema, model, step).items() if cid in items})
    items = {cid: c for cid, c in items.items() if cid not in out}
    # 2. Everything else in one new batch, remembered until its results are in.
    if items:
        output_format = {"type": "json_schema", "schema": anthropic.transform_schema(schema.model_json_schema())}
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        batch = guard_credits(lambda: client.messages.batches.create(requests=[
            {"custom_id": cid, "params": {
                "model": model, "max_tokens": max_tokens, "system": system_blocks,
                "messages": [{"role": "user", "content": content}],
                "output_config": {"effort": effort, "format": output_format},
            }} for cid, content in items.items()
        ]))
        state.setdefault(step, []).append({"id": batch.id, "ids": list(items), "date": date.today().isoformat()})
        _save_state(state)
        batch = _wait(client, batch, step, len(items), poll_seconds, progress)
        out.update(_results(client, batch.id, schema, model, step))
    state.pop(step, None)
    _save_state(state)
    return out


def _results(client, batch_id: str, schema: type[T], model: str, step: str) -> dict[str, T | Exception]:
    out: dict[str, T | Exception] = {}
    for result in client.messages.batches.results(batch_id):
        r = result.result
        if r.type != "succeeded":
            detail = str(getattr(r, "error", "") or "")
            if is_out_of_credits(RuntimeError(detail)):
                METER.out_of_credits = True
            out[result.custom_id] = RuntimeError(f"batch request {r.type} {detail[:200]}".strip())
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
