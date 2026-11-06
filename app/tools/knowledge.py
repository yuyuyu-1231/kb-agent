"""app/tools/knowledge.py — 知识库相关的两个工具。

这两个工具是整个 Agent 的"手脚"：模型能不能找到依据、能不能看到原文，
全取决于这里的返回内容。

设计要点：

1. **search_knowledge 必须返回 doc_id**
   `read_document_page` 需要 doc_id 作为参数。如果搜索时不返回，
   模型就无法在需要看原文时构造出正确的调用参数，这个工具等于废掉。

2. **只返回模型需要的字段**
   向量库里还有 created_at、char_start、char_end 等字段，
   对模型没用，全塞进去只是浪费 token。

3. **docstring 的参数说明要具体**
   模型选工具看工具描述，填参数看参数说明。写成"检索词"太笼统，
   写成"用具体的业务术语，不要用整句话"能明显降低检索质量波动。

4. **read_document_page 是本项目区别于普通 RAG 的能力**
   普通 RAG 只有"检索 top-k → 生成"，检索结果被截断就只能基于残片回答。
   这里模型可以主动翻原文，实现"证据不够就去看清楚"。
"""

from __future__ import annotations

from typing import Any

from app.services.retrieval import Retriever
from harness.tools import ToolRegistry


def register_knowledge_tools(registry: ToolRegistry, retriever: Retriever) -> None:
    """把知识库检索能力注册到工具表。"""

    @registry.tool(timeout=20, citation_aware=True)
    async def search_knowledge(
        query: str,
        top_k: int = 5,
        doc_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """在企业知识库中检索与问题相关的文档片段。

        返回的每条结果包含 index、doc_id、doc_name、page_no、section_title、text、score。
        引用时用 index 字段的值作为编号。如果结果片段被截断（末尾有 [truncated] 标记），
        可以用 read_document_page 读取完整原文。

        Args:
            query: 检索词。使用具体的业务术语，不要用整句话或口语化描述。
            top_k: 返回条数，默认 5。找不到时可以提高，但条数越多上下文越长。
            doc_name: 限定在某一份文档内检索，可选。不确定文档名时不要传。
        """
        hits = await retriever.search(query, top_k=top_k, doc_name=doc_name)
        return [
            {
                "doc_id": hit.get("doc_id", ""),
                "doc_name": hit.get("doc_name", ""),
                "page_no": hit.get("page_no"),
                "section_title": hit.get("section_title", ""),
                "text": hit.get("text", ""),
                "score": round(hit["score"], 4) if hit.get("score") is not None else None,
                # chunk_index 不直接给模型看（loop 用它做去重后删掉），
                # 但必须传出来，否则无法判断「是不是同一个 chunk」。
                "chunk_index": hit.get("chunk_index"),
            }
            for hit in hits
        ]

    @registry.tool(timeout=15)
    async def read_document_page(doc_id: str, page_no: int) -> dict[str, Any]:
        """读取某份文档某一页的完整原文。

        当检索片段被截断、或者需要看上下文才能判断答案时使用。
        如果该页不存在，返回 found=false。

        Args:
            doc_id: 文档标识。从 search_knowledge 结果的 doc_id 字段获取。
            page_no: 页码。从 search_knowledge 结果的 page_no 字段获取。
        """
        return await retriever.get_page(doc_id, page_no)
