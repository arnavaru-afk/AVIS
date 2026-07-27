"""Pipeline client wrapper for AVIS API."""

from __future__ import annotations

from avis.client.base import AVISClient
from avis.client.models import PipelineRun, PipelineRunDetail, PipelineRunListResponse


class PipelineClient(AVISClient):
    def list_runs(self, status: str | None = None) -> list[PipelineRun] | None:
        payload = self._get("/api/v1/pipeline/runs", params={"status": status} if status else None)
        if payload is None:
            return None
        parsed = self._parse_dataclass(payload, PipelineRunListResponse)
        return parsed.items

    def get_run(self, run_id: int) -> PipelineRunDetail | None:
        payload = self._get(f"/api/v1/pipeline/runs/{run_id}")
        if payload is None:
            return None
        return self._parse_dataclass(payload, PipelineRunDetail)
