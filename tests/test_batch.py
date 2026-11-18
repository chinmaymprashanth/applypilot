from types import SimpleNamespace as NS

from pydantic import BaseModel

from jobagent import llm
from jobagent.batch import run_batch


class Answer(BaseModel):
    ok: bool


class FakeBatches:
    def __init__(self):
        self.sent = None
        self.polls = 0

    def create(self, requests):
        self.sent = requests
        return NS(id="b1", processing_status="in_progress")

    def retrieve(self, bid):
        self.polls += 1
        return NS(id=bid, processing_status="ended" if self.polls > 1 else "in_progress",
                  request_counts=NS(succeeded=1, errored=1, expired=0, canceled=0))

    def results(self, bid):
        usage = NS(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                   server_tool_use=None)
        ok = NS(type="succeeded", message=NS(usage=usage, stop_reason="end_turn",
                                             content=[NS(type="text", text='{"ok": true}')]))
        return [NS(custom_id="a", result=ok), NS(custom_id="b", result=NS(type="errored"))]


def test_run_batch_parses_results_and_bills_half_price(monkeypatch):
    monkeypatch.setattr("jobagent.batch.time.sleep", lambda s: None)
    meter = llm.CostMeter()
    monkeypatch.setattr("jobagent.batch.METER", meter)
    client = NS(messages=NS(batches=FakeBatches()))
    out = run_batch(client, model="claude-haiku-5-5", schema=Answer, system="sys", items={"a": "x", "b": "y"},
                    step="first screen")
    assert out["a"] == Answer(ok=True) and isinstance(out["b"], Exception)
    sent = client.messages.batches.sent[0]["params"]
    assert sent["output_config"]["format"]["type"] == "json_schema" and "fallbacks" not in sent
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert round(meter.dollars["first screen"], 4) == 0.05  # 1M input tokens at $0.10, half price


def test_cost_meter_prices_by_model_and_saves_daily_totals(tmp_path):
    m = llm.CostMeter()
    usage = NS(input_tokens=10_000, output_tokens=2_000, cache_read_input_tokens=50_000, cache_creation_input_tokens=0,
               server_tool_use=NS(web_search_requests=2))
    m.record("research", "claude-sonnet-5-5", usage)
    # 10k*$2 + 2k*$10 + 50k*$0.10 per million, plus 2 searches at $0.01
    assert round(m.dollars["research"], 4) == round(0.02 + 0.02 + 0.005 + 0.02, 4)
    path = tmp_path / "costs.json"
    m.flush(path, day="2026-11-13")
    m.record("research", "claude-sonnet-5-5", usage)
    assert m.flush(path, day="2026-11-13") == {"research": 0.13}


def test_stopped_run_collects_its_batch_instead_of_paying_twice(monkeypatch, tmp_path):
    monkeypatch.setattr("jobagent.batch.time.sleep", lambda s: None)
    monkeypatch.setattr("jobagent.batch.STATE", tmp_path / "batches.json")
    monkeypatch.setattr("jobagent.batch.METER", llm.CostMeter())

    class Stopped(Exception):
        pass

    first = FakeBatches()
    first.retrieve = lambda bid: (_ for _ in ()).throw(Stopped())  # the computer is taken back mid-wait
    try:
        run_batch(NS(messages=NS(batches=first)), model="m", schema=Answer, system="s", items={"a": "x", "b": "y"},
                  step="scoring")
    except Stopped:
        pass
    assert "b1" in (tmp_path / "batches.json").read_text()

    second = FakeBatches()
    out = run_batch(NS(messages=NS(batches=second)), model="m", schema=Answer, system="s",
                    items={"a": "x", "b": "y"}, step="scoring")
    assert second.sent is None  # nothing submitted again
    assert out["a"] == Answer(ok=True)
    assert not (tmp_path / "batches.json").read_text().strip("{}\n ")  # forgotten once collected
