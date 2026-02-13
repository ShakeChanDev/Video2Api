"""进程内 Worker：从 SQLite 队列领取任务并执行。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from uuid import uuid4

from app.db.sqlite import sqlite_db
from app.services.ixbrowser_service import ixbrowser_service
from app.services.sora_nurture_service import sora_nurture_service
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)


class WorkerRunner:
    STALE_RUNNING_REASON = "worker interrupted/recovered stale running job"

    def __init__(self) -> None:
        self.owner = f"worker-{uuid4().hex[:8]}"
        self._stop_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._sora_loop_task: Optional[asyncio.Task] = None
        self._nurture_loop_task: Optional[asyncio.Task] = None
        self._sora_running: Dict[int, asyncio.Task] = {}
        self._nurture_running: Optional[asyncio.Task] = None
        self._sora_lease_seconds = 120
        self._nurture_lease_seconds = 180

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                self._log_event(
                    action="worker.start.skipped",
                    event="skip",
                    status="success",
                    level="INFO",
                    message="Worker 已启动，忽略重复 start",
                    metadata={"owner": self.owner},
                )
                return
            self._stop_event.clear()
            self._started = True
            try:
                sora_failed_cnt = sqlite_db.fail_stale_running_sora_jobs()
                nurture_cnt = sqlite_db.requeue_stale_sora_nurture_batches()
                self._log_event(
                    action="worker.start",
                    event="start",
                    status="success",
                    level="INFO",
                    message=f"Worker 启动，恢复任务 sora_failed={sora_failed_cnt} nurture={nurture_cnt}",
                    metadata={
                        "owner": self.owner,
                        "sora_failed_recovered": sora_failed_cnt,
                        "nurture_requeued": nurture_cnt,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception("Worker 启动恢复失败")
                self._log_event(
                    action="worker.start",
                    event="start",
                    status="failed",
                    level="WARN",
                    message="Worker 启动恢复失败",
                    metadata={"owner": self.owner},
                )

            self._sora_loop_task = spawn(
                self._sora_loop(),
                task_name="worker.sora.loop",
                metadata={"owner": self.owner},
            )
            self._nurture_loop_task = spawn(
                self._nurture_loop(),
                task_name="worker.nurture.loop",
                metadata={"owner": self.owner},
            )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                self._log_event(
                    action="worker.stop.skipped",
                    event="skip",
                    status="success",
                    level="INFO",
                    message="Worker 未启动，忽略 stop",
                    metadata={"owner": self.owner},
                )
                return
            self._stop_event.set()
            wait_tasks = []
            if self._sora_loop_task and not self._sora_loop_task.done():
                self._sora_loop_task.cancel()
                wait_tasks.append(self._sora_loop_task)
            if self._nurture_loop_task and not self._nurture_loop_task.done():
                self._nurture_loop_task.cancel()
                wait_tasks.append(self._nurture_loop_task)
            for task in list(self._sora_running.values()):
                if not task.done():
                    task.cancel()
                    wait_tasks.append(task)
            if self._nurture_running and not self._nurture_running.done():
                self._nurture_running.cancel()
                wait_tasks.append(self._nurture_running)
            if wait_tasks:
                await asyncio.gather(*wait_tasks, return_exceptions=True)
            self._sora_loop_task = None
            self._nurture_loop_task = None
            self._sora_running.clear()
            self._nurture_running = None
            self._started = False
            self._log_event(
                action="worker.stop",
                event="stop",
                status="success",
                level="INFO",
                message="Worker 已停止",
                metadata={"owner": self.owner},
            )

    async def _sora_loop(self) -> None:
        while not self._stop_event.is_set():
            # 清理已完成任务
            done_ids = [job_id for job_id, task in self._sora_running.items() if task.done()]
            for job_id in done_ids:
                self._sora_running.pop(job_id, None)

            max_parallel = max(
                1, int(getattr(ixbrowser_service, "sora_job_max_concurrency", 2) or 2)
            )
            while len(self._sora_running) < max_parallel:
                try:
                    row = sqlite_db.claim_next_sora_job(
                        owner=self.owner, lease_seconds=self._sora_lease_seconds
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log_event(
                        action="worker.sora.claim",
                        event="claim",
                        status="failed",
                        level="WARN",
                        message=f"Sora 任务领取失败: {exc}",
                        metadata={"owner": self.owner, "error": str(exc)},
                    )
                    break
                if not row:
                    break
                job_id = int(row.get("id") or 0)
                if job_id <= 0:
                    self._log_event(
                        action="worker.sora.claim",
                        event="claim",
                        status="failed",
                        level="WARN",
                        message="Sora 任务领取返回非法 job_id",
                        metadata={"owner": self.owner, "row": row},
                    )
                    break
                task = spawn(
                    self._run_one_sora_job(job_id),
                    task_name="worker.sora.run_one",
                    metadata={"owner": self.owner, "job_id": job_id},
                )
                self._sora_running[job_id] = task

            await asyncio.sleep(1.0)

    async def _run_one_sora_job(self, job_id: int) -> None:
        hb = spawn(
            self._heartbeat_sora_job(job_id),
            task_name="worker.sora.heartbeat",
            metadata={"owner": self.owner, "job_id": job_id},
        )
        try:
            await ixbrowser_service.run_sora_job(job_id)
        except asyncio.CancelledError:
            row = sqlite_db.get_sora_job(job_id) or {}
            status = str(row.get("status") or "").strip().lower()
            if status not in {"completed", "failed", "canceled"}:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                phase = str(row.get("phase") or "submit")
                sqlite_db.update_sora_job(
                    job_id,
                    {
                        "status": "failed",
                        "phase": phase,
                        "error": self.STALE_RUNNING_REASON,
                        "run_last_error": self.STALE_RUNNING_REASON,
                        "finished_at": now,
                    },
                )
                sqlite_db.create_sora_job_event(job_id, phase, "fail", self.STALE_RUNNING_REASON)
            self._log_event(
                action="worker.sora.run",
                event="canceled",
                status="failed",
                level="WARN",
                message="Sora 任务执行被取消，已标记失败",
                metadata={"owner": self.owner, "job_id": int(job_id)},
            )
            raise
        except Exception as exc:  # noqa: BLE001
            sqlite_db.update_sora_job(job_id, {"run_last_error": str(exc)})
            self._log_event(
                action="worker.sora.run",
                event="fail",
                status="failed",
                level="WARN",
                message=f"Sora 任务执行失败: {exc}",
                metadata={"owner": self.owner, "job_id": int(job_id), "error": str(exc)},
            )
            raise
        finally:
            hb.cancel()
            await asyncio.gather(hb, return_exceptions=True)
            cleared = sqlite_db.clear_sora_job_lease(job_id=job_id, owner=self.owner)
            if not cleared:
                self._log_event(
                    action="worker.sora.lease.clear",
                    event="clear",
                    status="failed",
                    level="WARN",
                    message="Sora 任务租约清理失败",
                    metadata={"owner": self.owner, "job_id": int(job_id)},
                )

    async def _heartbeat_sora_job(self, job_id: int) -> None:
        while not self._stop_event.is_set():
            ok = sqlite_db.heartbeat_sora_job_lease(
                job_id=job_id,
                owner=self.owner,
                lease_seconds=self._sora_lease_seconds,
            )
            if not ok:
                self._log_event(
                    action="worker.sora.heartbeat",
                    event="lost",
                    status="failed",
                    level="WARN",
                    message="Sora 任务心跳丢失，停止续租",
                    metadata={"owner": self.owner, "job_id": int(job_id)},
                )
                return
            await asyncio.sleep(max(5, int(self._sora_lease_seconds // 3)))

    async def _nurture_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._nurture_running and not self._nurture_running.done():
                await asyncio.sleep(1.0)
                continue

            try:
                row = sqlite_db.claim_next_sora_nurture_batch(
                    owner=self.owner, lease_seconds=self._nurture_lease_seconds
                )
            except Exception as exc:  # noqa: BLE001
                self._log_event(
                    action="worker.nurture.claim",
                    event="claim",
                    status="failed",
                    level="WARN",
                    message=f"养号批次领取失败: {exc}",
                    metadata={"owner": self.owner, "error": str(exc)},
                )
                await asyncio.sleep(1.0)
                continue
            if not row:
                await asyncio.sleep(1.0)
                continue

            batch_id = int(row.get("id") or 0)
            if batch_id <= 0:
                await asyncio.sleep(0.5)
                continue
            self._nurture_running = spawn(
                self._run_one_nurture_batch(batch_id),
                task_name="worker.nurture.run_one",
                metadata={"owner": self.owner, "batch_id": batch_id},
            )

    async def _run_one_nurture_batch(self, batch_id: int) -> None:
        hb = spawn(
            self._heartbeat_nurture_batch(batch_id),
            task_name="worker.nurture.heartbeat",
            metadata={"owner": self.owner, "batch_id": batch_id},
        )
        try:
            await sora_nurture_service._run_batch_impl(batch_id)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            sqlite_db.update_sora_nurture_batch(batch_id, {"error": str(exc)})
            self._log_event(
                action="worker.nurture.run",
                event="fail",
                status="failed",
                level="WARN",
                message=f"养号批次执行失败: {exc}",
                metadata={"owner": self.owner, "batch_id": int(batch_id), "error": str(exc)},
            )
            raise
        finally:
            hb.cancel()
            await asyncio.gather(hb, return_exceptions=True)
            cleared = sqlite_db.clear_sora_nurture_batch_lease(batch_id=batch_id, owner=self.owner)
            if not cleared:
                self._log_event(
                    action="worker.nurture.lease.clear",
                    event="clear",
                    status="failed",
                    level="WARN",
                    message="养号批次租约清理失败",
                    metadata={"owner": self.owner, "batch_id": int(batch_id)},
                )

    async def _heartbeat_nurture_batch(self, batch_id: int) -> None:
        while not self._stop_event.is_set():
            ok = sqlite_db.heartbeat_sora_nurture_batch_lease(
                batch_id=batch_id,
                owner=self.owner,
                lease_seconds=self._nurture_lease_seconds,
            )
            if not ok:
                self._log_event(
                    action="worker.nurture.heartbeat",
                    event="lost",
                    status="failed",
                    level="WARN",
                    message="养号批次心跳丢失，停止续租",
                    metadata={"owner": self.owner, "batch_id": int(batch_id)},
                )
                return
            await asyncio.sleep(max(5, int(self._nurture_lease_seconds // 3)))

    def _log_event(
        self,
        *,
        action: str,
        event: str,
        status: str,
        level: str,
        message: str,
        metadata: Optional[dict] = None,
    ) -> None:
        try:
            sqlite_db.create_event_log(
                source="system",
                action=action,
                event=event,
                status=status,
                level=level,
                message=message,
                metadata=metadata or {},
            )
        except Exception:  # noqa: BLE001
            logger.exception("写入 worker 事件日志失败: %s", action)


worker_runner = WorkerRunner()
