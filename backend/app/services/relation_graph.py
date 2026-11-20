"""关系图谱领域服务：构建实体语义关系图。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services import relation_endpoints as endpoints
from app.services.base import BaseModuleService
from app.store import store


RELATION_TYPES = [
    "从属关系", "引用关系", "时序关系", "同义关系",
    "因果关系", "对比关系", "并列关系", "层级关系",
]


class RelationGraphService(BaseModuleService):
    MODULE = "relation_graph"
    ENTITY = "关系边"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["边ID", "源实体", "目标实体", "关系类型", "抽取页面", "原文证据", "关联强度"]
    REQUIRED_FIELDS = ["边ID", "源实体", "目标实体"]
    STATUS_ORDER = ["已抽取", "已验证", "已入库", "已过期"]
    ACTION_RULES = {"验证关系": "已抽取", "确认入库": "已验证", "标记过期": "已抽取"}
    NEGATIVE_ACTIONS = []

    def build_relations(self, entity_extract_records: list[dict[str, Any]]) -> dict[str, Any]:
        """基于实体抽取记录构建关系图谱。消费 entity_extract 输出。

        建边结果沿用历史显示格式：字段名与次序都由 ``endpoints.display_edge``
        统一给出，不再在本方法里另写一份键名。
        """
        if len(entity_extract_records) < 2:
            return {"ok": True, "relation_count": 0, "relations": [], "reason": "实体数量不足"}

        relations = []
        for i in range(len(entity_extract_records)):
            for j in range(i + 1, min(i + 4, len(entity_extract_records))):
                src = entity_extract_records[i]
                tgt = entity_extract_records[j]
                if src.get("页面来源") != tgt.get("页面来源"):
                    continue
                rel_type_idx = (i * 7 + j * 13) % len(RELATION_TYPES)
                edge_id = hashlib.md5(
                    f"{src.get('实体ID')}|{tgt.get('实体ID')}|{RELATION_TYPES[rel_type_idx]}".encode()
                ).hexdigest()[:10]
                strength = round(0.85 - (abs(i - j) * 0.08), 2)
                relations.append(endpoints.display_edge(
                    edge_id=edge_id,
                    source_entity=src.get("实体名称", src.get("实体ID", "")),
                    target_entity=tgt.get("实体名称", tgt.get("实体ID", "")),
                    relation_type=RELATION_TYPES[rel_type_idx],
                    evidence_page=src.get("页面来源"),
                    evidence=f"从{src.get('页面来源')}中抽取的共现关系",
                    strength=str(strength),
                ))

        return {
            "ok": True,
            "entity_count": len(entity_extract_records),
            "relation_count": len(relations),
            "relations": relations,
            "relation_types_used": list(set(r["关系类型"] for r in relations)),
        }

    def feed_knowledge_link(
        self,
        relation_ids: list[str],
        *,
        manifest_id: str | None = None,
    ) -> dict[str, Any]:
        """将已验证的关系交接给知识链接模块。

        交接清单沿用历史交付格式。内部只组装一次：存储行先按显示格式读成规范边，
        再按交付格式回显，源端/目标端始终成套来自同一条存储行。同一批边若还有
        未关闭的交接清单，则继续该清单并在其中登记新版本与端点冲突，便于外链候选
        先于复核到达时重投复用已确认的出处。
        """
        relations: list[dict[str, Any]] = []
        canonical: list[dict[str, Any]] = []
        requested = 0
        for rid in relation_ids:
            requested += 1
            row = next((r for r in store.rows(self.MODULE) if r.get("边ID") == rid), None)
            if row and row.get("status") == "已验证":
                edge = endpoints.canonical_edge(row, fmt=endpoints.DISPLAY)
                canonical.append(edge)
                relations.append(endpoints.render_edge(edge, endpoints.HANDOVER))

        ledger = endpoints.handover_ledger
        if manifest_id is None:
            manifest_id = ledger.find_open_manifest(
                [edge["edge_id"] for edge in canonical]
            ) or ledger.next_manifest_id()
        if ledger.manifest(manifest_id) is None:
            ledger.begin(manifest_id, canonical)
        else:
            ledger.extend(manifest_id, canonical)

        return {
            "ok": True,
            "requested_ids": requested,
            "found_relations": len(relations),
            "relations": relations,
        }

    def build_endpoint_projection(self, relation_ids: list[str]) -> dict[str, Any]:
        """生成供知识链接消费的关系端点投影数据集。

        投影与交接清单由同一份规范边产生：每条边一行，两个端点带方向、成套属于
        同一个 ``version``。知识链接从这里取实际端点，不再自己拼一份。
        """
        canonical: list[dict[str, Any]] = []
        missing: list[str] = []
        for rid in relation_ids:
            row = next((r for r in store.rows(self.MODULE) if r.get("边ID") == rid), None)
            if row and row.get("status") == "已验证":
                canonical.append(endpoints.canonical_edge(row, fmt=endpoints.DISPLAY))
            else:
                missing.append(rid)
        projection = endpoints.build_projection(canonical)
        projection["requested_ids"] = len(relation_ids)
        projection["missing_ids"] = missing
        return projection

    def handover_status(self, manifest_id: str | None = None) -> dict[str, Any] | None:
        """另一个建边入口的同步视图：候选、端点冲突和实际发布结果。

        不传 ``manifest_id`` 时取最近一次未关闭的交接；没有未关闭清单时取最近一次。
        """
        ledger = endpoints.handover_ledger
        if manifest_id is None:
            manifest_id = ledger.latest_manifest_id(open_only=True)
        if manifest_id is None:
            return None
        manifest = ledger.manifest(manifest_id)
        if manifest is None:
            return None
        return {
            "manifest_id": manifest_id,
            "status": manifest["status"],
            "candidates": [
                {
                    "edge_id": edge.get("edge_id"),
                    "version": edge.get("version"),
                    "source_entity": edge.get("source_entity"),
                    "target_entity": edge.get("target_entity"),
                    "relation_type": edge.get("relation_type"),
                    "evidence_page": edge.get("evidence_page"),
                }
                for edge in manifest["candidates"]
            ],
            "conflicts": list(manifest["conflicts"]),
            "receipts": [
                {
                    "edge_id": receipt.get("edge_id"),
                    "version": receipt.get("version"),
                    "link_ids": list(receipt.get("link_ids", [])),
                }
                for receipt in manifest["receipts"]
            ],
            "published": [
                {
                    "edge_id": snapshot.get("edge_id"),
                    "version": snapshot.get("version"),
                    "source_entity": snapshot.get("source_entity"),
                    "target_entity": snapshot.get("target_entity"),
                    "link_ids": list(snapshot.get("link_ids", [])),
                }
                for snapshot in manifest["published"]
            ],
        }

    def interrupt_handover(self, manifest_id: str | None = None) -> dict[str, Any]:
        """交接中断：保留已确认候选与来源回执，等待重投。"""
        ledger = endpoints.handover_ledger
        if manifest_id is None:
            manifest_id = ledger.latest_manifest_id(open_only=True)
        if manifest_id is None or ledger.manifest(manifest_id) is None:
            return {"ok": False, "reason": "没有进行中的交接"}
        ledger.interrupt(manifest_id)
        return {"ok": True, "manifest_id": manifest_id, "status": "interrupted"}

    def resume_handover(self, relation_ids: list[str]) -> dict[str, Any]:
        """重投：继续未关闭的交接清单，已确认的出处按版本复用。"""
        return self.feed_knowledge_link(relation_ids)

    def published_edge_semantics(self, edge_id: str | None = None) -> dict[str, Any]:
        """已发布边的语义快照；复核更新后仍可按旧版本查回。"""
        snapshots = endpoints.handover_ledger.published_edges(edge_id)
        return {"ok": True, "count": len(snapshots), "edges": snapshots}

    def compute_graph_stats(self) -> dict[str, Any]:
        """图谱统计。"""
        rows = store.rows(self.MODULE)
        entities: set[str] = set()
        rel_type_dist: dict[str, int] = {}
        for row in rows:
            entities.add(str(row.get("源实体", "")))
            entities.add(str(row.get("目标实体", "")))
            rtype = row.get("关系类型", "未知")
            rel_type_dist[rtype] = rel_type_dist.get(rtype, 0) + 1
        return {
            "total_relations": len(rows),
            "unique_entities": len(entities) - (1 if "" in entities else 0),
            "relation_type_distribution": rel_type_dist,
            "validated": sum(1 for r in rows if r.get("status") == "已验证"),
            "ingested": sum(1 for r in rows if r.get("status") == "已入库"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["graph_stats"] = self.compute_graph_stats()
        return base
