"""harness/context.py — 上下文预算管理与分级裁剪。

三个必须讲清楚的设计点：

1. **token 怎么数**：不装 tiktoken。DeepSeek 的 tokenizer 不公开，
   cl100k / o200k 只是近似。这里用「中文字符 x 1.0 + 其他字符 x 0.3」
   粗估，然后在评估阶段用 API 返回的 usage.prompt_tokens 反推校准系数。
   这比装一个不准的 tokenizer 更诚实，也少一个依赖。

2. **裁剪顺序按「信息损失」排，不按编号排**：
       无损：截断超长的单条工具结果（打标记，模型知道被截了）
       低损：检索结果按分数从低到高丢弃（可重新检索，可再生）
       中损：历史对话滑窗（丢了就没了，影响指代消解）
       兜底：省略最早的中间 observation
   先丢可再生的，后丢不可再生的 —— 这是顺序的唯一依据。

   （SPEC 2.2 原先把顺序写成 L1..L4 递增，是笔误，以本文件为准。）

3. **裁剪必须保持 tool_calls 与 tool 消息的配对**：
   OpenAI 协议要求 assistant 消息里的每个 tool_call 都有对应
   tool_call_id 的 tool 消息。裁掉一半会让下一次请求直接 400。
   所以裁剪以「轮」为单位，且兜底时只替换内容、不删除消息。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------- #
# token 估算
# --------------------------------------------------------------------------- #

def _is_cjk(ch: str) -> bool:
    """是否中日韩统一表意文字（覆盖常用区与扩展 A 区）。"""
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF


def estimate_tokens(
    text: str,
    cn_ratio: float = 1.0,
    other_ratio: float = 0.3,
) -> int:
    """按字符类型估算 token 数。

    依据：主流 BPE tokenizer 里，一个常用汉字通常 1 token 左右，
    英文约 3-4 个字符 1 token。所以中文系数 1.0、其他 0.3。

    这是估算，不是精确值。精确值只能从 API 返回的 usage 拿，
    而拿的时候钱已经花掉了。评估阶段用真实 usage 校准这两个系数。
    """
    if not text:
        return 0
    cn = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cn
    return math.ceil(cn * cn_ratio + other * other_ratio)


def estimate_messages(
    messages: list[dict[str, Any]],
    cn_ratio: float = 1.0,
    other_ratio: float = 0.3,
) -> int:
    """估算整个 messages 数组的 token 数。

    每条消息有固定的协议开销（role、分隔符），业界经验值约 4 token。
    tool_calls 里的 name 和 arguments 也是模型要读的，不能漏算。
    """
    total = 0
    for msg in messages:
        total += 4
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content, cn_ratio, other_ratio)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += 4
            total += estimate_tokens(str(fn.get("name", "")), cn_ratio, other_ratio)
            total += estimate_tokens(str(fn.get("arguments", "")), cn_ratio, other_ratio)
    return total


# --------------------------------------------------------------------------- #
# 轮次切分
# --------------------------------------------------------------------------- #

def split_into_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """以 user 消息为边界把 messages 切成轮次。

    为什么必须以 user 为边界：assistant(tool_calls) 和紧随其后的
    tool 消息是一组、不可分割。user 消息是一轮对话的天然起点，
    只有它前面是安全切割点。
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "user" and current:
            turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)
    return turns


def _tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    """tool_call_id -> 工具名。用于识别哪条 tool 消息来自检索。"""
    mapping: dict[str, str] = {}
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id")
            if tc_id:
                mapping[str(tc_id)] = str((tc.get("function") or {}).get("name", ""))
    return mapping


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #

@dataclass
class FitResult:
    """裁剪结果。actions 记录做了什么，进 trace，评估时可统计触发频率。"""
    messages: list[dict[str, Any]]
    estimated_tokens: int
    original_tokens: int
    trimmed: bool
    actions: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 上下文管理
# --------------------------------------------------------------------------- #

class ContextManager:
    """按预算裁剪 messages。无状态，可并发复用。"""

    def __init__(
        self,
        budget: int = 30000,
        keep_recent_turns: int = 3,
        tool_result_max_chars: int = 2000,
        retrieval_keep_steps: tuple[int, ...] = (3, 1),
        cn_ratio: float = 1.0,
        other_ratio: float = 0.3,
    ) -> None:
        self.budget = budget
        self.keep_recent_turns = keep_recent_turns
        self.tool_result_max_chars = tool_result_max_chars
        self.retrieval_keep_steps = retrieval_keep_steps
        self.cn_ratio = cn_ratio
        self.other_ratio = other_ratio

    # ---------------------------------------------------------------- 估算

    def estimate(self, messages: list[dict[str, Any]]) -> int:
        return estimate_messages(messages, self.cn_ratio, self.other_ratio)

    # ---------------------------------------------------------------- 裁剪

    def fit(self, messages: list[dict[str, Any]]) -> FitResult:
        """把 messages 裁到预算内。未超预算时原样返回（不做任何改动）。"""
        original = self.estimate(messages)
        if original <= self.budget:
            return FitResult(
                messages=messages,
                estimated_tokens=original,
                original_tokens=original,
                trimmed=False,
            )

        # 浅拷贝：只改 dict 的键值，不动调用方的对象
        working: list[dict[str, Any]] = [dict(m) for m in messages]
        actions: list[str] = []

        # ---- 第 1 级（无损）：截断超长工具结果 ------------------------------
        changed = self._truncate_tool_outputs(working)
        if changed:
            actions.append(f"截断 {changed} 条超长工具结果")
        current = self.estimate(working)
        if current <= self.budget:
            return FitResult(working, current, original, True, actions)

        # ---- 第 2 级（低损）：检索结果按相关度降级 --------------------------
        for keep in self.retrieval_keep_steps:
            changed = self._shrink_retrieval(working, keep)
            if changed:
                actions.append(f"检索结果降级为 top{keep}（{changed} 条）")
            current = self.estimate(working)
            if current <= self.budget:
                return FitResult(working, current, original, True, actions)

        # ---- 第 3 级（中损）：历史对话滑窗 --------------------------------
        dropped_turns = self._keep_recent_turns(working)
        if dropped_turns:
            actions.append(f"历史滑窗，丢弃最早 {dropped_turns} 轮")
        current = self.estimate(working)
        if current <= self.budget:
            return FitResult(working, current, original, True, actions)

        # ---- 第 4 级（兜底）：省略最早的 observation ----------------------
        omitted = self._omit_oldest_observations(working)
        if omitted:
            actions.append(f"省略最早 {omitted} 条工具结果")

        return FitResult(working, self.estimate(working), original, True, actions)

    # ---------------------------------------------------------------- 各级实现

    def _truncate_tool_outputs(self, messages: list[dict[str, Any]]) -> int:
        """单条工具结果超长则截断，并在尾部打标记。

        标记不能省。模型如果不知道自己看到的是残缺内容，会基于半截信息
        给出自信的错误答案；打了标记它才有机会改用 read_document_page 精读。
        """
        changed = 0
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) <= self.tool_result_max_chars:
                continue
            original_len = len(content)
            msg["content"] = (
                content[: self.tool_result_max_chars]
                + f"\n[truncated, 原始 {original_len} 字符]"
            )
            changed += 1
        return changed

    def _shrink_retrieval(self, messages: list[dict[str, Any]], keep: int) -> int:
        """把检索工具的返回列表按相关度保留前 keep 条。

        检索结果是全链路里唯一「可再生」的内容 —— 丢掉后模型可以换 query
        重新检索。所以它排在历史对话之前被牺牲。
        """
        name_map = _tool_name_map(messages)
        changed = 0
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            if name_map.get(msg.get("tool_call_id")) != "search_knowledge":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            try:
                data = json.loads(content)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, list) or len(data) <= keep:
                continue
            dropped = len(data) - keep
            msg["content"] = (
                json.dumps(data[:keep], ensure_ascii=False, default=str)
                + f"\n[已按相关度丢弃 {dropped} 条低分片段]"
            )
            changed += 1
        return changed

    def _keep_recent_turns(self, messages: list[dict[str, Any]]) -> int:
        """只保留最近 N 轮，返回被丢弃的轮数。

        以轮为单位重建列表（原地替换内容），保证 tool_calls 配对不被打断。
        """
        turns = split_into_turns(messages)
        if len(turns) <= self.keep_recent_turns:
            return 0
        kept = [msg for turn in turns[-self.keep_recent_turns:] for msg in turn]
        dropped = len(turns) - self.keep_recent_turns
        messages[:] = kept
        return dropped

    def _omit_oldest_observations(self, messages: list[dict[str, Any]], limit: int = 3) -> int:
        """兜底：把最早的几条工具结果内容换成占位符。

        只替换 content、不删除消息 —— 删除会破坏 tool_call_id 配对，
        下一次请求直接 400。这是很多人在这一步踩的坑。
        """
        omitted = 0
        for msg in messages:
            if omitted >= limit:
                break
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if isinstance(content, str) and len(content) > 200:
                msg["content"] = "[已省略：上下文预算不足]"
                omitted += 1
        return omitted
