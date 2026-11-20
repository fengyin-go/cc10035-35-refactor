"""知识链接领域服务：管理实体与外部知识的链接关系。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services import relation_endpoints as endpoints
from app.services.base import BaseModuleService
from app.store import store


LINK_TYPES = ["wiki", "dbpedia", "knowledge_panel", "authority_page", "linkedin"]


class KnowledgeLinkService(BaseModuleService):
    MODULE = "knowledge_link"
    ENTITY = "链接记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["链接ID", "源实体", "外部URI", "链接类型", "外部来源", "验证状态", "上次验证"]
    REQUIRED_FIELDS = ["链接ID", "源实体", "外部URI"]
    STATUS_ORDER = ["待验证", "已验证", "已失效", "已移除"]
    ACTION_RULES = {"验证链接": "待验证", "标记失效": "已验证", "移除链接": "已验证"}
    NEGATIVE_ACTIONS = []

    # ---- 端点来源 ---------------------------------------------------------
    def consume_relations(self, relation_graph_edges: list[dict[str, Any]]) -> dict[str, Any]:
        """消费关系图谱的已验证边，为实体建立外部知识链接。

        入参仍是历史交付格式的边（旧外链输入）。端点不再在本方法里从
        ``source_entity``/``target_entity`` 各取一次，而是先交给
        ``endpoints.canonical_edge`` 归一、核对版本，再由投影数据集按同一规则
        取实际端点，保证两个端点成套属于同一版有向边。
        """
        if not relation_graph_edges:
            return {"ok": True, "link_count": 0, "links": []}

        canonical = [
            endpoints.canonical_edge(edge, fmt=endpoints.HANDOVER, allow_blank=False)
            for edge in relation_graph_edges
        ]
        projection = endpoints.build_projection(canonical)
        links = [
            link
            for row in projection["edges"]
            for link in self._links_for_edge(row)
        ]
        return {
            "ok": True,
            "input_edges": len(relation_graph_edges),
            "link_count": len(links),
            "links": links,
        }

    def consume_projection(self, projection: dict[str, Any]) -> dict[str, Any]:
        """消费关系端点投影数据集，不再自己解释端点。

        投影里的每一行都带着边版本；这里逐行复查端点仍属于同一版本，
        然后按两个方向角色成套取得端点。
        """
        edges = projection.get("edges", [])
        links: list[dict[str, Any]] = []
        for row in edges:
            endpoints.verify_edge_version(row)
            links.extend(self._links_for_edge(row))
        return {
            "ok": True,
            "input_edges": len(edges),
            "link_count": len(links),
            "links": links,
        }

    def handover_candidates(
        self,
        manifest_id: str | None = None,
        relation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """外链候选确认：从交接清单取边、生成外链、把回执回写到清单。

        同一 ``(edge_id, version)`` 的回执已经存在时直接复用——即使交接中断过、
        或者该边后来被复核成新版本，旧版本已确认的出处仍然可以查回。
        """
        ledger = endpoints.handover_ledger
        if manifest_id is None:
            manifest_id = ledger.latest_manifest_id(open_only=True)
        if manifest_id is None or ledger.manifest(manifest_id) is None:
            return {"ok": False, "reason": "没有进行中的交接清单"}

        manifest = ledger.manifest(manifest_id)
        records = list(manifest["candidates"])
        if relation_ids is not None:
            wanted = set(relation_ids)
            records = [record for record in records if record.get("edge_id") in wanted]

        results: list[dict[str, Any]] = []
        for record in records:
            # 候选在台账里就是规范边；先复查声明的版本仍由这对端点算得。
            edge = dict(record)
            endpoints.verify_edge_version(edge)
            links = self._links_for_edge(endpoints.project_edge(edge))
            receipt = ledger.record_receipt(manifest_id, edge, links)
            ledger.publish(manifest_id, edge, receipt)
            results.append({
                "edge_id": edge["edge_id"],
                "version": edge["version"],
                "source_entity": edge["source_entity"],
                "target_entity": edge["target_entity"],
                "link_ids": list(receipt["link_ids"]),
                "links": [dict(link) for link in receipt["links"]],
            })

        return {
            "ok": True,
            "manifest_id": manifest_id,
            "candidate_count": len(results),
            "candidates": results,
        }

    def fail_handover(self, manifest_id: str) -> dict[str, Any]:
        """同一交接事务失败：回滚候选与来源记录，避免半成品外链留下。"""
        ledger = endpoints.handover_ledger
        restored = ledger.rollback(manifest_id)
        return {
            "ok": restored,
            "manifest_id": manifest_id,
            "rolled_back": restored,
            "reason": "" if restored else "没有可回滚的检查点",
        }

    def complete_handover(self, manifest_id: str) -> dict[str, Any]:
        """交接确认完成：关闭清单，已发布边语义继续可查。"""
        ledger = endpoints.handover_ledger
        if ledger.manifest(manifest_id) is None:
            return {"ok": False, "manifest_id": manifest_id, "reason": "交接清单不存在"}
        ledger.commit(manifest_id)
        return {"ok": True, "manifest_id": manifest_id, "status": "closed"}

    def _links_for_edge(self, edge: dict[str, Any]) -> list[dict[str, Any]]:
        """按投影行里的两个方向端点各生成一条外链。"""
        links: list[dict[str, Any]] = []
        for entity in (edge.get("source_entity"), edge.get("target_entity")):
            if not entity:
                continue
            ehash = int(hashlib.md5(str(entity).encode()).hexdigest()[:4], 16)
            link_type = LINK_TYPES[ehash % len(LINK_TYPES)]
            link_id = hashlib.sha256(f"{entity}|{link_type}|ext".encode()).hexdigest()[:12]
            external_uri = f"https://{link_type}.example.com/entity/{entity}"
            links.append({
                "链接ID": link_id,
                "源实体": entity,
                "外部URI": external_uri,
                "链接类型": link_type,
                "外部来源": "relation_graph_pipeline",
                "验证状态": "自动生成",
                "上次验证": "2026-11-19T10:00:00Z",
            })
        return links

    def build_aggregate_input(self) -> dict[str, Any]:
        """构建聚合视图的输入数据。"""
        rows = store.rows(self.MODULE)
        link_type_dist: dict[str, int] = {}
        for row in rows:
            ltype = row.get("链接类型", "未知")
            link_type_dist[ltype] = link_type_dist.get(ltype, 0) + 1
        return {
            "total_links": len(rows),
            "verified_links": sum(1 for r in rows if r.get("status") == "已验证"),
            "invalid_links": sum(1 for r in rows if r.get("status") == "已失效"),
            "link_type_distribution": link_type_dist,
        }

    def validate_link_batch(self) -> dict[str, Any]:
        """批量验证待验证的链接。"""
        pending = [r for r in store.rows(self.MODULE) if r.get("status") == "待验证"]
        validated = 0
        for row in pending:
            link_id = row.get("id")
            if link_id:
                existing = store.find(self.MODULE, int(link_id))
                if existing:
                    existing["验证状态"] = "有效"
                    existing["上次验证"] = "2026-11-19T10:00:00Z"
                    validated += 1
        return {"ok": True, "pending_count": len(pending), "validated_count": validated}

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["link_stats"] = self.build_aggregate_input()
        return base
