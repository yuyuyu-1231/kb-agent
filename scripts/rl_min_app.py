"""最小 ASGI 应用，专供限流的「多 worker」验证。

## 为什么不直接用 `app.main:app`

真实 app 的 lifespan 会加载 BGE 模型 + 连 Qdrant。起 2 个 worker 就是
600MB+ 内存，而且跟限流毫无关系。这里只需要一个"被中间件包住的 200"。

## 为什么 window=3600

默认 60 秒窗口。测试期间跨过窗口边界计数会清零，结论就不可靠。
放大到 1 小时，保证所有请求落在同一窗口内。

用法：
    python -m uvicorn scripts.rl_min_app:app --port 8901 --workers 2
"""

import os

from app.api.auth import AccessControl


async def _echo(scope, receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


app = AccessControl(
    _echo,
    token=os.getenv("RL_TOKEN", "rl-worker-test"),
    rate_limit_per_minute=int(os.getenv("RL_LIMIT", "20")),
    window=3600.0,
)
