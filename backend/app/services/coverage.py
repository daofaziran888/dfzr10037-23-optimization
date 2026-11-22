"""版本化索引覆盖段台账。

背景
----
旧实现里构建任务推给图谱索引的“覆盖”只有一个实体数量，两个构建范围交叠时
无法判断覆盖是否重复，也无法按图（graph generation）核对每份构建结果。

本模块把覆盖拆成 *版本化索引覆盖段*：

- 覆盖段 ``segment``：一段可独立交付、独立核对、独立发布的覆盖范围，带版本号；
- 构建代 ``build_generation``：每次构建交付的单调递增代数，数字越大图越新；
- 构建回执 ``build_receipt``：构建方随结果提交的回执（实体数、快照代），
  与实际覆盖实体集必须一致，否则拒绝交付；
- 墓碑 ``tombstone``：已确认删除的实体，任何较旧构建代都不能将其恢复；
- 就绪/发布分离：交付核对通过只到“就绪”，发布后才进入生效覆盖；
- 别名映射：同一份覆盖取数时，把历史构建中的旧实体标识归一到当前标识。

所有状态保存在进程内（与项目其他模块一致，真实环境替换为数据库），
写操作通过 ``threading.RLock`` 串行化，保证并发发布同一覆盖段只生效一次。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.store import store


# 覆盖段生命周期：已登记 -> 已就绪（至少一次交付核对通过）-> 已发布
STAGE_REGISTERED = "已登记"
STAGE_READY = "已就绪"
STAGE_PUBLISHED = "已发布"
# 核对被暂缓的覆盖段（不影响其他段继续交付）
STAGE_HELD = "暂缓核对"

# 单次构建交付的核对结果
DELIVERY_ACCEPTED = "已接收"
DELIVERY_REJECTED = "已拒绝"
DELIVERY_DUPLICATE = "重复回执"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BuildDelivery:
    """一次构建结果交付（同一覆盖段可有多代交付）。"""

    delivery_id: int
    build_id: str
    build_generation: int
    added: list[str]
    removed: list[str]
    receipt_entity_count: int
    receipt_snapshot_generation: int
    graph_snapshot_generation: int
    result: str
    detail: dict[str, Any] = field(default_factory=dict)
    delivered_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "build_id": self.build_id,
            "build_generation": self.build_generation,
            "added": list(self.added),
            "removed": list(self.removed),
            "receipt_entity_count": self.receipt_entity_count,
            "receipt_snapshot_generation": self.receipt_snapshot_generation,
            "graph_snapshot_generation": self.graph_snapshot_generation,
            "result": self.result,
            "detail": self.detail,
            "delivered_at": self.delivered_at,
        }


@dataclass
class CoverageSegment:
    """版本化索引覆盖段。"""

    segment_id: int
    segment_code: str
    scope: str
    version: int
    stage: str = STAGE_REGISTERED
    # 段内“当前图视角”的实体覆盖（已按代仲裁、别名归一后的有效集合）
    entities: set[str] = field(default_factory=set)
    # 每个实体当前生效的构建代，读取时可追溯到段 -> 代
    entity_generations: dict[str, int] = field(default_factory=dict)
    # 已确认删除、旧代不得恢复的实体
    tombstones: dict[str, int] = field(default_factory=dict)
    deliveries: list[BuildDelivery] = field(default_factory=list)
    # 尚未仲裁的冲突（同实体在交叠的两代构建里一个新增、一个移除）
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    published_at: str | None = None
    updated_at: str = field(default_factory=_now)

    def to_dict(self, *, include_entities: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "segment_id": self.segment_id,
            "segment_code": self.segment_code,
            "scope": self.scope,
            "version": self.version,
            "stage": self.stage,
            "entity_count": len(self.entities),
            "tombstone_count": len(self.tombstones),
            "conflict_count": len(self.conflicts),
            "deliveries": [d.to_dict() for d in self.deliveries],
            "conflicts": list(self.conflicts),
            "published_at": self.published_at,
            "updated_at": self.updated_at,
        }
        if include_entities:
            data["entities"] = sorted(self.entities)
            data["entity_generations"] = dict(sorted(self.entity_generations.items()))
            data["tombstones"] = dict(sorted(self.tombstones.items()))
        return data


class CoverageLedger:
    """版本化覆盖段台账（单例，进程内）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._segments: dict[int, CoverageSegment] = {}
        self._seq_segment = 0
        self._seq_delivery = 0
        # 已发布过的 (segment_code, version)，并发发布只记一次
        self._published_keys: set[tuple[str, int]] = set()
        # 已接收过的 build_id，重复送达走幂等
        self._accepted_builds: dict[str, tuple[int, int]] = {}
        # 历史实体标识 -> 当前实体标识的归一化映射
        self._aliases: dict[str, str] = {}
        # 图代登记：graph_generation -> 图中实体集合
        self._snapshots: dict[int, set[str]] = {}
        self._snapshot_meta: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 实体标识兼容：历史构建里的旧标识归一到当前标识
    # ------------------------------------------------------------------
    def register_alias(self, legacy_id: str, current_id: str) -> dict[str, Any]:
        with self._lock:
            legacy_id = str(legacy_id).strip()
            current_id = str(current_id).strip()
            if not legacy_id or not current_id:
                raise ValueError("legacy_id 与 current_id 均不能为空")
            # 支持链式：legacy -> middle -> current
            target = self._aliases.get(current_id, current_id)
            self._aliases[legacy_id] = target
            return {"legacy_id": legacy_id, "current_id": target, "registered_at": _now()}

    def aliases(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"legacy_id": legacy, "current_id": current}
                for legacy, current in sorted(self._aliases.items())
            ]

    def _normalize(self, entity_id: str) -> str:
        entity_id = str(entity_id).strip()
        # 链式解析（防御环）
        seen: set[str] = set()
        while entity_id in self._aliases and entity_id not in seen:
            seen.add(entity_id)
            entity_id = self._aliases[entity_id]
        return entity_id

    def normalize_entity_ids(self, ids: list[str]) -> list[str]:
        """对外公开的实体标识归一化（别名解析 + 去重保序）。"""
        return self._normalize_batch(ids)

    def _normalize_batch(self, ids: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in ids or []:
            normalized = self._normalize(raw)
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered

    # ------------------------------------------------------------------
    # 图快照：核对覆盖段时按实际图代比对
    # ------------------------------------------------------------------
    def register_snapshot(self, graph_generation: int, entity_ids: list[str], note: str = "") -> dict[str, Any]:
        with self._lock:
            normalized = set(self._normalize_batch(entity_ids))
            self._snapshots[int(graph_generation)] = normalized
            self._snapshot_meta[int(graph_generation)] = {
                "graph_generation": int(graph_generation),
                "entity_count": len(normalized),
                "note": note,
                "registered_at": _now(),
            }
            return dict(self._snapshot_meta[int(graph_generation)])

    def snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._snapshot_meta[g]) for g in sorted(self._snapshot_meta)]

    def _snapshot_entities(self, generation: int) -> set[str] | None:
        return self._snapshots.get(int(generation))

    # ------------------------------------------------------------------
    # 覆盖段登记 / 查询
    # ------------------------------------------------------------------
    def register_segment(self, scope: str, segment_code: str | None = None) -> CoverageSegment:
        with self._lock:
            self._seq_segment += 1
            segment_id = self._seq_segment
            code = segment_code or f"SEG-{segment_id:04d}"
            segment = CoverageSegment(
                segment_id=segment_id,
                segment_code=code,
                scope=str(scope),
                version=1,
            )
            self._segments[segment_id] = segment
            return segment

    def list_segments(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict(include_entities=False) for s in self._segments.values()]

    def get_segment(self, segment_id: int) -> CoverageSegment | None:
        with self._lock:
            return self._segments.get(int(segment_id))

    def _require_segment(self, segment_id: int) -> CoverageSegment:
        segment = self._segments.get(int(segment_id))
        if segment is None:
            raise KeyError(f"覆盖段 {segment_id} 不存在")
        return segment

    # ------------------------------------------------------------------
    # 构建结果交付：回执校验 + 代仲裁 + 墓碑保护 + 冲突记录
    # ------------------------------------------------------------------
    def deliver_build(
        self,
        segment_id: int,
        *,
        build_id: str,
        build_generation: int,
        added: list[str],
        removed: list[str],
        receipt_entity_count: int,
        receipt_snapshot_generation: int,
        graph_snapshot_generation: int | None = None,
    ) -> BuildDelivery:
        with self._lock:
            segment = self._require_segment(segment_id)
            build_id = str(build_id).strip()
            generation = int(build_generation)

            # 幂等：同一份构建重新送达，不重复应用
            if build_id in self._accepted_builds:
                seg_id, dlv_id = self._accepted_builds[build_id]
                existing = next(
                    (d for d in segment.deliveries if d.delivery_id == dlv_id), None
                )
                if existing is not None and seg_id == segment_id:
                    existing.detail.setdefault("redeliveries", 0)
                    existing.detail["redeliveries"] += 1
                    existing.detail["last_redelivered_at"] = _now()
                    return BuildDelivery(
                        delivery_id=existing.delivery_id,
                        build_id=build_id,
                        build_generation=existing.build_generation,
                        added=list(existing.added),
                        removed=list(existing.removed),
                        receipt_entity_count=existing.receipt_entity_count,
                        receipt_snapshot_generation=existing.receipt_snapshot_generation,
                        graph_snapshot_generation=existing.graph_snapshot_generation,
                        result=DELIVERY_DUPLICATE,
                        detail=existing.detail,
                        delivered_at=existing.delivered_at,
                    )

            norm_added = self._normalize_batch(added)
            norm_removed = self._normalize_batch(removed)

            # 回执一致性：回执声明的实体数量必须与实际覆盖结果一致
            # （新增集合与移除集合不应有交集；有效覆盖 = 新增 - 移除）
            overlap = sorted(set(norm_added) & set(norm_removed))
            effective_added = [e for e in norm_added if e not in set(norm_removed)]
            if int(receipt_entity_count) != len(effective_added):
                return self._record_delivery(
                    segment, build_id, generation, norm_added, norm_removed,
                    receipt_entity_count, receipt_snapshot_generation,
                    graph_snapshot_generation, DELIVERY_REJECTED,
                    {
                        "reason": "receipt_mismatch",
                        "receipt_entity_count": int(receipt_entity_count),
                        "actual_entity_count": len(effective_added),
                    },
                )
            if overlap:
                return self._record_delivery(
                    segment, build_id, generation, norm_added, norm_removed,
                    receipt_entity_count, receipt_snapshot_generation,
                    graph_snapshot_generation, DELIVERY_REJECTED,
                    {"reason": "added_removed_overlap", "overlap": overlap},
                )

            # 墓碑保护：已确认删除的实体不能被较旧构建恢复
            blocked_resurrect = sorted(
                e for e in effective_added
                if e in segment.tombstones and segment.tombstones[e] > generation
            )
            applied_added = [e for e in effective_added if e not in set(blocked_resurrect)]

            # 代仲裁：同实体与已有覆盖的代比较，旧代不能覆盖新代结论
            stale_added: list[str] = []
            for entity_id in applied_added:
                current_gen = segment.entity_generations.get(entity_id)
                if current_gen is not None and current_gen > generation:
                    stale_added.append(entity_id)
            applied_added = [e for e in applied_added if e not in set(stale_added)]

            # 移除只对不新于本代的结论生效；新代已确认存在的实体不能被旧代移除
            stale_removed: list[str] = []
            applied_removed: list[str] = []
            for entity_id in norm_removed:
                current_gen = segment.entity_generations.get(entity_id)
                if current_gen is not None and current_gen > generation:
                    stale_removed.append(entity_id)
                    continue
                applied_removed.append(entity_id)

            # 应用变更
            for entity_id in applied_removed:
                segment.entities.discard(entity_id)
                segment.entity_generations.pop(entity_id, None)
                segment.tombstones[entity_id] = generation
            for entity_id in applied_added:
                segment.entities.add(entity_id)
                segment.entity_generations[entity_id] = generation
                # 新代重新确认存在时，撤销更早的墓碑
                stale_tomb = segment.tombstones.get(entity_id)
                if stale_tomb is not None and stale_tomb <= generation:
                    segment.tombstones.pop(entity_id, None)

            # 冲突检测：两代构建覆盖有交集，且对同一实体结论相反（一个新增、一个移除）
            new_conflicts = self._detect_conflicts(
                segment, generation, applied_added, applied_removed
            )

            if segment.stage == STAGE_REGISTERED:
                segment.stage = STAGE_READY
            segment.updated_at = _now()

            detail: dict[str, Any] = {
                "applied_added": applied_added,
                "applied_removed": applied_removed,
                "blocked_resurrect": blocked_resurrect,
                "stale_added": stale_added,
                "stale_removed": stale_removed,
                "new_conflicts": len(new_conflicts),
            }
            delivery = self._record_delivery(
                segment, build_id, generation, norm_added, norm_removed,
                receipt_entity_count, receipt_snapshot_generation,
                graph_snapshot_generation, DELIVERY_ACCEPTED, detail,
            )
            self._accepted_builds[build_id] = (segment.segment_id, delivery.delivery_id)
            return delivery

    def _record_delivery(
        self,
        segment: CoverageSegment,
        build_id: str,
        generation: int,
        added: list[str],
        removed: list[str],
        receipt_entity_count: int,
        receipt_snapshot_generation: int,
        graph_snapshot_generation: int | None,
        result: str,
        detail: dict[str, Any],
    ) -> BuildDelivery:
        self._seq_delivery += 1
        delivery = BuildDelivery(
            delivery_id=self._seq_delivery,
            build_id=build_id,
            build_generation=generation,
            added=list(added),
            removed=list(removed),
            receipt_entity_count=int(receipt_entity_count),
            receipt_snapshot_generation=int(receipt_snapshot_generation),
            graph_snapshot_generation=(
                int(graph_snapshot_generation)
                if graph_snapshot_generation is not None
                else int(receipt_snapshot_generation)
            ),
            result=result,
            detail=detail,
        )
        segment.deliveries.append(delivery)
        segment.updated_at = _now()
        return delivery

    def _detect_conflicts(
        self,
        segment: CoverageSegment,
        generation: int,
        added: list[str],
        removed: list[str],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        prior = [d for d in segment.deliveries if d.result == DELIVERY_ACCEPTED]
        for other in prior:
            if other.build_generation == generation:
                continue
            prior_added, prior_removed = set(other.added), set(other.removed)
            # 旧代新增、本代移除
            for entity_id in sorted(prior_added & set(removed)):
                conflict = {
                    "entity_id": entity_id,
                    "older_generation": min(generation, other.build_generation),
                    "newer_generation": max(generation, other.build_generation),
                    "older_action": "added" if other.build_generation < generation else "removed",
                    "newer_action": "removed" if other.build_generation < generation else "added",
                    "resolution": "newer_generation_wins",
                }
                conflicts.append(conflict)
                segment.conflicts.append(conflict)
            # 旧代移除、本代新增
            for entity_id in sorted(prior_removed & set(added)):
                conflict = {
                    "entity_id": entity_id,
                    "older_generation": min(generation, other.build_generation),
                    "newer_generation": max(generation, other.build_generation),
                    "older_action": "removed" if other.build_generation < generation else "added",
                    "newer_action": "added" if other.build_generation < generation else "removed",
                    "resolution": "newer_generation_wins",
                }
                conflicts.append(conflict)
                segment.conflicts.append(conflict)
        return conflicts

    # ------------------------------------------------------------------
    # 暂缓 / 恢复：某段核对卡住不影响其他段继续交付
    # ------------------------------------------------------------------
    def hold_segment(self, segment_id: int, reason: str) -> CoverageSegment:
        with self._lock:
            segment = self._require_segment(segment_id)
            if segment.stage == STAGE_PUBLISHED:
                raise ValueError(f"覆盖段 {segment.segment_code} 已发布，不能暂缓")
            segment.stage = STAGE_HELD
            segment.updated_at = _now()
            segment.conflicts.append({"hold": True, "reason": reason, "at": segment.updated_at})
            return segment

    def release_segment(self, segment_id: int) -> CoverageSegment:
        with self._lock:
            segment = self._require_segment(segment_id)
            if segment.stage != STAGE_HELD:
                raise ValueError(f"覆盖段 {segment.segment_code} 当前未处于暂缓状态")
            segment.stage = STAGE_READY if segment.deliveries else STAGE_REGISTERED
            segment.updated_at = _now()
            return segment

    # ------------------------------------------------------------------
    # 按实际图代核对覆盖段
    # ------------------------------------------------------------------
    def reconcile_segment(self, segment_id: int, graph_generation: int | None = None) -> dict[str, Any]:
        with self._lock:
            segment = self._require_segment(segment_id)
            if segment.stage == STAGE_HELD:
                return {
                    "segment_id": segment.segment_id,
                    "segment_code": segment.segment_code,
                    "reconciled": False,
                    "blocked": True,
                    "reason": "segment_held",
                    "message": "覆盖段处于暂缓核对状态，其他已完成段可继续交付",
                }

            target_gen = int(graph_generation) if graph_generation is not None else max(
                (d.graph_snapshot_generation for d in segment.deliveries), default=0
            )
            snapshot = self._snapshot_entities(target_gen)
            if snapshot is None:
                return {
                    "segment_id": segment.segment_id,
                    "segment_code": segment.segment_code,
                    "reconciled": False,
                    "blocked": True,
                    "reason": "snapshot_not_registered",
                    "graph_generation": target_gen,
                    "message": f"图代 {target_gen} 的快照尚未登记，核对卡住",
                }

            covered = set(segment.entities)
            missing_in_graph = sorted(covered - snapshot)       # 覆盖声称有、图里没有
            graph_not_covered = sorted(snapshot - covered)      # 图里有、覆盖段没覆盖
            stale_tombstones = sorted(
                entity_id for entity_id, gen in segment.tombstones.items()
                if entity_id in snapshot and gen <= target_gen and entity_id not in covered
            )
            # 已按“新代优先”自动仲裁的冲突不再阻塞核对；仅人工未裁决项算未解决
            unresolved = [
                conflict for conflict in segment.conflicts
                if "hold" not in conflict and conflict.get("resolution") != "newer_generation_wins"
            ]
            reconciled = not missing_in_graph and not unresolved and not stale_tombstones

            result = {
                "segment_id": segment.segment_id,
                "segment_code": segment.segment_code,
                "graph_generation": target_gen,
                "reconciled": reconciled,
                "blocked": False,
                "covered_entity_count": len(covered),
                "missing_in_graph": missing_in_graph,
                "graph_not_covered": graph_not_covered,
                "stale_tombstones": stale_tombstones,
                "unresolved_conflicts": unresolved,
                "entity_generations": dict(sorted(segment.entity_generations.items())),
                "checked_at": _now(),
            }
            segment.updated_at = result["checked_at"]
            return result

    def reconcile_all(self, graph_generation: int | None = None) -> dict[str, Any]:
        """全量核对：一段卡住时，检索方仍可拿到其他段的交付结论。"""
        with self._lock:
            reports = [
                self.reconcile_segment(segment.segment_id, graph_generation)
                for segment in self._segments.values()
            ]
            return {
                "total": len(reports),
                "reconciled": sum(1 for r in reports if r.get("reconciled")),
                "blocked": [
                    {"segment_id": r["segment_id"], "reason": r.get("reason")}
                    for r in reports if r.get("blocked")
                ],
                "reports": reports,
            }

    # ------------------------------------------------------------------
    # 发布：并发发布同一覆盖段只记一次；生效后同步图谱构建待办
    # ------------------------------------------------------------------
    def publish_segment(self, segment_id: int) -> dict[str, Any]:
        with self._lock:
            segment = self._require_segment(segment_id)
            key = (segment.segment_code, segment.version)
            if key in self._published_keys or segment.stage == STAGE_PUBLISHED:
                return {
                    "segment_id": segment.segment_id,
                    "segment_code": segment.segment_code,
                    "published": False,
                    "deduplicated": True,
                    "message": "该覆盖段已发布，并发发布只记一次",
                    "published_at": segment.published_at,
                }
            if segment.stage == STAGE_HELD:
                raise ValueError(f"覆盖段 {segment.segment_code} 处于暂缓状态，不能发布")
            if not segment.deliveries or not segment.entities:
                raise ValueError(f"覆盖段 {segment.segment_code} 尚无已核对的构建结果，不能发布")

            segment.stage = STAGE_PUBLISHED
            segment.published_at = _now()
            segment.updated_at = segment.published_at
            self._published_keys.add(key)

            todo_message = self._sync_graph_build_todo(segment)
            return {
                "segment_id": segment.segment_id,
                "segment_code": segment.segment_code,
                "version": segment.version,
                "published": True,
                "deduplicated": False,
                "published_at": segment.published_at,
                "effective_entity_count": len(segment.entities),
                "graph_build_todo": todo_message,
            }

    def _sync_graph_build_todo(self, segment: CoverageSegment) -> str:
        """生效覆盖的结果同步到图谱构建待办（graph_build）。"""
        rows = store.rows("graph_build")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        todo = {
            "id": next_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-IDXSYNC-{next_id:04d}",
            "数据范围": f"{segment.segment_code}@v{segment.version}",
            "图类型": "index-coverage-sync",
            "实体数量": len(segment.entities),
            "关系数量": 0,
            "构建模式": "coverage-publish-sync",
            "增量标记": f"segment={segment.segment_code};version={segment.version}",
            "构建时间": segment.published_at or _now(),
        }
        rows.append(todo)
        return f"已同步图谱构建待办 {todo['任务编号']}（{todo['实体数量']} 个实体）"

    # ------------------------------------------------------------------
    # 读取：区分已发布覆盖与仅准备就绪部分，并追溯各段构建代
    # ------------------------------------------------------------------
    def read_coverage(self, entity_ids: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            published: dict[str, dict[str, Any]] = {}
            ready_only: dict[str, dict[str, Any]] = {}

            targets: set[str] | None = None
            if entity_ids is not None:
                targets = set(self._normalize_batch(entity_ids))

            for segment in self._segments.values():
                for entity_id in sorted(segment.entities):
                    if targets is not None and entity_id not in targets:
                        continue
                    bucket = published if segment.stage == STAGE_PUBLISHED else ready_only
                    generation = segment.entity_generations.get(entity_id)
                    delivery = next(
                        (d for d in reversed(segment.deliveries)
                         if d.result == DELIVERY_ACCEPTED and d.build_generation == generation),
                        None,
                    )
                    bucket[entity_id] = {
                        "entity_id": entity_id,
                        "segment_id": segment.segment_id,
                        "segment_code": segment.segment_code,
                        "segment_version": segment.version,
                        "build_generation": generation,
                        "build_id": delivery.build_id if delivery else None,
                        "stage": segment.stage,
                    }

            return {
                "published": list(published.values()),
                "ready_only": list(ready_only.values()),
                "published_count": len(published),
                "ready_only_count": len(ready_only),
                "held_segments": [
                    s.segment_code for s in self._segments.values() if s.stage == STAGE_HELD
                ],
                "read_at": _now(),
            }

    def search(self, entity_id: str) -> dict[str, Any]:
        """检索方视角：某段卡住时，仍可判断其他已完成段能否继续交付。"""
        with self._lock:
            normalized = self._normalize(entity_id)
            coverage = self.read_coverage([normalized])
            deliverable = bool(coverage["published"])
            partial = bool(coverage["ready_only"])
            return {
                "query": entity_id,
                "normalized_entity_id": normalized,
                "retrievable": deliverable,
                "partial_ready": partial,
                "published_matches": coverage["published"],
                "ready_matches": coverage["ready_only"],
                "held_segments": coverage["held_segments"],
                "message": (
                    "已发布覆盖可检索" if deliverable
                    else "仅就绪未发布，或实体暂不可检索；卡住的段不影响其他段交付"
                ),
            }

    # ------------------------------------------------------------------
    # 补构建清单：用版本化索引覆盖段对照当前图代生成
    # ------------------------------------------------------------------
    def build_supplement_plan(self, graph_generation: int) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot_entities(graph_generation)
            if snapshot is None:
                raise KeyError(f"图代 {graph_generation} 的快照尚未登记，无法生成补构建清单")

            covered: set[str] = set()
            for segment in self._segments.values():
                if segment.stage == STAGE_HELD:
                    continue
                covered |= set(segment.entities)

            # 图里存在、但没有任何已就绪/已发布覆盖段覆盖的实体 -> 需要补构建
            supplement_entities = sorted(snapshot - covered)
            # 覆盖段声称覆盖、但当前图代已不存在 -> 需要重建/退役核对
            stale_covered = sorted(covered - snapshot)

            tasks: list[dict[str, Any]] = []
            for index, entity_id in enumerate(supplement_entities, start=1):
                tasks.append({
                    "task_code": f"SUPP-{graph_generation}-{index:04d}",
                    "entity_id": entity_id,
                    "graph_generation": int(graph_generation),
                    "reason": "not_covered_by_any_segment",
                    "scope_segments": sorted(
                        s.segment_code for s in self._segments.values()
                        if s.stage != STAGE_HELD
                    ),
                })

            return {
                "graph_generation": int(graph_generation),
                "snapshot_entity_count": len(snapshot),
                "covered_entity_count": len(covered & snapshot),
                "supplement_count": len(tasks),
                "stale_covered": stale_covered,
                "tasks": tasks,
                "generated_at": _now(),
            }

    def refresh(self, graph_generation: int | None = None) -> dict[str, Any]:
        """刷新：重新核对所有段并给出当前可检索实体与后续补构建范围。"""
        with self._lock:
            reconciliation = self.reconcile_all(graph_generation)
            target_gen = int(graph_generation) if graph_generation is not None else max(
                self._snapshots.keys(), default=0
            )
            coverage = self.read_coverage()
            plan: dict[str, Any] | None = None
            if target_gen in self._snapshots:
                plan = self.build_supplement_plan(target_gen)
            return {
                "graph_generation": target_gen,
                "reconciliation": reconciliation,
                "retrievable_entities": sorted(
                    item["entity_id"] for item in coverage["published"]
                ),
                "ready_only_entities": sorted(
                    item["entity_id"] for item in coverage["ready_only"]
                ),
                "supplement_plan": plan,
                "refreshed_at": _now(),
            }


# 进程级单例
coverage_ledger = CoverageLedger()
