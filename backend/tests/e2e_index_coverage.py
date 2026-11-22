"""版本化索引覆盖段端到端核对脚本。

用标准库 urllib 跑通完整业务叙事：

1. 历史实体标识别名归一（同一份覆盖取数兼容历史构建标识）
2. 登记两代图快照，创建两个覆盖范围交叠的覆盖段
3. 两代实体覆盖有交集的构建并发交付：一份新增设备、一份退役设备，
   代仲裁 + 墓碑保护 + 冲突留痕；回执不一致直接拒收
4. 暂缓其中一段：该段核对卡住，其他段照常交付/发布/可检索
5. 恢复后重新送达：重复回执幂等，不重复应用
6. 并发发布同一覆盖段只记一次
7. 读取区分已发布覆盖与仅就绪部分，并可追溯各段构建代
8. 发布结果同步图谱构建待办；按版本化覆盖段生成补构建清单
9. 刷新后核对已可检索实体与后续补构建数据范围
10. graph_build 构建完成自动登记覆盖段（L5 链路）

用法：python -I tests/e2e_index_coverage.py [base_url]
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def call(method: str, path: str, payload: dict | None = None, expect_error: bool = False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if expect_error:
                raise AssertionError(f"{method} {path} 预期失败却成功：{body}")
            return body
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise AssertionError(f"{method} {path} 意外失败：{exc.read().decode('utf-8')}") from exc
        return json.loads(exc.read().decode("utf-8"))


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} -> {detail}")


def main() -> int:
    print("== 1. 历史实体标识兼容：登记别名 DEV-OLD-001 -> DEV-001 ==")
    call("POST", "/api/index-coverage/aliases",
         {"legacy_id": "DEV-OLD-001", "current_id": "DEV-001"})
    aliases = call("GET", "/api/index-coverage/aliases")["items"]
    check("别名已登记", any(a["legacy_id"] == "DEV-OLD-001" and a["current_id"] == "DEV-001"
                            for a in aliases), aliases)

    print("== 2. 登记两代图快照（G100 与 G200，G200 退役 DEV-002、新增 DEV-004/DEV-005）==")
    call("POST", "/api/index-coverage/snapshots", {
        "graph_generation": 100,
        "entity_ids": ["DEV-001", "DEV-002", "DEV-003"],
        "note": "第一代图",
    })
    call("POST", "/api/index-coverage/snapshots", {
        "graph_generation": 200,
        "entity_ids": ["DEV-001", "DEV-003", "DEV-004", "DEV-005"],
        "note": "第二代图：DEV-002 退役",
    })
    check("两代图快照可查", len(call("GET", "/api/index-coverage/snapshots")["items"]) == 2)

    print("== 3. 创建两个覆盖交叠的版本化覆盖段 ==")
    seg_a = call("POST", "/api/index-coverage/segments",
                 {"scope": "机房A", "segment_code": "SEG-A"})
    seg_b = call("POST", "/api/index-coverage/segments",
                 {"scope": "机房B", "segment_code": "SEG-B"})
    id_a, id_b = seg_a["segment_id"], seg_b["segment_id"]
    check("覆盖段带版本号 v1", seg_a["version"] == 1 and seg_b["version"] == 1)

    print("== 4. SEG-A 第一代交付：使用历史标识 DEV-OLD-001，取数应归一为 DEV-001 ==")
    dlv_a = call("POST", f"/api/index-coverage/segments/{id_a}/deliveries", {
        "build_id": "BUILD-A-G100", "build_generation": 100,
        "added": ["DEV-OLD-001", "DEV-002"], "removed": [],
        "receipt_entity_count": 2, "receipt_snapshot_generation": 100,
        "graph_snapshot_generation": 100,
    })
    check("交付被接收", dlv_a["result"] == "已接收", dlv_a)
    seg_a_now = call("GET", f"/api/index-coverage/segments/{id_a}")
    check("旧标识已归一为 DEV-001", set(seg_a_now["entities"]) == {"DEV-001", "DEV-002"},
          seg_a_now["entities"])
    check("实体可追溯构建代 100",
          seg_a_now["entity_generations"] == {"DEV-001": 100, "DEV-002": 100},
          seg_a_now["entity_generations"])
    check("首份交付后进入已就绪", seg_a_now["stage"] == "已就绪")

    print("== 5. 回执与实际覆盖不一致必须拒收（已确认覆盖与构建回执一致）==")
    bad = call("POST", f"/api/index-coverage/segments/{id_a}/deliveries", {
        "build_id": "BUILD-A-BAD", "build_generation": 100,
        "added": ["DEV-001"], "removed": [],
        "receipt_entity_count": 9, "receipt_snapshot_generation": 100,
    })
    check("回执数量不符被拒绝", bad["result"] == "已拒绝"
          and bad["detail"]["reason"] == "receipt_mismatch", bad)
    seg_a_now = call("GET", f"/api/index-coverage/segments/{id_a}")
    check("拒收不改变覆盖实体", set(seg_a_now["entities"]) == {"DEV-001", "DEV-002"})

    print("== 6. SEG-B 两代交叠交付：G100 含 DEV-002，G200 退役 DEV-002、新增 DEV-004 ==")
    dlv_b1 = call("POST", f"/api/index-coverage/segments/{id_b}/deliveries", {
        "build_id": "BUILD-B-G100", "build_generation": 100,
        "added": ["DEV-002", "DEV-003"], "removed": [],
        "receipt_entity_count": 2, "receipt_snapshot_generation": 100,
        "graph_snapshot_generation": 100,
    })
    check("SEG-B 第一代接收", dlv_b1["result"] == "已接收")
    dlv_b2 = call("POST", f"/api/index-coverage/segments/{id_b}/deliveries", {
        "build_id": "BUILD-B-G200", "build_generation": 200,
        "added": ["DEV-004"], "removed": ["DEV-002"],
        "receipt_entity_count": 1, "receipt_snapshot_generation": 200,
        "graph_snapshot_generation": 200,
    })
    check("SEG-B 第二代接收", dlv_b2["result"] == "已接收", dlv_b2)
    seg_b_now = call("GET", f"/api/index-coverage/segments/{id_b}")
    check("退役设备已移出覆盖", "DEV-002" not in seg_b_now["entities"]
          and set(seg_b_now["entities"]) == {"DEV-003", "DEV-004"}, seg_b_now["entities"])
    check("退役设备进入墓碑并记录删除代 200",
          seg_b_now["tombstones"].get("DEV-002") == 200, seg_b_now["tombstones"])
    check("两代结论冲突已留痕且按新代仲裁",
          any(c.get("entity_id") == "DEV-002" and c.get("resolution") == "newer_generation_wins"
              for c in seg_b_now["conflicts"]), seg_b_now["conflicts"])

    print("== 7. 墓碑保护：较旧构建（G150）不能恢复已确认删除的 DEV-002 ==")
    dlv_old = call("POST", f"/api/index-coverage/segments/{id_b}/deliveries", {
        "build_id": "BUILD-B-G150-STALE", "build_generation": 150,
        "added": ["DEV-002"], "removed": [],
        "receipt_entity_count": 1, "receipt_snapshot_generation": 150,
        "graph_snapshot_generation": 150,
    })
    check("旧代交付被接收但复活被拦截",
          dlv_old["result"] == "已接收" and dlv_old["detail"]["blocked_resurrect"] == ["DEV-002"],
          dlv_old)
    seg_b_now = call("GET", f"/api/index-coverage/segments/{id_b}")
    check("DEV-002 未被旧代恢复", "DEV-002" not in seg_b_now["entities"])

    print("== 8. 暂缓 SEG-B：该段核对卡住，SEG-A 不受影响仍可交付 ==")
    held = call("POST", f"/api/index-coverage/segments/{id_b}/hold",
                {"reason": "跨段核对等待图代确认"})
    check("SEG-B 进入暂缓核对", held["stage"] == "暂缓核对")
    blocked = call("POST", f"/api/index-coverage/segments/{id_b}/reconcile",
                   {"graph_generation": 200})
    check("暂缓段核对被阻断并给出原因", blocked["blocked"] is True
          and blocked["reason"] == "segment_held", blocked)
    ok_a = call("POST", f"/api/index-coverage/segments/{id_a}/reconcile",
                {"graph_generation": 100})
    check("其他段核对不受影响", ok_a["reconciled"] is True and ok_a["blocked"] is False, ok_a)
    overview = call("POST", "/api/index-coverage/reconcile", {"graph_generation": 200})
    check("全量核对同时给出卡住段与可交付段",
          len(overview["blocked"]) == 1 and overview["blocked"][0]["segment_id"] == id_b,
          overview)

    print("== 9. 暂缓期间 SEG-A 照常发布，检索方仍能拿到已发布覆盖 ==")
    pub_a = call("POST", f"/api/index-coverage/segments/{id_a}/publish")
    check("SEG-A 发布成功", pub_a["published"] is True and pub_a["deduplicated"] is False, pub_a)
    search_a = call("GET", "/api/index-coverage/search/DEV-001")
    check("DEV-001 可检索且带构建代溯源",
          search_a["retrievable"] is True
          and search_a["published_matches"][0]["build_generation"] == 100
          and search_a["published_matches"][0]["segment_code"] == "SEG-A", search_a)
    search_b = call("GET", "/api/index-coverage/search/DEV-003")
    check("SEG-B 暂缓时 DEV-003 仅就绪、不可检索",
          search_b["retrievable"] is False and search_b["partial_ready"] is True
          and "SEG-B" in search_b["held_segments"], search_b)
    held_pub = call("POST", f"/api/index-coverage/segments/{id_b}/publish", expect_error=True)
    check("暂缓段禁止发布", "detail" in held_pub)

    print("== 10. 并发发布同一覆盖段只记一次 ==")
    call("POST", f"/api/index-coverage/segments/{id_b}/release")
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def publish() -> None:
        barrier.wait()
        results.append(call("POST", f"/api/index-coverage/segments/{id_b}/publish"))

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("两次并发发布仅一次真正生效",
          sum(1 for r in results if r["published"] is True) == 1
          and sum(1 for r in results if r["deduplicated"] is True) == 1, results)
    check("发布同步图谱构建待办",
          "GRAP-IDXSYNC" in results[0]["graph_build_todo"]
          or "GRAP-IDXSYNC" in results[1]["graph_build_todo"], results)

    print("== 11. 重新送达同一份构建：重复回执幂等，不重复应用 ==")
    redeliver = call("POST", f"/api/index-coverage/segments/{id_a}/deliveries", {
        "build_id": "BUILD-A-G100", "build_generation": 100,
        "added": ["DEV-OLD-001", "DEV-002"], "removed": [],
        "receipt_entity_count": 2, "receipt_snapshot_generation": 100,
        "graph_snapshot_generation": 100,
    })
    check("重复送达识别为重复回执", redeliver["result"] == "重复回执", redeliver)
    check("重复送达计数累加", redeliver["detail"].get("redeliveries", 0) >= 1, redeliver)
    seg_a_now = call("GET", f"/api/index-coverage/segments/{id_a}")
    check("覆盖实体未被重复应用", set(seg_a_now["entities"]) == {"DEV-001", "DEV-002"})

    print("== 12. 读取区分已发布覆盖与仅就绪部分，并追溯各段构建代 ==")
    seg_c = call("POST", "/api/index-coverage/segments",
                 {"scope": "机房C", "segment_code": "SEG-C"})
    id_c = seg_c["segment_id"]
    call("POST", f"/api/index-coverage/segments/{id_c}/deliveries", {
        "build_id": "BUILD-C-G200", "build_generation": 200,
        "added": ["DEV-004"], "removed": [],
        "receipt_entity_count": 1, "receipt_snapshot_generation": 200,
        "graph_snapshot_generation": 200,
    })
    coverage = call("GET", "/api/index-coverage/read")
    published_codes = {item["segment_code"] for item in coverage["published"]}
    ready_codes = {item["segment_code"] for item in coverage["ready_only"]}
    check("已发布覆盖包含 SEG-A/SEG-B 的实体",
          {"SEG-A", "SEG-B"} <= published_codes, coverage)
    check("未发布的 SEG-C 只出现在仅就绪分区",
          ready_codes == {"SEG-C"} and "SEG-C" not in published_codes, coverage)
    check("每次读取带构建代与构建编号溯源",
          all(item["build_generation"] and item["build_id"]
              for item in coverage["published"] + coverage["ready_only"]), coverage)
    # 暂缓未发布的 SEG-C 验证：卡住段实体留在仅就绪视图并登记暂缓段
    call("POST", f"/api/index-coverage/segments/{id_c}/hold", {"reason": "验证暂缓分区"})
    coverage2 = call("GET", "/api/index-coverage/read")
    check("暂缓段实体留在仅就绪视图且登记暂缓段",
          {item["entity_id"] for item in coverage2["ready_only"]} == {"DEV-004"}
          and "SEG-C" in coverage2["held_segments"], coverage2)
    check("已发布的 SEG-B 不受 SEG-C 暂缓影响",
          "SEG-B" in {item["segment_code"] for item in coverage2["published"]})
    call("POST", f"/api/index-coverage/segments/{id_c}/release")
    check("恢复后 SEG-C 仍为仅就绪（未发布不会自动发布）",
          "SEG-C" in {item["segment_code"] for item in
                      call("GET", "/api/index-coverage/read")["ready_only"]})

    print("== 13. 生效覆盖结果同步到图谱构建待办 ==")
    build_rows = call("GET", "/api/graph_build?size=200")["items"]
    sync_rows = [r for r in build_rows if str(r.get("任务编号", "")).startswith("GRAP-IDXSYNC")]
    check("graph_build 出现覆盖发布同步待办且为待执行",
          len(sync_rows) >= 1 and all(r["status"] == "待执行" for r in sync_rows),
          sync_rows)

    print("== 14. 用版本化覆盖段生成 G200 补构建清单 ==")
    plan = call("POST", "/api/index-coverage/supplement-plan/200")
    task_entities = [t["entity_id"] for t in plan["tasks"]]
    check("图中仅 DEV-005 无任何段覆盖 -> 进入补构建", task_entities == ["DEV-005"], plan)
    check("已退役 DEV-002 标记为过期覆盖", plan["stale_covered"] == ["DEV-002"], plan)
    check("补构建任务带图代与任务编号",
          all(t["task_code"].startswith("SUPP-200-") and t["graph_generation"] == 200
              for t in plan["tasks"]), plan)
    missing = call("POST", "/api/index-coverage/supplement-plan/999", expect_error=True)
    check("未登记图代无法生成补构建清单", "detail" in missing)

    print("== 15. 刷新：核对已可检索实体与后续补构建数据范围 ==")
    refreshed = call("POST", "/api/index-coverage/refresh", {"graph_generation": 200})
    check("刷新返回已发布可检索实体",
          set(refreshed["retrievable_entities"]) == {"DEV-001", "DEV-002", "DEV-003", "DEV-004"},
          refreshed["retrievable_entities"])
    check("仅就绪实体单列（SEG-C 尚未发布）",
          refreshed["ready_only_entities"] == ["DEV-004"], refreshed["ready_only_entities"])
    check("刷新附带补构建范围 DEV-005",
          [t["entity_id"] for t in refreshed["supplement_plan"]["tasks"]] == ["DEV-005"])
    check("刷新包含逐段核对结论", refreshed["reconciliation"]["total"] >= 2)

    print("== 16. L5 链路：graph_build 构建完成自动登记版本化覆盖段 ==")
    before = len(call("GET", "/api/index-coverage/segments")["items"])
    created = call("POST", "/api/graph_build", {
        "values": {
            "任务编号": "GRAP-E2E-0001",
            "数据范围": "DEV-001,DEV-003",
            "图类型": "设备图",
        }
    })
    gb_id = created["entry"]["id"]
    call("POST", f"/api/graph_build/{gb_id}/actions", {"action": "启动构建"})
    done = call("POST", f"/api/graph_build/{gb_id}/actions", {"action": "中止构建"})
    check("构建完成提示覆盖段已登记", "覆盖段 COV-GB-" in done["message"], done["message"])
    after_segments = call("GET", "/api/index-coverage/segments")["items"]
    check("覆盖段数量增加且带 COV-GB 编号",
          len(after_segments) == before + 1
          and any(s["segment_code"] == f"COV-GB-{gb_id:04d}" for s in after_segments),
          after_segments)

    print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
