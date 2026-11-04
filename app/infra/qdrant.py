"""app/infra/qdrant.py — Qdrant 封装。

四个设计要点：

1. **collection 结构在第一次写入前就定死**
   payload 字段和 SPEC 3.1 保持一致：
       doc_id, doc_name, page_no, section_title, chunk_index,
       char_start, char_end, text, created_at
   第 4 周换标题分块时只重灌数据，检索/生成/前端零改动。

2. **point id 用 uuid5 而不是自增**
   Qdrant 的 point id 只接受 uint64 或 UUID 字符串。
   用 uuid5(namespace, f"{doc_id}:{chunk_index}") 保证「同一文档同一块」
   得到同样的 id，重复入库天然幂等（upsert 会覆盖而不是插重复）。

3. **建 collection 用 VectorParams + Cosine**
   归一化向量下 Cosine 与 Dot 等价，但 Cosine 对未归一化的向量更稳。
   fastembed 输出的 BGE 向量已归一化，用 Cosine 不会出错。

4. **检索用 query_points 而不是 search**
   qdrant-client 1.10+ 把 search 归并进 Query API，1.20 上
   query_points 是推荐入口，search 保留但属于旧接口。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

# 固定命名空间，保证不同进程算出的 point id 一致
ID_NAMESPACE = uuid.UUID("6f1c2a3e-8b4d-4f5a-9c7e-1d2b3a4c5e6f")


def make_point_id(doc_id: str, chunk_index: int) -> str:
    """同一文档的同一块永远得到同一个 id。"""
    return str(uuid.uuid5(ID_NAMESPACE, f"{doc_id}:{chunk_index}"))


class QdrantStore:
    def __init__(
        self,
        *,
        url: str = "http://localhost:6333",
        collection: str = "kb_chunks",
        dim: int = 512,
    ) -> None:
        self.url = url
        self.collection = collection
        self.dim = dim
        self._client = AsyncQdrantClient(url=url, timeout=30.0)

    @classmethod
    def from_settings(cls, settings: Any) -> "QdrantStore":
        return cls(
            url=settings.qdrant_url,
            collection=settings.qdrant_collection,
            dim=settings.embed_dim,
        )

    # ---------------------------------------------------------------- 生命周期

    async def ensure_collection(self) -> bool:
        """确保 collection 存在且维度正确。返回 True 表示本次创建了。"""
        exists = await self._client.collection_exists(self.collection)
        if exists:
            info = await self._client.get_collection(self.collection)
            actual = info.config.params.vectors.size
            if actual != self.dim:
                raise ValueError(
                    f"collection {self.collection} 维度是 {actual}，"
                    f"但配置要求 {self.dim}。换模型必须重建 collection。"
                )
            return False

        await self._client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )
        return True

    async def count(self) -> int:
        result = await self._client.count(collection_name=self.collection, exact=True)
        return int(result.count)

    async def close(self) -> None:
        await self._client.close()

    # ---------------------------------------------------------------- 写入

    async def upsert_chunks(
        self,
        *,
        doc_id: str,
        doc_name: str,
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
    ) -> int:
        """批量写入。chunk 与 vector 必须一一对应。

        chunks 里每项至少有 text / page_no / chunk_index；
        可选 section_title / char_start / char_end。
        """
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunk 数({len(chunks)}) 与向量数({len(vectors)}) 不一致"
            )
        if not chunks:
            return 0

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        points: list[PointStruct] = []
        for chunk, vector in zip(chunks, vectors):
            idx = int(chunk.get("chunk_index", len(points)))
            payload = {
                "doc_id": doc_id,
                "doc_name": doc_name,
                "page_no": chunk.get("page_no"),
                "section_title": chunk.get("section_title", ""),
                "chunk_index": idx,
                "char_start": chunk.get("char_start"),
                "char_end": chunk.get("char_end"),
                "text": chunk.get("text", ""),
                "created_at": now,
            }
            points.append(PointStruct(
                id=make_point_id(doc_id, idx),
                vector=vector,
                payload=payload,
            ))

        await self._client.upsert(
            collection_name=self.collection,
            points=points,
            wait=True,               # 等索引完成再返回，避免"写完立刻查不到"
        )
        return len(points)

    async def delete_document(self, doc_id: str) -> None:
        """删除某个文档的所有块（重新上传同一文件时先清旧数据）。"""
        await self._client.delete(
            collection_name=self.collection,
            points_selector=Filter(must=[
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id))
            ]),
            wait=True,
        )

    # ---------------------------------------------------------------- 检索

    async def search(
        self,
        vector: list[float],
        *,
        top_k: int = 5,
        doc_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索，返回按相关度降序的 chunk 列表。

        返回的列表必须保持降序 —— context.py 的 _shrink_retrieval
        依赖这个顺序做「丢低分」裁剪。这是一个隐式契约。
        """
        query_filter: Filter | None = None
        if doc_name:
            query_filter = Filter(must=[
                FieldCondition(key="doc_name", match=MatchValue(value=doc_name))
            ])

        result = await self._client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            query_filter=query_filter,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=False,
        )

        hits: list[dict[str, Any]] = []
        for point in result.points:
            payload = point.payload or {}
            hits.append({
                "text": payload.get("text", ""),
                "doc_id": payload.get("doc_id", ""),
                "doc_name": payload.get("doc_name", ""),
                "page_no": payload.get("page_no"),
                "section_title": payload.get("section_title", ""),
                "chunk_index": payload.get("chunk_index"),
                "score": float(point.score) if point.score is not None else None,
            })
        return hits

    # ---------------------------------------------------------------- 诊断

    async def health(self) -> dict[str, Any]:
        created = await self.ensure_collection()
        return {
            "url": self.url,
            "collection": self.collection,
            "dim": self.dim,
            "created_now": created,
            "points": await self.count(),
        }
