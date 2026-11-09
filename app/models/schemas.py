"""app/models/schemas.py — 请求/响应契约。

三条约定：

1. **入参用 Pydantic 校验，出参用 Pydantic 裁剪**
   出参模型只列允许暴露的字段。内部字段（trace 文件路径、chunk_index、
   token 明细）不进响应体 —— 既省带宽，也避免把内部实现写进对外契约。

2. **session_id 是可选的**
   首次请求不传，服务端生成并返回；后续请求回传。
   见 AskRequest 的注释。

3. **错误不用 Pydantic 模型**
   FastAPI 的 HTTPException 会生成 `{"detail": "..."}`，
   再定义一套 ErrorResponse 只会让客户端多写一个分支。

命名：`*Request` 入参、`*Response` 出参、`*Out` 嵌在响应里的子结构。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 问答
# --------------------------------------------------------------------------- #

class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="用户问题",
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "会话 ID。首次请求不传，服务端生成并在响应里返回；"
            "后续请求必须原样回传，否则视为新会话"
        ),
    )


class CitationOut(BaseModel):
    """一条引用。index 对应答案里的 [n]。"""
    index: int
    doc_name: str
    page_no: int | None = None
    section_title: str = ""
    snippet: str = ""
    score: float | None = None


class AskResponse(BaseModel):
    answer: str
    session_id: str = Field(..., description="本次会话 ID，后续请求回传它")
    trace_id: str = Field(..., description="轨迹 ID，可用 GET /traces/{id} 回放")
    stop_reason: str
    citations: list[CitationOut] = Field(default_factory=list)
    ticket_id: str | None = None
    steps: int = 0
    latency_ms: int = 0
    tokens: int = 0


# --------------------------------------------------------------------------- #
# 文档上传
# --------------------------------------------------------------------------- #

class UploadResponse(BaseModel):
    """同步上传的响应。

    `status` 固定为 `completed`。以后切换异步时，这里改成
    `pending/parsing/embedding/indexed/failed`，前端代码不用动。
    """
    document_id: str
    chunk_count: int
    status: str = "completed"


# --------------------------------------------------------------------------- #
# 工单
# --------------------------------------------------------------------------- #

class TicketOut(BaseModel):
    id: str
    title: str
    category: str
    priority: str
    status: str
    summary: str = ""
    evidence: str = ""
    created_at: str
    updated_at: str


class TicketListResponse(BaseModel):
    total: int
    items: list[TicketOut]


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #

class HealthResponse(BaseModel):
    """**永远返回 200**，依赖状态放在 body 里。

    为什么不用 503：编排系统用状态码决定“要不要摘掉这个实例”。
    健康检查的语义是“进程还活着”，依赖临时不可用不等于进程该死。
    返回 503 会让 K8s 杀掉所有副本，反而扩大故障。

    status 取值：
      ok        一切正常
      degraded  进程活着，但向量库不可用（启动时没连上）
    """
    status: str
    chunks: int
    tickets: int
    sessions: int
    # degraded 时带上原因，前端直接展示，省得用户猜
    detail: str = ""
