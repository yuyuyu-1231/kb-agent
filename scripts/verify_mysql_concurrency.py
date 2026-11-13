r"""并发建单验证：N 个线程同时 create，统计主键冲突。

## 为什么要这个脚本

`MySQLTicketStore._next_id` 的旧实现是「查最大序号 + 1」：

    SELECT id FROM tickets WHERE id LIKE 'T-20261113-%' ORDER BY id DESC LIMIT 1

**MySQL 的普通 SELECT 是快照读，不加锁**。两个事务可以同时读到 `0003`，
然后都去插 `0004` —— 第二个撞主键。

单元测试测不出来（串行执行），只有真并发才能暴露。

## 怎么跑

    # 基线（旧实现）
    MYSQL_POOL_SIZE=20 python scripts/verify_mysql_concurrency.py

    # 改完之后再跑一次，期望 0 冲突

## 一个容易忽略的点

连接池大小必须 >= 线程数，否则请求会在池里排队，真正的并发度被池大小限制住，
测出来的是「池的串行化」而不是「数据库的竞态」。
"""

from __future__ import annotations

import os
import sys
import threading
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_BACKEND", "mysql")
os.environ.setdefault("MYSQL_HOST", os.getenv("MYSQL_HOST", "127.0.0.1"))
os.environ.setdefault("MYSQL_PORT", os.getenv("MYSQL_PORT", "3306"))
os.environ.setdefault("MYSQL_USER", os.getenv("MYSQL_USER", "kb"))
os.environ.setdefault("MYSQL_PASSWORD", os.getenv("MYSQL_PASSWORD", "kbpass"))
os.environ.setdefault("MYSQL_DATABASE", os.getenv("MYSQL_DATABASE", "kb_agent"))

from app.core.config import get_settings  # noqa: E402
from app.infra.db_stores import build_ticket_store, init_backend  # noqa: E402

N = int(os.getenv("CONCURRENCY", "20"))


def main() -> int:
    settings = get_settings()
    print(f"backend={settings.db_backend}  pool_size={settings.mysql_pool_size}  线程数={N}")

    backend, ready = init_backend(settings)
    if not ready:
        print("[FAIL] MySQL 未就绪")
        return 1

    store = build_ticket_store(settings, backend)
    before = store.stats().get("total", 0)

    lock = threading.Lock()
    ok: list[str] = []
    errors: Counter = Counter()
    err_samples: dict[str, str] = {}
    start = threading.Barrier(N)          # 让 20 个线程尽量同时出发

    def worker(i: int) -> None:
        try:
            start.wait(timeout=10)        # 等所有人就位，最大化并发
        except threading.BrokenBarrierError:
            pass
        try:
            t = store.create(
                title=f"并发验证工单 {i:02d}",
                category="其他",
                priority="P3",
                summary="verify_mysql_concurrency",
                session_id="concurrency-test",
            )
            with lock:
                ok.append(t.get("id", "?"))
        except Exception as exc:          # noqa: BLE001
            name = type(exc).__name__
            with lock:
                errors[name] += 1
                err_samples.setdefault(name, str(exc)[:160])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    after = store.stats().get("total", 0)

    print("\n" + "=" * 60)
    print(f"成功建单: {len(ok)} / {N}")
    print(f"库内新增: {after - before} 条")
    if errors:
        print("失败统计:")
        for name, cnt in errors.most_common():
            print(f"  {name}: {cnt}")
            print(f"    样例: {err_samples[name]}")
    else:
        print("失败统计: 无")

    ids = Counter(ok)
    dup = [i for i, c in ids.items() if c > 1]
    if dup:
        print(f"!! 成功返回里出现重复单号: {dup}")

    ok_all = len(ok) == N and after - before == N and not errors
    print("\n" + ("[PASS] 并发下零冲突" if ok_all else "[FAIL] 存在冲突或丢失"))
    backend.close_all()
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
