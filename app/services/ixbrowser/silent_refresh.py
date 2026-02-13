"""ixBrowser 静默更新任务逻辑。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import IXBrowserSilentRefreshCreateResponse, IXBrowserSilentRefreshJob
from app.services.ixbrowser.errors import IXBrowserNotFoundError, IXBrowserServiceError
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)


class SilentRefreshMixin:
    def _calc_progress_pct(self, processed_windows: int, total_windows: int) -> float:
        processed = max(int(processed_windows or 0), 0)
        total = max(int(total_windows or 0), 1)
        pct = (processed / total) * 100
        return round(max(min(pct, 100.0), 0.0), 2)

    def _build_silent_refresh_job(self, row: Dict[str, Any]) -> IXBrowserSilentRefreshJob:
        return IXBrowserSilentRefreshJob(
            job_id=int(row.get("id") or 0),
            group_title=str(row.get("group_title") or ""),
            status=str(row.get("status") or "queued"),
            total_windows=int(row.get("total_windows") or 0),
            processed_windows=int(row.get("processed_windows") or 0),
            success_count=int(row.get("success_count") or 0),
            failed_count=int(row.get("failed_count") or 0),
            progress_pct=float(row.get("progress_pct") or 0),
            current_profile_id=int(row["current_profile_id"])
            if row.get("current_profile_id") is not None
            else None,
            current_window_name=row.get("current_window_name"),
            message=row.get("message"),
            error=row.get("error"),
            run_id=int(row["run_id"]) if row.get("run_id") is not None else None,
            with_fallback=bool(row.get("with_fallback")),
            operator_user_id=int(row["operator_user_id"])
            if row.get("operator_user_id") is not None
            else None,
            operator_username=row.get("operator_username"),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            finished_at=str(row.get("finished_at")) if row.get("finished_at") else None,
        )

    def get_silent_refresh_job(self, job_id: int) -> IXBrowserSilentRefreshJob:
        row = sqlite_db.get_ixbrowser_silent_refresh_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到静默更新任务：{job_id}")
        return self._build_silent_refresh_job(row)

    async def start_silent_refresh(
        self,
        group_title: str = "Sora",
        operator_user: Optional[dict] = None,
        with_fallback: bool = True,
    ) -> IXBrowserSilentRefreshCreateResponse:
        normalized_group = str(group_title or "Sora").strip() or "Sora"
        with_fallback_bool = bool(with_fallback)
        operator_user_id = operator_user.get("id") if isinstance(operator_user, dict) else None
        operator_username = (
            operator_user.get("username") if isinstance(operator_user, dict) else None
        )

        running = sqlite_db.get_running_ixbrowser_silent_refresh_job(normalized_group)
        if running:
            sqlite_db.create_event_log(
                source="ixbrowser",
                action="ixbrowser.silent_refresh.reused",
                event="reused",
                status="success",
                level="INFO",
                message="复用运行中静默更新任务",
                resource_type="ixbrowser_silent_refresh_job",
                resource_id=str(running.get("id")),
                operator_user_id=operator_user_id,
                operator_username=operator_username,
                metadata={
                    "group_title": normalized_group,
                    "with_fallback": with_fallback_bool,
                },
            )
            return IXBrowserSilentRefreshCreateResponse(
                job=self._build_silent_refresh_job(running),
                reused=True,
            )

        job_id = sqlite_db.create_ixbrowser_silent_refresh_job(
            {
                "group_title": normalized_group,
                "status": "queued",
                "with_fallback": with_fallback_bool,
                "message": "任务已创建，等待执行",
                "operator_user_id": operator_user_id,
                "operator_username": operator_username,
            }
        )
        row = sqlite_db.get_ixbrowser_silent_refresh_job(job_id)
        if not row:
            raise IXBrowserServiceError(f"静默更新任务创建失败：{job_id}")

        sqlite_db.create_event_log(
            source="ixbrowser",
            action="ixbrowser.silent_refresh.start",
            event="start",
            status="success",
            level="INFO",
            message="静默更新任务已启动",
            resource_type="ixbrowser_silent_refresh_job",
            resource_id=str(job_id),
            operator_user_id=operator_user_id,
            operator_username=operator_username,
            metadata={
                "group_title": normalized_group,
                "with_fallback": with_fallback_bool,
            },
        )

        spawn(
            self._run_silent_refresh_job(
                job_id=job_id,
                group_title=normalized_group,
                with_fallback=with_fallback_bool,
                operator_user=operator_user,
            ),
            task_name="ixbrowser.silent_refresh.run",
            metadata={"job_id": int(job_id), "group_title": normalized_group},
        )
        return IXBrowserSilentRefreshCreateResponse(
            job=self._build_silent_refresh_job(row), reused=False
        )

    async def _run_silent_refresh_job(
        self,
        job_id: int,
        group_title: str,
        with_fallback: bool = True,
        operator_user: Optional[dict] = None,
    ) -> None:
        logger.info(
            "静默更新任务开始 | job_id=%s | 分组=%s | with_fallback=%s",
            int(job_id),
            str(group_title),
            bool(with_fallback),
        )
        sqlite_db.update_ixbrowser_silent_refresh_job(
            job_id,
            {
                "status": "running",
                "message": "开始静默更新账号信息",
                "error": None,
            },
        )

        def _apply_progress(payload: Dict[str, Any]) -> None:
            patch: Dict[str, Any] = {
                "status": str(payload.get("status") or "running"),
                "total_windows": int(payload.get("total_windows") or 0),
                "processed_windows": int(payload.get("processed_windows") or 0),
                "success_count": int(payload.get("success_count") or 0),
                "failed_count": int(payload.get("failed_count") or 0),
                "progress_pct": float(payload.get("progress_pct") or 0),
                "current_profile_id": payload.get("current_profile_id"),
                "current_window_name": payload.get("current_window_name"),
                "message": payload.get("message"),
            }
            if "run_id" in payload:
                patch["run_id"] = payload.get("run_id")
            if "error" in payload:
                patch["error"] = payload.get("error")
            sqlite_db.update_ixbrowser_silent_refresh_job(job_id, patch)

        try:
            response = await self.scan_group_sora_sessions_silent_api(
                group_title=group_title,
                operator_user=operator_user,
                with_fallback=with_fallback,
                progress_callback=_apply_progress,
            )
            sqlite_db.update_ixbrowser_silent_refresh_job(
                job_id,
                {
                    "status": "completed",
                    "total_windows": int(response.total_windows),
                    "processed_windows": int(response.total_windows),
                    "success_count": int(response.success_count),
                    "failed_count": int(response.failed_count),
                    "progress_pct": self._calc_progress_pct(
                        response.total_windows, response.total_windows
                    ),
                    "current_profile_id": None,
                    "current_window_name": None,
                    "message": "静默更新完成",
                    "error": None,
                    "run_id": response.run_id,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            sqlite_db.create_event_log(
                source="ixbrowser",
                action="ixbrowser.silent_refresh.finish",
                event="finish",
                status="success",
                level="INFO",
                message="静默更新任务完成",
                resource_type="ixbrowser_silent_refresh_job",
                resource_id=str(job_id),
                operator_user_id=operator_user.get("id")
                if isinstance(operator_user, dict)
                else None,
                operator_username=operator_user.get("username")
                if isinstance(operator_user, dict)
                else None,
                metadata={
                    "group_title": group_title,
                    "run_id": response.run_id,
                    "total_windows": response.total_windows,
                    "success_count": response.success_count,
                    "failed_count": response.failed_count,
                },
            )
            logger.info(
                "静默更新任务完成 | job_id=%s | 分组=%s | run_id=%s | total=%s | success=%s | failed=%s",
                int(job_id),
                str(group_title),
                int(response.run_id) if response.run_id is not None else None,
                int(response.total_windows),
                int(response.success_count),
                int(response.failed_count),
            )
        except Exception as exc:  # noqa: BLE001
            sqlite_db.update_ixbrowser_silent_refresh_job(
                job_id,
                {
                    "status": "failed",
                    "message": "静默更新失败",
                    "error": str(exc),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            sqlite_db.create_event_log(
                source="ixbrowser",
                action="ixbrowser.silent_refresh.fail",
                event="fail",
                status="failed",
                level="ERROR",
                message=f"静默更新任务失败: {exc}",
                resource_type="ixbrowser_silent_refresh_job",
                resource_id=str(job_id),
                operator_user_id=operator_user.get("id")
                if isinstance(operator_user, dict)
                else None,
                operator_username=operator_user.get("username")
                if isinstance(operator_user, dict)
                else None,
                metadata={
                    "group_title": group_title,
                },
            )
            logger.exception(
                "静默更新任务失败 | job_id=%s | 分组=%s | 错误=%s",
                int(job_id),
                str(group_title),
                str(exc),
            )
