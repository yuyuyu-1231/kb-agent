"""app/models — API 层的请求/响应数据结构。

单独成包的理由：路由层只做协议转换，数据结构集中在一处，
改字段不会漏改某个路由。
"""

from .schemas import (
    AskRequest,
    AskResponse,
    CitationOut,
    HealthResponse,
    TicketListResponse,
    TicketOut,
    UploadResponse,
)

__all__ = [
    "AskRequest",
    "AskResponse",
    "CitationOut",
    "HealthResponse",
    "TicketListResponse",
    "TicketOut",
    "UploadResponse",
]
