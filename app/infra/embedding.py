"""app/infra/embedding.py — fastembed 封装（BGE-small-zh）。

三个关键设计：

1. **query 和 passage 用不同的入口**
   BGE 系列模型的检索用法是：query 侧加一句 instruction，passage 侧不加。
   官方原话是 v1.5 版本 instruction 不是必需的，但加上能提升几个点。
   所以对外暴露两个方法而不是一个通用的 embed()，避免调用方用错。

2. **模型懒加载 + 异步初始化**
   TextEmbedding() 第一次构造时会下载模型（约 90MB）并加载 ONNX 会话，
   耗时可能十几秒。放在 import 时做会让整个进程启动变慢；
   放在每次调用时做会重复加载。所以用「首次使用时加载 + 锁保护」。

3. **同步 CPU 计算丢线程池**
   ONNX Runtime 的推理是同步的 C++ 调用，直接在协程里跑会阻塞事件循环。
   asyncio.to_thread 把它挪到线程池；ONNX 内部会释放 GIL，所以确实能并行。

关于维度：bge-small-zh-v1.5 是 512 维。这个数字必须和 Qdrant collection
的 size 一致，改模型就要重建 collection。
"""

from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path
from typing import Any

# 必须在 import fastembed / huggingface_hub 之前设置：
# huggingface_hub 在 import 时就把 HF_ENDPOINT 读成模块级常量，之后改环境变量无效。
#
# 为什么要改：实测国内直连 huggingface.co 时，「浏览器能打开，但 fastembed
# 下载模型 timeout」。因为 hf_hub_download 走的是 Python requests，它不读
# Windows 系统代理设置。hf-mirror.com 是国内常用镜像，实测可用。
# 用 setdefault 是为了不覆盖用户自己设的值。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from fastembed import TextEmbedding  # noqa: E402


class Embedder:
    """BGE 嵌入器。实例可并发复用（内部有锁保护初始化）。"""

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        dim: int = 512,
        query_instruction: str = "为这个句子生成表示以用于检索相关文章：",
        cache_dir: Path | str | None = None,
    ) -> None:
        self.model_name = model_name
        self.dim = dim
        self.query_instruction = query_instruction
        self.cache_dir = str(cache_dir) if cache_dir else None
        self._model: TextEmbedding | None = None
        self._init_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Any) -> "Embedder":
        return cls(
            model_name=settings.embed_model,
            dim=settings.embed_dim,
            query_instruction=settings.embed_query_instruction,
            # 缓存目录优先读环境变量：容器里是 /models/fastembed_cache（镜像构建期已预热），
            # 本地开发沿用 E:\DevCaches\fastembed，避免落到 C 盘用户目录。
            # 以前这里写死 Windows 路径，容器里该路径根本不存在 ——
            # 结果每次启动都当空缓存，重下 95MB 模型，/health 卡在启动阶段十几分钟。
            cache_dir=Path(os.getenv("FASTEMBED_CACHE_PATH") or r"E:\DevCaches\fastembed"),
        )

    # ---------------------------------------------------------------- 模型

    async def _ensure_model(self) -> TextEmbedding:
        """首次调用时下载并加载模型。"""
        if self._model is not None:
            return self._model

        async with self._init_lock:
            if self._model is None:
                # 下载 + 初始化是阻塞操作，必须丢线程池
                self._model = await asyncio.to_thread(
                    functools.partial(
                        TextEmbedding,
                        model_name=self.model_name,
                        cache_dir=self.cache_dir,
                        threads=2,
                    )
                )
        return self._model

    # ---------------------------------------------------------------- 编码

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """编码被检索的文档片段。不加 instruction。"""
        if not texts:
            return []
        model = await self._ensure_model()
        vectors = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [v.tolist() for v in vectors]

    async def embed_query(self, text: str) -> list[float]:
        """编码检索提问。加 instruction。"""
        if not text:
            raise ValueError("query 不能为空")
        model = await self._ensure_model()
        payload = self.query_instruction + text
        vectors = await asyncio.to_thread(lambda: list(model.embed([payload])))
        return vectors[0].tolist()

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """批量编码 query（评估阶段一次算几十条时用）。"""
        if not texts:
            return []
        model = await self._ensure_model()
        payload = [self.query_instruction + t for t in texts]
        vectors = await asyncio.to_thread(lambda: list(model.embed(payload)))
        return [v.tolist() for v in vectors]

    # ---------------------------------------------------------------- 诊断

    async def warmup(self) -> dict[str, Any]:
        """触发模型加载并自检维度。返回诊断信息。"""
        vector = await self.embed_query("这是一个用于自检的句子")
        return {
            "model": self.model_name,
            "expected_dim": self.dim,
            "actual_dim": len(vector),
            "dim_ok": len(vector) == self.dim,
        }
