r"""验证 deepseek-flash 是否支持 Function Calling。

这是整个 Agent 项目的地基假设：如果模型不支持 tools 参数，
ToolRegistry 生成的所有 schema 都是废纸。

验证两件事：
1. 单轮：能否正确返回 tool_calls
2. 多轮：把 tool 结果喂回去后，能否基于结果继续生成答案

成本：约 2 次调用、几百 token，几分钱。
结果写入 E:\DevCaches\kb_fc_report.txt
"""

import json
from pathlib import Path

OUT = r"E:\DevCaches\kb_fc_report.txt"
ENV_PATH = Path(r"E:\PythonProject3\kb-agent\.env")

lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


def parse_env(path: Path) -> dict[str, str]:
    text = None
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    result: dict[str, str] = {}
    if not text:
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


env = parse_env(ENV_PATH)
model = env.get("LLM_MODEL_ID", "deepseek-flash")
base_url = env.get("LLM_BASE_URL", "https://api.deepseek.com")
api_key = env.get("LLM_API_KEY", "")

say("=" * 66)
say("Function Calling 冒烟测试")
say("=" * 66)
say(f"  model    = {model}")
say(f"  base_url = {base_url}")
say(f"  api_key  = {api_key[:6]}***" if api_key else "  api_key  = (空)")

if not api_key:
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print("abort: no api key")
    raise SystemExit(1)

from openai import OpenAI  # noqa: E402

client = OpenAI(api_key=api_key, base_url=base_url)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "在企业知识库中检索与问题相关的文档片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言检索词"},
                "top_k": {"type": "integer", "default": 5, "description": "返回条数"},
            },
            "required": ["query"],
        },
    },
}]

QUESTION = "试用期员工能休年假吗？请先检索知识库再回答。"


# --------------------------------------------------------------------------- #
say("")
say("-" * 66)
say("第一轮：模型是否主动调用工具")
say("-" * 66)

try:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": QUESTION}],
        tools=TOOLS,
    )
except Exception as exc:  # noqa: BLE001
    say(f"  [FAIL] 调用失败: {type(exc).__name__}: {exc}")
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print("fail: first call error")
    raise SystemExit(1)

choice = resp.choices[0]
say(f"  finish_reason = {choice.finish_reason}")
say(f"  content       = {choice.message.content!r}")
say(f"  usage         = prompt={resp.usage.prompt_tokens} completion={resp.usage.completion_tokens}")

tool_calls = choice.message.tool_calls or []
say(f"  tool_calls 数量 = {len(tool_calls)}")

for tc in tool_calls:
    say(f"    - id        = {tc.id}")
    say(f"      name      = {tc.function.name}")
    say(f"      arguments = {tc.function.arguments}")

if not tool_calls:
    say("")
    say("  [FAIL] 模型没有返回 tool_calls —— Function Calling 不可用")
    say(f"  建议：换 deepseek-v4-pro 再试，或用 prompt 硬编码 JSON 输出（降级方案）")
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print("fail: no tool_calls")
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
say("")
say("-" * 66)
say("第二轮：把工具结果喂回去，模型能否基于结果作答")
say("-" * 66)

fake_tool_result = json.dumps(
    [
        {"text": "员工累计工作满 1 年不满 10 年的，年休假 5 天。", "page_no": 12,
         "doc_name": "员工手册.pdf", "section_title": "第三章 假期制度"},
    ],
    ensure_ascii=False,
)

messages = [
    {"role": "user", "content": QUESTION},
    choice.message.model_dump(exclude_none=True),
]

# 关键：每个 tool_call_id 都必须有一条对应的 tool 消息。
# 模型这次返回了 2 个 tool_calls（它自己拆成了两个检索词），
# 只回喂 1 条会直接 400 —— 这就是协议配对约束的真实形态。
for tc in tool_calls:
    messages.append({
        "role": "tool",
        "tool_call_id": tc.id,
        "content": fake_tool_result,
    })

try:
    resp2 = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
except Exception as exc:  # noqa: BLE001
    say(f"  [FAIL] 第二轮调用失败: {type(exc).__name__}: {exc}")
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print("fail: second call error")
    raise SystemExit(1)

choice2 = resp2.choices[0]
say(f"  finish_reason = {choice2.finish_reason}")
say(f"  content       = {choice2.message.content!r}")
say(f"  是否再调工具   = {bool(choice2.message.tool_calls)}")
say(f"  usage         = prompt={resp2.usage.prompt_tokens} completion={resp2.usage.completion_tokens}")

answer = choice2.message.content or ""
if "5" in answer or "五" in answer:
    say("")
    say("  [PASS] 模型正确使用了工具返回的内容（提到了 5 天）")
else:
    say("")
    say("  [WARN] 模型可能没有正确利用工具结果，需人工检查上面的 content")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("结论")
say("=" * 66)
say(f"  Function Calling: 可用")
say(f"  tool_call_id 格式: {tool_calls[0].id}")
say(f"  完整两轮 token 成本: {resp.usage.total_tokens + resp2.usage.total_tokens}")

Path(OUT).write_text("\n".join(lines), encoding="utf-8")
print(f"written: {OUT}")
