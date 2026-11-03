"""harness/trace.py — 轨迹落盘与回放。

这个模块解决两个问题：
  1. Agent 出问题时怎么调试（一步步看清它想了什么、调了什么、拿到什么）
  2. 评估时数据从哪来（不另写一套采集，直接读 trace）

三个设计要点：

1. **JSONL 里只存摘要，全文另存 artifact**
   一条 observation 可能是几 KB 的检索结果。如果整条塞进 JSONL，
   文件会迅速膨胀到几百 MB，而且同一条内容会被反复写入。
   所以 JSONL 只存 digest + 前 120 字符预览，全文按内容哈希存到
   artifacts/{digest}.json —— 内容寻址天然去重。

2. **fail-open：写盘失败绝不影响主流程**
   trace 是观测设施，不是业务逻辑。磁盘满了、权限错了，都不该让
   用户的问题答不出来。所有写操作都包在 try/except 里吞掉。

3. **一次 step 一次 open/append/close**
   不保持长连接。一个 run 只有几步到十几步，这点开销可忽略；
   换来的是进程崩溃时不丢已写入的数据。

回放：replay(trace_dir, trace_id) 扫描所有按日期分片的文件，
按 trace_id 过滤、按 step 排序，可选把 observation 全文补回来。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """带时区的本地时间字符串，形如 2026-11-03T14:30:00+08:00。

    用带时区的格式而不是裸 datetime.now()，是因为 trace 可能被跨机器
    汇总分析。没有时区的时间戳在合并时无法排序，也无法判断是哪台机器的。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _digest(text: str) -> str:
    """内容哈希，取前 16 位。与 guard.py 的指纹同一套思路。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _to_text(value: Any) -> str:
    """把任意 observation 统一成字符串。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# Tracer
# --------------------------------------------------------------------------- #

class Tracer:
    """一次 Agent run 对应一个 Tracer 实例。

    典型用法：

        tracer = Tracer(data_dir / "traces", data_dir / "artifacts")
        tracer.step(thought="需要查制度", tool="search_knowledge",
                    args={"query": "年假"}, ok=True, latency_ms=45,
                    observation=result.content, tokens=120)
        ...
        tracer.finish(stop_reason="answered", answer="年假 5 天")
    """

    def __init__(
        self,
        trace_dir: Path | str,
        artifact_dir: Path | str,
        trace_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.trace_dir = Path(trace_dir)
        self.artifact_dir = Path(artifact_dir)
        self.trace_id = trace_id or uuid.uuid4().hex
        self.enabled = enabled
        self._step = 0
        self._started = time.perf_counter()

    # ---------------------------------------------------------------- 记录

    def step(
        self,
        *,
        thought: str = "",
        tool: str = "",
        args: dict[str, Any] | None = None,
        ok: bool = True,
        latency_ms: int = 0,
        tokens: int = 0,
        observation: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        """记录一步，返回步号。

        thought 是模型的推理文本（有就记，没有留空）。
        observation 传原始内容，内部会做摘要 + 全文分离存储。
        """
        if not self.enabled:
            return self._step

        self._step += 1

        obs_digest = ""
        obs_preview = ""
        if observation is not None:
            obs_digest, obs_preview = self._save_observation(observation)

        self._append({
            "trace_id": self.trace_id,
            "kind": "step",
            "step": self._step,
            "ts": _now_iso(),
            "thought": thought,
            "tool": tool,
            "args": args or {},
            "ok": ok,
            "latency_ms": latency_ms,
            "tokens": tokens,
            "obs_digest": obs_digest,
            "obs_preview": obs_preview,
            **(extra or {}),
        })
        return self._step

    def finish(
        self,
        *,
        stop_reason: str,
        answer: str = "",
        tokens: int = 0,
        ticket_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """记录结束状态。这是评估时按 stop_reason 分组统计的入口。"""
        if not self.enabled:
            return

        self._append({
            "trace_id": self.trace_id,
            "kind": "final",
            "step": self._step + 1,
            "ts": _now_iso(),
            "stop_reason": stop_reason,
            "answer_preview": (answer or "")[:200],
            "ticket_id": ticket_id,
            "tokens": tokens,
            "total_latency_ms": int((time.perf_counter() - self._started) * 1000),
            **(extra or {}),
        })

    # ---------------------------------------------------------------- 存储

    def _save_observation(self, observation: Any) -> tuple[str, str]:
        """返回 (digest, preview)。

        全文按内容哈希存 artifacts/{digest}.json。同一个 digest 已存在就跳过，
        所以同一条检索结果被读多次也只占一份空间。
        """
        text = _to_text(observation)
        digest = _digest(text)

        try:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifact_dir / f"{digest}.json"
            if not path.exists():
                path.write_text(
                    json.dumps({"digest": digest, "content": text}, ensure_ascii=False),
                    encoding="utf-8",
                )
        except OSError:
            # fail-open：artifact 写不进去，摘要仍然有，不影响主流程
            pass

        preview = text[:120].replace("\n", " ").replace("\r", " ")
        return digest, preview

    def _append(self, record: dict[str, Any]) -> None:
        """追加一行到当天的 JSONL。任何 IO 异常都吞掉。"""
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now().strftime("%Y-%m-%d")
            path = self.trace_dir / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # fail-open：trace 挂了不能让业务挂
            pass


# --------------------------------------------------------------------------- #
# 回放
# --------------------------------------------------------------------------- #

def replay(
    trace_dir: Path | str,
    trace_id: str,
    artifact_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """按 trace_id 取回全部记录，按 step 升序。

    扫描所有 *.jsonl 而不是只读当天文件，因为一次 run 可能跨零点。
    坏行（截断、手动改坏）跳过而不是抛异常 —— 回放是排查工具，
    越宽松越好用。
    """
    records: list[dict[str, Any]] = []
    base = Path(trace_dir)

    if not base.exists():
        return records

    for path in sorted(base.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("trace_id") == trace_id:
                        records.append(record)
        except OSError:
            continue

    records.sort(key=lambda r: r.get("step", 0))

    if artifact_dir:
        art_base = Path(artifact_dir)
        for record in records:
            digest = record.get("obs_digest")
            if not digest:
                continue
            artifact = art_base / f"{digest}.json"
            try:
                if artifact.exists():
                    payload = json.loads(artifact.read_text(encoding="utf-8"))
                    record["observation"] = payload.get("content", "")
            except (OSError, json.JSONDecodeError):
                continue

    return records


def list_trace_ids(trace_dir: Path | str) -> list[str]:
    """列出所有出现过的 trace_id，按最后出现时间倒序。

    给 scripts/replay.py 的命令行用：先看有哪些 run，再挑一个回放。
    """
    seen: dict[str, str] = {}
    base = Path(trace_dir)
    if not base.exists():
        return []

    for path in sorted(base.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tid = record.get("trace_id")
                    if tid:
                        seen[tid] = record.get("ts", "")
        except OSError:
            continue

    return [tid for tid, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]
