"""图谱构建领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.coverage import DELIVERY_ACCEPTED, coverage_ledger
from app.store import store


class GraphBuildService(BaseModuleService):
    MODULE = "graph_build"
    ENTITY = "构建任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "数据范围", "图类型", "实体数量", "关系数量", "构建模式", "增量标记", "构建时间"]
    REQUIRED_FIELDS = ["任务编号", "数据范围", "图类型"]
    STATUS_ORDER = ["待执行", "构建中", "构建完成", "构建异常"]
    ACTION_RULES = {"启动构建": "构建中", "中止构建": "构建完成", "标记异常": "构建异常"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L3: graph_build → graph_index（构建完成后更新索引）
        if entry.get("status") == "构建完成":
            idx_msg = self._push_to_graph_index(entry)
            message += "；" + idx_msg
        # 链路 L4: graph_build → data_lineage（构建完成后触发血缘追踪）
        if entry.get("status") == "构建完成":
            lineage_msg = self._push_to_data_lineage(entry)
            message += "；" + lineage_msg
        # 链路 L5: graph_build → 版本化索引覆盖段（按构建代登记覆盖，替代只记实体数量）
        if entry.get("status") == "构建完成":
            coverage_msg = self._push_to_coverage_ledger(entry)
            message += "；" + coverage_msg
        return entry, message

    def _push_to_coverage_ledger(self, entry: dict[str, Any]) -> str:
        """把构建结果登记为版本化索引覆盖段的一次构建交付。

        数据范围里的实体标识以逗号分隔；构建代取构建时间的日期序数，保证
        多次构建可按代比较。回执实体数与构建任务的实体数量保持一致。
        """
        scope = str(entry.get("数据范围", ""))
        segment = coverage_ledger.register_segment(
            scope=scope,
            segment_code=f"COV-GB-{int(entry.get('id', 0)):04d}",
        )
        raw_ids = [part.strip() for part in scope.replace(";", ",").split(",") if part.strip()]
        entity_ids = coverage_ledger.normalize_entity_ids(raw_ids)
        generation = self._build_generation(entry)
        delivery = coverage_ledger.deliver_build(
            segment.segment_id,
            build_id=f"BUILD-{entry.get('任务编号', entry.get('id', 0))}",
            build_generation=generation,
            added=entity_ids,
            removed=[],
            receipt_entity_count=len(entity_ids),
            receipt_snapshot_generation=generation,
            graph_snapshot_generation=generation,
        )
        if delivery.result == DELIVERY_ACCEPTED:
            return (
                f"覆盖段 {segment.segment_code} 已登记构建代 {generation}，"
                f"覆盖实体 {len(entity_ids)} 个（就绪待发布）"
            )
        return f"覆盖段 {segment.segment_code} 交付未通过核对：{delivery.detail.get('reason', delivery.result)}"

    @staticmethod
    def _build_generation(entry: dict[str, Any]) -> int:
        """从构建时间推导图构建代（YYYY-MM-DD -> 日期序数），失败时回退到记录 id。"""
        built_at = str(entry.get("构建时间", ""))
        try:
            from datetime import datetime

            return int(datetime.strptime(built_at[:10], "%Y-%m-%d").timestamp())
        except (ValueError, TypeError):
            return int(entry.get("id", 0)) + 1_000_000

    def _push_to_graph_index(self, entry: dict[str, Any]) -> str:
        idx_rows = store.rows("graph_index")
        next_id = max((int(r.get("id", 0)) for r in idx_rows), default=0) + 1
        idx_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待构建",
            "pending": True,
            "abnormal": False,
            "索引编号": f"GRAP-IDX-{next_id:04d}",
            "目标属性": str(entry.get("图类型", "")),
            "索引类型": "graph-entity-fulltext",
            "覆盖实体": str(entry.get("实体数量", "0")),
            "存储体积": "estimated-128MB",
            "构建耗时": "",
            "刷新周期": "on-build-complete",
            "最近重建": str(entry.get("构建时间", "2026-11-18")),
        }
        idx_rows.append(idx_entry)
        return f"已更新图谱索引 {idx_entry['索引编号']}"

    def _push_to_data_lineage(self, entry: dict[str, Any]) -> str:
        lineage_rows = store.rows("data_lineage")
        next_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        lin_entry: dict[str, Any] = {
            "id": next_id,
            "status": "自动推导",
            "pending": True,
            "abnormal": False,
            "链路编号": f"DATA-LINE-{next_id:04d}",
            "上游实体": str(entry.get("数据范围", "")),
            "下游实体": str(entry.get("图类型", "")),
            "转换描述": f"graph_build_id={entry.get('id',0)}",
            "关联字段": str(entry.get("任务编号", "")),
            "影响层级": "graph-level",
            "链路类型": "auto-derived",
            "最后更新": str(entry.get("构建时间", "2026-11-18")),
        }
        lineage_rows.append(lin_entry)
        return f"已触发血缘追踪 {lin_entry['链路编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_index", "data_lineage", "index_coverage"]
        return base