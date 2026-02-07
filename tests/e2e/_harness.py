import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple

import httpx
import pytest
import uvicorn

from app.db.sqlite import sqlite_db

REPO_ROOT = Path(__file__).resolve().parents[2]


def require_admin_dist_or_skip() -> Path:
    index = REPO_ROOT / "admin" / "dist" / "index.html"
    if not index.exists():
        pytest.skip("跳过 E2E：未找到 admin/dist/index.html（请先 make admin-build）")
    return index


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def wait_http_ok(url: str, timeout_sec: float = 10.0) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    last_exc = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code < 500:
                return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(0.15)
    raise RuntimeError(f"服务未就绪：{url} last_exc={last_exc}")


def is_headless() -> bool:
    raw = str(os.getenv("PW_HEADLESS") or "").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    return True


@contextmanager
def temp_sqlite_db(tmp_path: Path, filename: str) -> Iterator[Path]:
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / filename
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path


def start_uvicorn(app, port: int, host: str = "127.0.0.1") -> Tuple[uvicorn.Server, threading.Thread, str]:
    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://{host}:{int(port)}"
    wait_http_ok(f"{base_url}/health", timeout_sec=12.0)
    return server, thread, base_url


def stop_uvicorn(server: uvicorn.Server, thread: threading.Thread, timeout_sec: float = 10.0) -> None:
    try:
        server.should_exit = True
    except Exception:
        pass
    try:
        thread.join(timeout=max(1.0, float(timeout_sec)))
    except Exception:
        pass

