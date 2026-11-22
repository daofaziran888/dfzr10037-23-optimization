"""版本化索引覆盖段接口。

独立于通用资源路由，承载图谱索引的增量合并能力：
覆盖段登记、构建交付（回执校验/代仲裁/墓碑保护）、暂缓与恢复、
按图代核对、发布（幂等）、读取（已发布/仅就绪分离）、补构建清单与刷新。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.coverage import coverage_ledger

router = APIRouter(prefix="/api/index-coverage", tags=["版本化索引覆盖段"])


class AliasPayload(BaseModel):
    legacy_id: str
    current_id: str


class SnapshotPayload(BaseModel):
    graph_generation: int
    entity_ids: list[str] = Field(default_factory=list)
    note: str = ""


class SegmentPayload(BaseModel):
    scope: str
    segment_code: str | None = None


class DeliverPayload(BaseModel):
    build_id: str
    build_generation: int
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    receipt_entity_count: int
    receipt_snapshot_generation: int
    graph_snapshot_generation: int | None = None


class HoldPayload(BaseModel):
    reason: str = ""


class ReconcileQuery(BaseModel):
    graph_generation: int | None = None


def _segment_or_404(segment_id: int):
    segment = coverage_ledger.get_segment(segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail=f"覆盖段 {segment_id} 不存在")
    return segment


@router.get("/aliases")
def list_aliases() -> dict[str, Any]:
    return {"items": coverage_ledger.aliases()}


@router.post("/aliases")
def register_alias(payload: AliasPayload) -> dict[str, Any]:
    try:
        return coverage_ledger.register_alias(payload.legacy_id, payload.current_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/snapshots")
def list_snapshots() -> dict[str, Any]:
    return {"items": coverage_ledger.snapshots()}


@router.post("/snapshots")
def register_snapshot(payload: SnapshotPayload) -> dict[str, Any]:
    return coverage_ledger.register_snapshot(
        payload.graph_generation, payload.entity_ids, payload.note
    )


@router.get("/segments")
def list_segments() -> dict[str, Any]:
    return {"items": coverage_ledger.list_segments()}


@router.post("/segments")
def register_segment(payload: SegmentPayload) -> dict[str, Any]:
    segment = coverage_ledger.register_segment(payload.scope, payload.segment_code)
    return segment.to_dict()


@router.get("/segments/{segment_id}")
def get_segment(segment_id: int) -> dict[str, Any]:
    return _segment_or_404(segment_id).to_dict()


@router.post("/segments/{segment_id}/deliveries")
def deliver_build(segment_id: int, payload: DeliverPayload) -> dict[str, Any]:
    _segment_or_404(segment_id)
    try:
        delivery = coverage_ledger.deliver_build(
            segment_id,
            build_id=payload.build_id,
            build_generation=payload.build_generation,
            added=payload.added,
            removed=payload.removed,
            receipt_entity_count=payload.receipt_entity_count,
            receipt_snapshot_generation=payload.receipt_snapshot_generation,
            graph_snapshot_generation=payload.graph_snapshot_generation,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return delivery.to_dict()


@router.post("/segments/{segment_id}/hold")
def hold_segment(segment_id: int, payload: HoldPayload) -> dict[str, Any]:
    _segment_or_404(segment_id)
    try:
        return coverage_ledger.hold_segment(segment_id, payload.reason).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/segments/{segment_id}/release")
def release_segment(segment_id: int) -> dict[str, Any]:
    _segment_or_404(segment_id)
    try:
        return coverage_ledger.release_segment(segment_id).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/segments/{segment_id}/reconcile")
def reconcile_segment(segment_id: int, payload: ReconcileQuery | None = None) -> dict[str, Any]:
    _segment_or_404(segment_id)
    generation = payload.graph_generation if payload else None
    return coverage_ledger.reconcile_segment(segment_id, generation)


@router.post("/segments/{segment_id}/publish")
def publish_segment(segment_id: int) -> dict[str, Any]:
    _segment_or_404(segment_id)
    try:
        return coverage_ledger.publish_segment(segment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reconcile")
def reconcile_all(payload: ReconcileQuery | None = None) -> dict[str, Any]:
    generation = payload.graph_generation if payload else None
    return coverage_ledger.reconcile_all(generation)


@router.get("/read")
def read_coverage(entity_ids: str | None = None) -> dict[str, Any]:
    ids = [item.strip() for item in entity_ids.split(",")] if entity_ids else None
    return coverage_ledger.read_coverage(ids)


@router.get("/search/{entity_id}")
def search_entity(entity_id: str) -> dict[str, Any]:
    return coverage_ledger.search(entity_id)


@router.post("/supplement-plan/{graph_generation}")
def supplement_plan(graph_generation: int) -> dict[str, Any]:
    try:
        return coverage_ledger.build_supplement_plan(graph_generation)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/refresh")
def refresh(payload: ReconcileQuery | None = None) -> dict[str, Any]:
    generation = payload.graph_generation if payload else None
    return coverage_ledger.refresh(generation)
