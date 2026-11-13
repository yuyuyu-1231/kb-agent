r"""MySQL 后端验证：写入 / 读取 / 降级。

## 为什么分成三个子命令

因为「重启容器后数据还在」这个验证必须跨进程：
写入进程退出 → 容器重启 → 另一个进程去读。
一个脚本跑完的话，永远是同一个连接池，测不出持久化。

    python scripts/verify_mysql.py write      # 建单 + 写两轮会话
    docker restart kb-mysql
    python scripts/verify_mysql.py read       # 校验数据还在
    docker stop kb-mysql
    python scripts/verify_mysql.py degraded   # 校验服务不崩

## 读环境的方式

不读 .env（里面是 DB_BACKEND=sqlite），直接用环境变量覆盖，
避免为了验证改项目配置。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 强制走 MySQL。放在 import 应用代码之前——Settings 是 lru_cache 单例，
# 晚设置就读不到。
os.environ.setdefault("DB_BACKEND", "mysql")
os.environ.setdefault("MYSQL_HOST", os.getenv("MYSQL_HOST", "127.0.0.1"))
os.environ.setdefault("MYSQL_PORT", os.getenv("MYSQL_PORT", "3306"))
os.environ.setdefault("MYSQL_USER", os.getenv("MYSQL_USER", "kb"))
os.environ.setdefault("MYSQL_PASSWORD", os.getenv("MYSQL_PASSWORD", "kbpass"))
os.environ.setdefault("MYSQL_DATABASE", os.getenv("MYSQL_DATABASE", "kb_agent"))

from app.core.config import get_settings  # noqa: E402
from app.infra.db_stores import (  # noqa: E402
    build_memory,
    build_ticket_store,
    init_backend,
)

STATE = ROOT / "data" / "mysql_verify_state.json"


def _print_header(title: str) -> None:
    print("=" * 64)
    print(title)
    print("=" * 64)


def cmd_write() -> int:
    settings = get_settings()
    backend, ready = init_backend(settings)
    print(f"backend={settings.db_backend}  ready={ready}")
    if not ready:
        print("[FAIL] 建表失败，MySQL 可能没起来")
        return 1

    tickets = build_ticket_store(settings, backend)
    memory = build_memory(settings, backend)

    t = tickets.create(
        title="MySQL 后端验证工单",
        category="故障报修",
        priority="P2",
        summary="由 verify_mysql.py 创建，用于验证持久化",
        evidence=json.dumps({"queries": ["验证"], "hits": 0}, ensure_ascii=False),
        session_id="verify-session-1",
    )
    print(f"建单: {t['id']}  status={t['status']}  category={t['category']}")

    memory.ensure("verify-session-1")
    memory.append(
        "verify-session-1",
        {"role": "user", "content": "年假有几天？"},
        {"role": "assistant", "content": "满 1 年不满 10 年 5 天。"},
    )
    memory.append(
        "verify-session-1",
        {"role": "user", "content": "那病假呢？"},
        {"role": "assistant", "content": "病假工资按 80% 发放。"},
    )
    history = memory.load("verify-session-1")
    print(f"写入会话: {len(history)} 条消息")

    stats_t = tickets.stats()
    stats_m = memory.stats()
    print(f"工单 stats: {stats_t}")
    print(f"会话 stats: {stats_m}")

    STATE.write_text(
        json.dumps({"ticket_id": t["id"], "session_id": "verify-session-1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n状态已存至 {STATE.name}，下一步：docker restart kb-mysql")
    return 0


def cmd_read() -> int:
    settings = get_settings()
    backend, ready = init_backend(settings)
    if not ready:
        print("[FAIL] 数据库不可用")
        return 1

    if not STATE.exists():
        print(f"[FAIL] 没找到 {STATE.name}，先跑 write")
        return 1
    state = json.loads(STATE.read_text(encoding="utf-8"))

    tickets = build_ticket_store(settings, backend)
    memory = build_memory(settings, backend)

    t = tickets.get(state["ticket_id"])
    history = memory.load(state["session_id"])

    ok = True
    if not t:
        print(f"[FAIL] 工单 {state['ticket_id']} 读不到")
        ok = False
    else:
        print(f"工单还在: {t['id']}  {t['title']}  status={t['status']}")

    if len(history) != 4:
        print(f"[FAIL] 会话应为 4 条，实际 {len(history)}")
        ok = False
    else:
        print(f"会话还在: {len(history)} 条，最后一条 = {history[-1]['content'][:20]}")

    print("\n" + ("[OK] 重启后数据完好" if ok else "[FAIL] 数据丢失"))
    return 0 if ok else 1


def cmd_degraded() -> int:
    """MySQL 停掉时调这个：要求不抛异常、返回带 _error 的结构。"""
    settings = get_settings()
    backend, ready = init_backend(settings)
    print(f"init_backend ready={ready}（预期 False 或连接失败）")

    tickets = build_ticket_store(settings, backend)
    memory = build_memory(settings, backend)

    # 关键：这三个调用都不能抛
    st = tickets.stats()
    sm = memory.stats()
    lst = tickets.list()
    hist = memory.load("verify-session-1")

    print(f"tickets.stats()  → {st}")
    print(f"memory.stats()   → {sm}")
    print(f"tickets.list()   → {lst}")
    print(f"memory.load()    → {hist}")

    ok = (
        st.get("_error") and sm.get("_error") and lst == [] and hist == []
    )
    print("\n" + ("[OK] 降级正常：全部返回空值 + _error，没有抛异常" if ok else "[FAIL] 降级行为不符合预期"))
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/verify_mysql.py write|read|degraded")
        return 1
    cmd = sys.argv[1]
    if cmd == "write":
        return cmd_write()
    if cmd == "read":
        return cmd_read()
    if cmd == "degraded":
        return cmd_degraded()
    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
