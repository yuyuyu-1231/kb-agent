"""harness/trace.py 的单元测试。全部使用 tmp_path，不碰真实 data 目录。"""

from __future__ import annotations

import json
from pathlib import Path

from harness.trace import Tracer, list_trace_ids, replay


def make_tracer(tmp_path: Path, **kwargs) -> Tracer:
    return Tracer(
        trace_dir=tmp_path / "traces",
        artifact_dir=tmp_path / "artifacts",
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 基本写入与回放
# --------------------------------------------------------------------------- #

def test_step_and_replay(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(thought="需要查制度", tool="search_knowledge", args={"query": "年假"},
                ok=True, latency_ms=42, tokens=120, observation="[{\"text\":\"年假5天\"}]")
    tracer.finish(stop_reason="answered", answer="年假 5 天", tokens=300)

    records = replay(tmp_path / "traces", tracer.trace_id)
    assert len(records) == 2

    step_rec = records[0]
    assert step_rec["kind"] == "step"
    assert step_rec["step"] == 1
    assert step_rec["tool"] == "search_knowledge"
    assert step_rec["args"] == {"query": "年假"}
    assert step_rec["latency_ms"] == 42
    assert step_rec["tokens"] == 120

    final_rec = records[1]
    assert final_rec["kind"] == "final"
    assert final_rec["stop_reason"] == "answered"
    assert final_rec["ticket_id"] is None


def test_step_returns_increasing_index(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    assert tracer.step(tool="a") == 1
    assert tracer.step(tool="b") == 2
    assert tracer.step(tool="c") == 3


def test_trace_id_unique_by_default(tmp_path: Path) -> None:
    a = make_tracer(tmp_path)
    b = make_tracer(tmp_path)
    assert a.trace_id != b.trace_id
    assert len(a.trace_id) == 32


def test_trace_id_can_be_injected(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path, trace_id="fixed-id-001")
    assert tracer.trace_id == "fixed-id-001"


# --------------------------------------------------------------------------- #
# observation 摘要 + 全文分离
# --------------------------------------------------------------------------- #

def test_observation_split_into_artifact(tmp_path: Path) -> None:
    """JSONL 只存 digest + preview，全文进 artifacts。"""
    tracer = make_tracer(tmp_path)
    big = "年假制度说明：" + "A" * 5000
    tracer.step(tool="read_document_page", observation=big)

    # JSONL 里没有全文
    jsonl = next((tmp_path / "traces").glob("*.jsonl"))
    raw = jsonl.read_text(encoding="utf-8")
    assert "A" * 5000 not in raw
    assert len(raw) < 2000, "JSONL 单行应该是摘要级别的体积"

    # artifact 里有全文
    artifacts = list((tmp_path / "artifacts").glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["content"] == big


def test_same_observation_deduplicated(tmp_path: Path) -> None:
    """同一段 observation 只存一份 artifact（内容寻址去重）。"""
    tracer = make_tracer(tmp_path)
    same = "完全相同的一段检索结果"
    tracer.step(tool="search", observation=same)
    tracer.step(tool="search", observation=same)
    tracer.step(tool="search", observation=same)

    assert len(list((tmp_path / "artifacts").glob("*.json"))) == 1


def test_replay_can_restore_full_observation(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    content = "完整内容" * 500
    tracer.step(tool="search", observation=content)

    records = replay(
        tmp_path / "traces",
        tracer.trace_id,
        artifact_dir=tmp_path / "artifacts",
    )
    assert records[0]["observation"] == content


def test_replay_without_artifact_dir_keeps_preview_only(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(tool="search", observation="x" * 300)

    records = replay(tmp_path / "traces", tracer.trace_id)
    assert "observation" not in records[0]
    assert records[0]["obs_preview"]
    assert len(records[0]["obs_preview"]) <= 120


def test_preview_strips_newlines(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(observation="第一行\n第二行\r\n第三行")
    records = replay(tmp_path / "traces", tracer.trace_id)
    assert "\n" not in records[0]["obs_preview"]


def test_non_string_observation_serialized(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(observation=[{"page": 1, "text": "年假"}])

    records = replay(tmp_path / "traces", tracer.trace_id, artifact_dir=tmp_path / "artifacts")
    body = records[0]["observation"]
    assert json.loads(body) == [{"page": 1, "text": "年假"}]


# --------------------------------------------------------------------------- #
# 跨文件、坏行、禁用
# --------------------------------------------------------------------------- #

def test_replay_across_multiple_files(tmp_path: Path) -> None:
    """模拟一次 run 跨零点：记录分散在两个日期的文件里。"""
    traces = tmp_path / "traces"
    traces.mkdir(parents=True)
    (traces / "2026-11-02.jsonl").write_text(
        json.dumps({"trace_id": "T1", "step": 1, "kind": "step"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (traces / "2026-11-03.jsonl").write_text(
        json.dumps({"trace_id": "T1", "step": 2, "kind": "step"}, ensure_ascii=False) + "\n"
        + json.dumps({"trace_id": "T2", "step": 1, "kind": "step"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    records = replay(traces, "T1")
    assert [r["step"] for r in records] == [1, 2]


def test_replay_skips_broken_lines(tmp_path: Path) -> None:
    traces = tmp_path / "traces"
    traces.mkdir(parents=True)
    (traces / "2026-11-03.jsonl").write_text(
        '{"trace_id": "T1", "step": 1, "kind": "step"}\n'
        'this is not json\n'
        '{"trace_id": "T1", "step": 2, "kind": "step"}\n',
        encoding="utf-8",
    )
    records = replay(traces, "T1")
    assert len(records) == 2


def test_replay_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert replay(tmp_path / "nope", "any") == []


def test_disabled_tracer_writes_nothing(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path, enabled=False)
    tracer.step(tool="x", observation="y")
    tracer.finish(stop_reason="answered")
    assert not (tmp_path / "traces").exists()


def test_unknown_trace_id_returns_empty(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(tool="x")
    assert replay(tmp_path / "traces", "not-exist") == []


# --------------------------------------------------------------------------- #
# 其他
# --------------------------------------------------------------------------- #

def test_extra_fields_recorded(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(tool="x", extra={"tokens_before": 900, "tokens_after": 300})
    records = replay(tmp_path / "traces", tracer.trace_id)
    assert records[0]["tokens_before"] == 900
    assert records[0]["tokens_after"] == 300


def test_list_trace_ids(tmp_path: Path) -> None:
    traces = tmp_path / "traces"
    traces.mkdir(parents=True)
    (traces / "2026-11-03.jsonl").write_text(
        json.dumps({"trace_id": "A", "ts": "2026-11-03T10:00:00+08:00", "step": 1}) + "\n"
        + json.dumps({"trace_id": "B", "ts": "2026-11-03T11:00:00+08:00", "step": 1}) + "\n",
        encoding="utf-8",
    )
    ids = list_trace_ids(traces)
    assert ids == ["B", "A"], "按最后出现时间倒序"


def test_finish_records_ticket_id(tmp_path: Path) -> None:
    tracer = make_tracer(tmp_path)
    tracer.step(tool="create_ticket")
    tracer.finish(stop_reason="ticket_created", ticket_id="T-123")
    records = replay(tmp_path / "traces", tracer.trace_id)
    assert records[-1]["ticket_id"] == "T-123"
