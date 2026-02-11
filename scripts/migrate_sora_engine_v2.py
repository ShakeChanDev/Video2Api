"""Sora Engine v2 一次性迁移脚本。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.db.sqlite import sqlite_db


def main() -> None:
    print("[1/6] 初始化/升级 schema ...")
    sqlite_db._init_db()  # noqa: SLF001

    print("[2/6] 清理过期 profile 运行锁 ...")
    released = sqlite_db.release_expired_profile_runtime_locks()
    print(f"  - released locks: {released}")

    print("[3/6] 将未完成任务标记为切换失败 ...")
    aborted = sqlite_db.mark_unfinished_sora_jobs_engine_cutover("engine_cutover_abort")
    print(f"  - aborted jobs: {aborted}")

    print("[4/6] 回填 engine_version=v2 默认值 ...")
    defaults = sqlite_db.ensure_sora_engine_v2_default()
    print(f"  - updated jobs: {defaults}")

    print("[5/6] 回填最近 7 天 timeline ...")
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    backfilled = sqlite_db.backfill_sora_timeline_from_events(since)
    print(f"  - timeline backfilled: {backfilled}")

    print("[6/6] 完成。请重启后端与 worker。")


if __name__ == "__main__":
    main()
