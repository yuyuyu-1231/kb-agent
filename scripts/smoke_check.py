r"""第 0 步冒烟：验证依赖可用性与关键假设。

结果写入 E:\DevCaches\kb_smoke.txt
"""

import importlib
import os

OUT = r"E:\DevCaches\kb_smoke.txt"
lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


# --------------------------------------------------------------------------- #
say("=" * 66)
say("1. 依赖洁净度：确认没有装进不该有的东西")
say("=" * 66)

for name in ("torch", "pandas", "magika"):
    try:
        importlib.import_module(name)
        say(f"  [WARN] {name} 被装进来了")
    except ImportError:
        say(f"  [OK]   {name} 未安装")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("2. 关键包版本")
say("=" * 66)

for mod in ("fastapi", "uvicorn", "pydantic", "pydantic_settings",
            "openai", "qdrant_client", "fastembed", "numpy", "onnxruntime"):
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, "__version__", "?")
        say(f"  {mod:<18} {ver}")
    except Exception as exc:  # noqa: BLE001
        say(f"  {mod:<18} 导入失败: {type(exc).__name__}: {exc}")

# PyMuPDF 2.x 改了模块名，两个都试
fitz = None
for candidate in ("pymupdf", "fitz"):
    try:
        fitz = importlib.import_module(candidate)
        say(f"  {candidate:<18} {getattr(fitz, '__version__', '?')}  (模块名: {candidate})")
        break
    except Exception:  # noqa: BLE001
        continue
if fitz is None:
    say("  [FAIL] PyMuPDF 两种模块名都导不进来")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("3. 核心假设：fastembed 是否支持 bge-small-zh-v1.5")
say("=" * 66)

try:
    from fastembed import TextEmbedding

    models = TextEmbedding.list_supported_models()
    zh_models = [m for m in models if "zh" in str(m.get("model", "")).lower()]

    say(f"  共支持 {len(models)} 个文本嵌入模型，其中中文相关 {len(zh_models)} 个：")
    for m in zh_models:
        dim = m.get("dim") or m.get("dimension")
        size = m.get("size_in_GB")
        say(f"    - {m.get('model')}   dim={dim}   size={size}GB")

    target = [m for m in zh_models if "bge-small-zh" in str(m.get("model", ""))]
    if target:
        say("")
        say(f"  [OK] 目标模型存在：{target[0].get('model')}")
        say(f"       dim={target[0].get('dim')}  （Qdrant collection 必须用这个维度）")
    else:
        say("")
        say("  [FAIL] 没有找到 bge-small-zh-v1.5，备选方案如下：")
        for m in models[:12]:
            say(f"    {m.get('model')}")
except Exception as exc:  # noqa: BLE001
    say(f"  [FAIL] fastembed 导入或查询失败: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("4. PyMuPDF 能力确认")
say("=" * 66)

if fitz is not None:
    say(f"  Page.get_text 存在: {hasattr(fitz.Page, 'get_text')}")
    say(f"  支持 'blocks' 模式: 'blocks' in str(fitz.__doc__ or '') or '见文档'")
    try:
        import pymupdf4llm
        say(f"  pymupdf4llm 可用（PDF -> Markdown）")
    except ImportError as exc:
        say(f"  pymupdf4llm 不可用: {exc}")
    try:
        import pymupdf_layout  # noqa: F401
        say(f"  pymupdf_layout 可用（版面分析）")
    except ImportError as exc:
        say(f"  pymupdf_layout 不可用: {exc}")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("5. 缓存位置：确认模型不会落到 C 盘")
say("=" * 66)

say(f"  HF_HOME              = {os.environ.get('HF_HOME')}")
say(f"  HF_HUB_CACHE         = {os.environ.get('HF_HUB_CACHE')}")
say(f"  XDG_CACHE_HOME       = {os.environ.get('XDG_CACHE_HOME')}")
say(f"  用户目录             = {os.path.expanduser('~')}")
say("")
say("  说明：fastembed 通过 huggingface_hub 下载模型，会读 HF_HOME。")
say("        若 HF_HOME 为空，模型会落到 C:\\Users\\<name>\\.cache\\huggingface。")


# --------------------------------------------------------------------------- #
say("")
say("=" * 66)
say("6. 环境变量当前状态（.env 尚未创建）")
say("=" * 66)
for key in ("LLM_BASE_URL", "LLM_MODEL_ID", "LLM_API_KEY", "QDRANT_URL"):
    val = os.environ.get(key)
    if key == "LLM_API_KEY" and val:
        val = val[:6] + "***"
    say(f"  {key:<14} = {val or '(未设置)'}")


with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

print(f"written: {OUT} ({len(lines)} lines)")
