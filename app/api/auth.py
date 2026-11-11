"""app/api/auth.py — 公网访问控制：共享口令 + 每 IP 限流。

## 为什么必须有

`/qa/ask` 背后是 DeepSeek 的**按量计费**接口。服务一旦挂到公网，
扫描器通常在几小时内就能发现它。没有口令意味着：

- 别人可以拿你的 key 刷账单（钱是不可控的）
- `/documents/upload` 谁都能往知识库里塞东西
- `/qa/ask` 谁都能读到你的知识库内容

内网跑无所谓，公网跑是必须项。

## 为什么是纯 ASGI 中间件，而不是 BaseHTTPMiddleware

`BaseHTTPMiddleware` 会包一层 `send`/`receive`，把响应体先接进自己的缓冲
再转发。对这条链路是**致命的**：

    /qa/ask/stream 是 SSE，逐个 token yield

响应体一旦被中间件接管，token 会攒在中间件里不往下发，
"流式"就退化成"跑完一次性返回"——正是前端那个 8 段长度采样的测试
一直在盯的东西。

纯 ASGI 中间件只在 `scope` 上做判断，`send` 原样透传，一个字节都不碰。

## 顺序：先限流，后鉴权

反过来（先鉴权后限流）的话，暴力猜口令的请求**不消耗配额**，
等于给攻击者开了免费的重试通道。现在的顺序让爆破也受限。

代价：白名单路径（`/health`、`/`）同样占配额。前端每 15 秒轮询一次
`/health`，一分钟 4 次，相对 60 的额度可以忽略。

## 已知边界

限流状态在**进程内存**里：

- 多 worker 部署时每个 worker 各算各的，实际额度 = 配置值 × worker 数
- 进程重启后清零

真要跨进程限流得放 Redis。对"给简历挂个演示站"来说这是过度设计。
这个边界和 `harness/memory.py` 的并发问题同源：v1 承认它，不假装解决。

## 口令放在哪

`.env` 的 `ACCESS_TOKEN`，**不进镜像、不进 git**（`.dockerignore` 和
`.gitignore` 都已排除 `.env`），compose 用 `env_file` 注入。
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_HEADER = "x-access-token"

# 不需要口令的路径。
#
# `/` 必须放行：不然页面都打不开，用户没地方输入口令。
# `/health` 必须放行：探活工具和前端状态灯都要用它。
# `/static/` 留着给以后的静态资源（现在是单文件 HTML）。
#
# 反过来说，`/openapi.json`、`/docs` **故意不在白名单**里 ——
# 公网上暴露完整接口清单等于给扫描器送地图。
_PUBLIC_EXACT = {"/", "/health", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/static/",)


class RateLimiter:
    """滑动窗口限流：同一个 key 在 window 秒内最多 limit 次。

    用 deque 存时间戳而不是计数 + 定时重置，是因为固定窗口有边界问题：
    第 59 秒打满 60 次、第 61 秒又能打 60 次，实际是 2 秒内 120 次。
    """

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]

        # 丢掉窗口外的旧记录
        while q and now - q[0] >= self.window:
            q.popleft()

        if len(q) >= self.limit:
            return False

        q.append(now)

        # 粗粒度清理：字典太大时扫一遍，删掉已经空掉的 key。
        # 不做的话，每个来过的 IP 都会永久留一个空 deque。
        if len(self._hits) > 2048:
            for k in [k for k, v in self._hits.items() if not v]:
                del self._hits[k]

        return True


class AccessControl:
    """口令 + 限流。

    `token` 为空时整个中间件直通 —— 本地开发和单元测试不需要背口令。
    这个默认值有风险（忘配 = 裸奔），所以构造时打一行 WARN，
    部署后看日志就能发现。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str = "",
        rate_limit_per_minute: int = 60,
        window: float = 60.0,
    ) -> None:
        self.app = app
        self.token = token
        self.limiter = RateLimiter(rate_limit_per_minute, window) if token else None

        if token:
            print(f"[AUTH] 已启用访问口令，限流 {rate_limit_per_minute} 次/分钟/IP")
        else:
            print("[WARN] ACCESS_TOKEN 为空：接口无鉴权，仅适合本地开发")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 只拦 HTTP。lifespan / websocket 走原路（我们没有 websocket，
        # 但写全一点，以后加的时候不会静默绕过鉴权）。
        if self.token and scope["type"] == "http":
            client = scope.get("client")
            ip = client[0] if client else "unknown"

            # ① 先限流：猜口令也要消耗配额
            if self.limiter and not self.limiter.allow(ip):
                await self._reject(
                    scope, receive, send,
                    429,
                    f"请求过于频繁：每 IP 每分钟最多 {self.limiter.limit} 次",
                )
                return

            # ② 白名单直通（只免口令，不免限流）
            path = scope.get("path", "")
            if path not in _PUBLIC_EXACT and not path.startswith(_PUBLIC_PREFIXES):
                provided = self._header(scope, TOKEN_HEADER)
                # compare_digest 而不是 ==：`==` 在第一个不同字节就返回，
                # 攻击者能靠响应耗时一个字节一个字节地把口令猜出来。
                if not hmac.compare_digest(provided, self.token):
                    await self._reject(scope, receive, send, 401, "缺少或错误的访问口令")
                    return

        await self.app(scope, receive, send)

    @staticmethod
    def _header(scope: Scope, name: str) -> str:
        """从 ASGI scope 里取 header。

        不用 `Headers(scope=scope)` 也能做，但那样会构造一个 Mapping，
        这里只需要一个值，直接遍历更直白。
        """
        target = name.lower().encode("latin-1")
        for key, value in scope.get("headers", []):
            if key.lower() == target:
                return value.decode("latin-1")
        return ""

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, status: int, detail: str
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status)
        await response(scope, receive, send)
