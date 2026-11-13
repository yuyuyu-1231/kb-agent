r"""限流三项实测（可复现）。

    python scripts/verify_rate_limit.py

## 测什么

① 同一 IP 连续请求，第 21 次是否 429；响应头和 body 长什么样
② 不同 IP 是否各自独立计数
③ `uvicorn --workers 2` 时额度是否真的翻倍

## 为什么 ①② 直调中间件

要测「不同 IP 各自计数」必须能控制 `scope["client"]`。
走真实 HTTP 的话源 IP 永远是 127.0.0.1，除非改 nginx。

## 为什么 ③ 必须起真进程

进程内计数是**每个进程各一份**。不真的起 2 个 worker，就测不出这个边界。

## 一个必须注意的坑

探活**不能用 HTTP**：`/health` 在白名单里只免口令、不免限流，
用 HTTP 探活会吃掉 1 次配额 —— 实测会出现「1 worker 只过 19 次」的假失败。
所以 `wait_ready` 只探 TCP 端口。
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.auth import AccessControl  # noqa: E402

TOKEN = "rl-test-token"
LIMIT = 20
PORT = 8901
TOTAL = 100


# --------------------------------------------------------------------------- #
# ①②：直调中间件
# --------------------------------------------------------------------------- #

async def _echo(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(mw, ip: str) -> tuple[int, dict[str, str], str]:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/documents",
        "query_string": b"",
        "headers": [(b"x-access-token", TOKEN.encode())],
        "client": (ip, 54321),
    }
    out = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        out.append(msg)

    await mw(scope, receive, send)
    start = next((m for m in out if m["type"] == "http.response.start"), None)
    if start is None:
        return 0, {}, ""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return start["status"], headers, body.decode("utf-8", "replace")


async def check_same_ip() -> tuple[bool, dict]:
    print("=" * 68)
    print(f"① 同一 IP 连续请求（额度 {LIMIT}/分钟）")
    print("=" * 68)
    mw = AccessControl(_echo, token=TOKEN, rate_limit_per_minute=LIMIT)

    codes = []
    for i in range(1, LIMIT + 2):
        code, _h, _b = await _call(mw, "10.0.0.1")
        codes.append(code)
        if i in (1, LIMIT, LIMIT + 1):
            print(f"  第 {i:>2} 次: {code}")

    code, headers, body = await _call(mw, "10.0.0.1")
    print(f"\n  超额请求的完整响应：")
    print(f"    status : {code}")
    print(f"    headers: {json.dumps(headers, ensure_ascii=False)}")
    print(f"    body   : {body}")

    ok = codes == [200] * LIMIT + [429]
    print(f"\n  判定: {'PASS' if ok else 'FAIL'}（期望前 {LIMIT} 次 200、第 {LIMIT + 1} 次 429）")
    return ok, headers


async def check_per_ip() -> bool:
    print()
    print("=" * 68)
    print("② 不同 IP 各自独立计数")
    print("=" * 68)
    mw = AccessControl(_echo, token=TOKEN, rate_limit_per_minute=LIMIT)

    for _ in range(LIMIT):
        await _call(mw, "10.0.0.1")
    a_over, _h, _b = await _call(mw, "10.0.0.1")
    b_first, _h2, _b2 = await _call(mw, "10.0.0.2")
    for _ in range(LIMIT - 1):
        await _call(mw, "10.0.0.2")
    b_over, _h3, _b3 = await _call(mw, "10.0.0.2")

    print(f"  A 的第 {LIMIT + 1} 次 : {a_over}（期望 429）")
    print(f"  B 的第 1 次        : {b_first}（期望 200）")
    print(f"  B 的第 {LIMIT + 1} 次 : {b_over}（期望 429）")
    ok = a_over == 429 and b_first == 200 and b_over == 429
    print(f"  判定: {'PASS' if ok else 'FAIL'}")
    return ok


# --------------------------------------------------------------------------- #
# ③：多 worker
# --------------------------------------------------------------------------- #

def _start(workers: int, log_path: Path):
    # stdout 必须重定向到文件，不能用 subprocess.PIPE：
    # 父进程不读管道时，uvicorn 的日志会填满 64KB 缓冲区导致子进程阻塞。
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "scripts.rl_min_app:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--workers", str(workers)],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


def _wait_tcp(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _hit() -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/documents")
    req.add_header("X-Access-Token", "rl-worker-test")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def _stop(proc) -> None:
    if proc is None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _measure(workers: int, log_path: Path) -> dict:
    proc = None
    try:
        proc = _start(workers, log_path)
        if not _wait_tcp():
            return {"ok": False}
        time.sleep(1.5)
        codes = [_hit() for _ in range(TOTAL)]
        return {
            "ok": True,
            "200": codes.count(200),
            "429": codes.count(429),
            "other": [c for c in codes if c not in (200, 429)],
        }
    finally:
        _stop(proc)
        time.sleep(1.0)


def check_workers() -> bool:
    print()
    print("=" * 68)
    print(f"③ 多 worker 额度验证（单 worker 上限 {LIMIT}，共打 {TOTAL} 次）")
    print("=" * 68)
    log_path = ROOT / "data" / "reports" / "_rl_worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n[1 worker]")
    r1 = _measure(1, log_path)
    print(f"  200={r1.get('200')}  429={r1.get('429')}  other={r1.get('other')}")

    print("\n[2 workers]")
    r2 = _measure(2, log_path)
    print(f"  200={r2.get('200')}  429={r2.get('429')}  other={r2.get('other')}")

    if not (r1.get("ok") and r2.get("ok")):
        print("  FAIL: 服务未能启动")
        return False

    ok1 = r1["200"] == LIMIT
    ok2 = 35 <= r2["200"] <= 45
    print()
    print(f"  1 worker  通过 {r1['200']}（期望 {LIMIT}）: {'PASS' if ok1 else 'FAIL'}")
    print(f"  2 workers 通过 {r2['200']}（期望约 {LIMIT * 2}）: {'PASS' if ok2 else 'FAIL'}")
    if ok1 and ok2:
        print(f"  → 额度放大 {r2['200'] / r1['200']:.1f} 倍，证实「多进程各自计数」")
    return ok1 and ok2


async def main() -> int:
    ok1, headers = await check_same_ip()
    ok2 = await check_per_ip()
    ok3 = check_workers()

    print()
    print("=" * 68)
    print(f"① 同 IP 第 {LIMIT + 1} 次 429 : {'PASS' if ok1 else 'FAIL'}")
    print(f"② 不同 IP 独立计数    : {'PASS' if ok2 else 'FAIL'}")
    print(f"③ 多 worker 额度翻倍  : {'PASS' if ok3 else 'FAIL'}")
    print("=" * 68)
    print(f"429 响应头 = {sorted(headers)}")
    if "retry-after" not in headers:
        print("  注意：没有 Retry-After，客户端无法知道多久后重试")
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
