r"""读取主项目 .env，生成 kb-agent/.env，并验证 LLM 模型名是否可用。

安全约束（重要）：
- 只输出脱敏信息：key 显示前 6 位 + ***，绝不打印完整密钥
- 只有当目标 .env 不存在时才写入，不覆盖已有配置
- 验证请求只打 GET /models（只读），不做任何计费调用

结果写入 E:\DevCaches\kb_env_report.txt
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(r"E:\PythonProject3\.env")
DST = Path(r"E:\PythonProject3\kb-agent\.env")
OUT = r"E:\DevCaches\kb_env_report.txt"

lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


def mask(secret: str) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= 10:
        return secret[:2] + "***"
    return secret[:6] + "***" + f"（长度 {len(secret)}）"


def parse_env(path: Path) -> dict[str, str]:
    """宽松解析 .env：跳过注释和空行，容忍 export 前缀与引号。"""
    text = None
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"无法解码 {path}")

    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


# --------------------------------------------------------------------------- #
say("=" * 66)
say("1. 读取主项目 .env")
say("=" * 66)

if not SRC.exists():
    say(f"  [FAIL] 源文件不存在: {SRC}")
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print("abort: source .env missing")
    raise SystemExit(1)

src = parse_env(SRC)
say(f"  解析到 {len(src)} 个变量")

llm_key = src.get("LLM_API_KEY", "")
llm_url = src.get("LLM_BASE_URL", "") or "https://api.deepseek.com"
llm_model = src.get("LLM_MODEL_ID", "")

say(f"  LLM_API_KEY   = {mask(llm_key)}")
say(f"  LLM_BASE_URL  = {llm_url or '(未设置)'}")
say(f"  LLM_MODEL_ID  = {llm_model or '(未设置)'}")

missing = [k for k, v in (("LLM_API_KEY", llm_key), ("LLM_MODEL_ID", llm_model)) if not v]
if missing:
    say("")
    say(f"  [FAIL] 缺少必要变量: {', '.join(missing)}")
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print("abort: required vars missing")
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("2. 生成 kb-agent/.env")
say("=" * 66)

if DST.exists():
    say(f"  [SKIP] {DST} 已存在，不覆盖")
else:
    content = f"""# 由 scripts/init_env.py 从 E:\\PythonProject3\\.env 生成
# 不要提交到 git（.gitignore 已排除 .env）

# ---- LLM ----
LLM_BASE_URL={llm_url}
LLM_MODEL_ID={llm_model}
LLM_API_KEY={llm_key}

# ---- Qdrant ----
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=kb_chunks

# ---- Embedding ----
EMBED_MODEL=BAAI/bge-small-zh-v1.5
EMBED_DIM=512
# BGE 中文模型 query 侧必须加此前缀，passage 侧不加
EMBED_QUERY_INSTRUCTION=为这个句子生成表示以用于检索相关文章：

# ---- 分块 ----
CHUNK_SIZE=400
CHUNK_OVERLAP=80

# ---- Agent 内核 ----
TOKEN_BUDGET=30000
MAX_STEPS=6
TOKEN_ESTIMATE_CN=1.0
TOKEN_ESTIMATE_OTHER=0.3
TOOL_TIMEOUT=30
TOOL_RESULT_MAX_CHARS=2000
"""
    DST.write_text(content, encoding="utf-8")
    say(f"  [OK] 已写入 {DST}")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("3. 验证模型名：GET /models（只读）")
say("=" * 66)

base = llm_url.rstrip("/")
if base.endswith("/v1"):
    endpoint = base + "/models"
else:
    endpoint = base + "/models"

say(f"  请求: {endpoint}")

try:
    req = urllib.request.Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {llm_key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    ids = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
    say(f"  [OK] 可用模型 {len(ids)} 个：")
    for mid in ids:
        mark = "  <-- 当前配置" if mid == llm_model else ""
        say(f"       - {mid}{mark}")

    if llm_model in ids:
        say("")
        say(f"  [PASS] 配置的 {llm_model} 确实存在")
    else:
        say("")
        say(f"  [WARN] 配置的 {llm_model} 不在此列表中！必须修改 LLM_MODEL_ID")
        candidates = [m for m in ids if "chat" in m or "reasoner" in m]
        if candidates:
            say(f"         建议改成: {candidates[0]}")

except urllib.error.HTTPError as exc:
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")[:300]
    except Exception:  # noqa: BLE001
        pass
    say(f"  [FAIL] HTTP {exc.code} {exc.reason}")
    if exc.code == 401:
        say("         原因：API key 无效或已过期")
    elif exc.code == 404:
        say("         原因：base_url 路径不对，检查是否需要带 /v1")
    if body:
        say(f"         响应体: {body}")
except urllib.error.URLError as exc:
    say(f"  [FAIL] 连接失败: {exc.reason}")
    say("         可能原因：网络受限（DeepSeek 需要可访问外网），或 base_url 写错")
except Exception as exc:  # noqa: BLE001
    say(f"  [FAIL] {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("4. 下一步：验证 Qdrant")
say("=" * 66)
say("  docker ps 应能看到 qdrant 容器，端口 6333")
say("  若未启动：docker run -d -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant")


Path(OUT).write_text("\n".join(lines), encoding="utf-8")
print(f"written: {OUT}")
