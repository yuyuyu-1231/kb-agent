"""app/services/retrieval.py — 检索封装。

为什么不直接把 embedder + store 暴露给工具层：

1. **query 侧的 instruction 只能在一处加**
   BGE 的用法是 query 加 instruction、passage 不加。如果工具层自己拼前缀，
   迟早有人忘。封在 Retriever 里，调用方永远只传纯查询串。

2. **检索结果的形状要被约束**
   `context.py` 的 `_shrink_retrieval` 假设返回的是「按 score 降序的 JSON 数组」。
   这个契约在 Retriever 的返回值里明确定义，而不是靠"谁调用谁知道"。

3. **页码整页读取需要一个额外的存储访问**
   `read_document_page` 工具要拿某一页的全文，这需要按 doc_id + page_no
   scroll 出该页所有 chunk 再拼接。这个逻辑属于检索层，不属于工具层。

关于 score_threshold：现在默认 None（不过滤）。
实测发现相关命中 0.59-0.76、不相关 0.32-0.52，分界不明显，
所以 v1 先不做阈值拒答，等第 3 周评估数据出来再定。
"""

from __future__ import annotations

from typing import Any

from app.infra.embedding import Embedder
from app.infra.qdrant import QdrantStore


class Retriever:
    """检索器。无状态，可并发复用。"""

    def __init__(
        self,
        *,
        embedder: Embedder,
        store: QdrantStore,
    ) -> None:
        self.embedder = embedder
        self.store = store

    @classmethod
    def from_infra(cls, embedder: Embedder, store: QdrantStore) -> "Retriever":
        return cls(embedder=embedder, store=store)

    # ---------------------------------------------------------------- 检索

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        doc_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """语义检索。返回按 score 降序的 chunk 列表。

        降序是给 context.py 的 _shrink_retrieval 用的隐式契约——
        它按「丢低分」的方式裁剪，如果顺序反了会保留最差的内容。
        """
        if not query or not query.strip():
            return []

        vector = await self.embedder.embed_query(query)
        hits = await self.store.search(
            vector,
            top_k=top_k,
            doc_name=doc_name,
            score_threshold=score_threshold,
        )
        # 防御性排序：即使 store 返回顺序有问题，这里也保证降序
        hits.sort(key=lambda h: h.get("score") or 0.0, reverse=True)
        return hits

    # ---------------------------------------------------------------- 整页

    async def get_page(self, doc_id: str, page_no: int) -> dict[str, Any]:
        """取某一页的全文。

        用途：`read_document_page` 工具。当 search_knowledge 返回的 chunk
        被上下文裁剪截断时，模型可以主动"翻到原文那一页"看完整内容——
        这是普通 RAG 做不到的能力，也是本项目的演示亮点之一。

        实现方式是从向量库 scroll 出该页的所有 chunk 再按 chunk_index 拼接，
        而不是重新解析 PDF：省一次磁盘 IO，而且保证与已入库内容完全一致。
        """
        chunks = await self.store.get_page_chunks(doc_id, page_no)

        if not chunks:
            return {
                "found": False,
                "doc_id": doc_id,
                "page_no": page_no,
                "text": "",
                "chunk_count": 0,
            }

        chunks.sort(key=lambda c: c.get("chunk_index") or 0)
        text = "\n".join(c.get("text", "") for c in chunks)

        return {
            "found": True,
            "doc_id": doc_id,
            "doc_name": chunks[0].get("doc_name", ""),
            "page_no": page_no,
            "text": text,
            "chunk_count": len(chunks),
            "sections": sorted({c.get("section_title", "") for c in chunks if c.get("section_title")}),
        }

    # ---------------------------------------------------------------- 诊断

    async def stats(self) -> dict[str, Any]:
        """给 /health 和评估脚本用。"""
        return {
            "collection": self.store.collection,
            "dim": self.store.dim,
            "points": await self.store.count(),
        }
