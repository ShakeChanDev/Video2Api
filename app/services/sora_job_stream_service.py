"""Sora 任务 SSE 流服务：快照差分与阶段事件筛选。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import SoraJob, SoraJobEvent
from app.services.ixbrowser_service import ixbrowser_service


@dataclass(frozen=True)
class SoraJobStreamFilter:
    group_title: Optional[str] = None
    profile_id: Optional[int] = None
    status: Optional[str] = None
    phase: Optional[str] = None
    keyword: Optional[str] = None
    limit: int = 100


class SoraJobStreamService:
    """管理 Sora 任务流式推送的快照与增量计算。"""

    # 允许测试中 monkeypatch 调小间隔，避免等待过久。
    poll_interval_seconds: float = 1.0
    ping_interval_seconds: float = 25.0
    phase_poll_limit: int = 200

    def __init__(self, job_service=ixbrowser_service, db=sqlite_db) -> None:
        self._jobs = job_service
        self._db = db

    @staticmethod
    def build_filter(
        *,
        group_title: Optional[str],
        profile_id: Optional[int],
        status: Optional[str],
        phase: Optional[str],
        keyword: Optional[str],
        limit: int,
    ) -> SoraJobStreamFilter:
        safe_status = str(status or "").strip().lower() or None
        safe_phase = str(phase or "").strip().lower() or None
        safe_group = str(group_title or "").strip() or None
        safe_keyword = str(keyword or "").strip() or None
        return SoraJobStreamFilter(
            group_title=safe_group,
            profile_id=profile_id,
            status=safe_status,
            phase=safe_phase,
            keyword=safe_keyword,
            limit=min(max(int(limit), 1), 200),
        )

    def list_jobs(self, stream_filter: SoraJobStreamFilter) -> List[SoraJob]:
        return self._jobs.list_sora_jobs(
            group_title=stream_filter.group_title,
            profile_id=stream_filter.profile_id,
            status=stream_filter.status,
            phase=stream_filter.phase,
            keyword=stream_filter.keyword,
            limit=stream_filter.limit,
        )

    @staticmethod
    def build_snapshot_payload(jobs: Sequence[SoraJob]) -> Dict[str, object]:
        return {
            "jobs": list(jobs),
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _job_fingerprint(job: SoraJob) -> Tuple[object, ...]:
        return (
            int(job.job_id),
            job.updated_at,
            job.status,
            job.phase,
            job.progress_pct,
            job.image_url,
            job.task_id,
            job.generation_id,
            job.publish_url,
            job.watermark_status,
            job.watermark_url,
            job.watermark_error,
            job.error,
        )

    def build_fingerprint_map(self, jobs: Sequence[SoraJob]) -> Dict[int, Tuple[object, ...]]:
        result: Dict[int, Tuple[object, ...]] = {}
        for job in jobs:
            try:
                job_id = int(job.job_id)
            except Exception:
                continue
            result[job_id] = self._job_fingerprint(job)
        return result

    def diff_jobs(
        self,
        previous_fingerprints: Dict[int, Tuple[object, ...]],
        current_jobs: Sequence[SoraJob],
    ) -> Tuple[List[SoraJob], List[int], Dict[int, Tuple[object, ...]], Set[int]]:
        current_fingerprints = self.build_fingerprint_map(current_jobs)
        changed: List[SoraJob] = []
        for job in current_jobs:
            try:
                job_id = int(job.job_id)
            except Exception:
                continue
            if previous_fingerprints.get(job_id) != current_fingerprints.get(job_id):
                changed.append(job)
        removed_ids = sorted(set(previous_fingerprints.keys()) - set(current_fingerprints.keys()))
        return changed, removed_ids, current_fingerprints, set(current_fingerprints.keys())

    def get_latest_phase_event_id(self) -> int:
        rows = self._db.list_event_logs(
            source="task",
            resource_type="sora_job",
            limit=1,
        ).get("items", [])
        if not rows:
            return 0
        try:
            return int(rows[0].get("id") or 0)
        except Exception:
            return 0

    def list_phase_events_since(
        self,
        *,
        after_id: int,
        visible_job_ids: Set[int],
        limit: int,
    ) -> Tuple[List[SoraJobEvent], int]:
        rows = self._db.list_event_logs_since(
            after_id=int(after_id or 0),
            source="task",
            resource_type="sora_job",
            limit=min(max(int(limit), 1), 500),
        )
        events: List[SoraJobEvent] = []
        last_id = int(after_id or 0)
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id > last_id:
                last_id = row_id
            if str(row.get("resource_type") or "") != "sora_job":
                continue
            try:
                job_id = int(row.get("resource_id") or 0)
            except Exception:
                continue
            if not job_id or job_id not in visible_job_ids:
                continue
            phase = str(row.get("phase") or "").strip() or "unknown"
            event = str(row.get("event") or "").strip() or "unknown"
            created_at = str(row.get("created_at") or "").strip()
            if not created_at:
                continue
            events.append(
                SoraJobEvent(
                    id=row_id,
                    job_id=job_id,
                    phase=phase,
                    event=event,
                    message=row.get("message"),
                    created_at=created_at,
                )
            )
        return events, last_id


sora_job_stream_service = SoraJobStreamService()
