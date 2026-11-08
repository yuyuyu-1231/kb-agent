"""app/api/documents.py — 文档上传。

对应实现规范里的四条硬约束：

    C4  响应固定 document_id / chunk_count / status
    C5  超 50 页或 20 MB → 413，不让超限请求跑满 worker
    C6  解析 + embedding 是重活，不在事件循环里直接做
    C7  以文件内容 sha256 为幂等键

## 幂等的三个分支

| 情况 | 行为 |
|---|---|
| 同名 + 同内容 | 直接返回已有 chunk 数，**跳过解析和 embedding** |
| 同名 + 不同内容 | 删旧写新（视为同一份文档的更新） |
| 不同名 | 各自独立入库 |

`doc_id` 取文件名去扩展名，所以**同名文件会被视为同一份文档**。
这是企业知识库场景的合理假设：同名 = 同一份制度的新版本。

## 为什么 413 要分两道检查

- **第一道看字节数**：在读之前就能拒绝，连解析都省了
- **第二道看页数**：只有解析完才知道页数，但至少 embedding 还没跑

贵的操作（embedding）排在两道检查之后，这是顺序设计的关键。
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import get_retriever
from app.core.config import get_settings
from app.models.schemas import UploadResponse
from app.services.ingest import parse_pdf
from app.services.retrieval import Retriever

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_PAGES = 50
MAX_BYTES = 20 * 1024 * 1024      # 20 MB
MAX_MB = MAX_BYTES // (1024 * 1024)


@router.post(
    "/upload",
    response_model=UploadResponse,
    summary="上传文档并入库",
    responses={
        413: {"description": "文件超过 50 页或 20 MB"},
        422: {"description": "PDF 解析失败或无可提取文本"},
    },
)
async def upload_document(
    file: UploadFile = File(...),
    retriever: Retriever = Depends(get_retriever),
) -> UploadResponse:
    settings = get_settings()

    # ---- 1. 读内容 + 大小检查（第一道 413）----
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")

    size_mb = len(data) / (1024 * 1024)
    if len(data) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {size_mb:.1f} MB，超过 {MAX_MB} MB 上限，请拆分后上传",
        )

    original_name = file.filename or "unnamed.pdf"
    doc_id = Path(original_name).stem
    content_hash = hashlib.sha256(data).hexdigest()

    # ---- 2. 幂等：同名同内容直接返回（C7）----
    meta = await retriever.store.get_document_meta(doc_id)
    if meta and meta["chunk_count"] > 0 and meta["content_hash"] == content_hash:
        return UploadResponse(
            document_id=doc_id,
            chunk_count=meta["chunk_count"],
            status="completed",
        )

    # ---- 3. 落临时文件，解析（C6：CPU 重活丢线程池）----
    tmp_path = Path(tempfile.gettempdir()) / f"kb-upload-{uuid4().hex}.pdf"
    tmp_path.write_bytes(data)

    try:
        chunks, result = await asyncio.to_thread(
            parse_pdf,
            tmp_path,
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
        )
    except Exception as exc:  # noqa: BLE001 - 损坏的 PDF / 加密 / 非 PDF 都可能抛
        raise HTTPException(
            status_code=422,
            detail=f"PDF 解析失败：{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    # ---- 4. 页数检查（第二道 413）----
    if result.pages > MAX_PAGES:
        raise HTTPException(
            status_code=413,
            detail=f"文档共 {result.pages} 页，超过 {MAX_PAGES} 页上限，请拆分后上传",
        )

    if not chunks:
        raise HTTPException(
            status_code=422,
            detail="未能从 PDF 中提取到任何文本。如果是扫描件，需要先做 OCR。",
        )

    # ---- 5. embedding + 写入（C6：同样丢线程池）----
    vectors = await retriever.embedder.embed_passages([c["text"] for c in chunks])

    # 同名不同内容：先删旧数据，避免旧 chunk 残留成为干扰项
    await retriever.store.delete_document(doc_id)
    await retriever.store.upsert_chunks(
        doc_id=doc_id,
        doc_name=original_name,
        chunks=chunks,
        vectors=vectors,
        content_hash=content_hash,
    )

    return UploadResponse(
        document_id=doc_id,
        chunk_count=len(chunks),
        status="completed",
    )


@router.get("", summary="列出已入库的文档")
async def list_documents(
    retriever: Retriever = Depends(get_retriever),
) -> dict:
    """列出知识库里有哪些文档。

    上传面板需要它来显示"库里已有什么"，否则用户不知道能问什么。
    """
    docs = await retriever.store.list_documents()
    return {"total": len(docs), "items": docs}


@router.get("/{doc_id}", summary="查看某份文档的入库信息")
async def get_document(
    doc_id: str,
    retriever: Retriever = Depends(get_retriever),
) -> dict:
    meta = await retriever.store.get_document_meta(doc_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"文档不存在: {doc_id}")
    return meta
