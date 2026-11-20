"""关系端点组装、交接与知识链接的回归测试。

覆盖三类要求：
1. 两种历史格式各自的字段、字段次序、方向和外链标识保持不变；
2. 同一条边在交接期间出现两版有向端点时，候选与回执仍按版本成套；
3. 交接中断后重投复用已确认出处，同一事务失败时候选与来源记录一起回滚。
"""
from __future__ import annotations

import copy
import unittest

from app.services import relation_endpoints as endpoints
from app.services.knowledge_link import KnowledgeLinkService
from app.services.relation_graph import RelationGraphService
from app.store import store


ENTITIES = [
    {"实体ID": "e1", "实体名称": "Alpha", "页面来源": "https://pages.example/1"},
    {"实体ID": "e2", "实体名称": "Beta", "页面来源": "https://pages.example/1"},
    {"实体ID": "e3", "实体名称": "Gamma", "页面来源": "https://pages.example/1"},
]


class EndpointCase(unittest.TestCase):
    def setUp(self) -> None:
        endpoints.reset_handover_ledger()
        self.relation = RelationGraphService()
        self.link = KnowledgeLinkService()
        self.original_rows = copy.deepcopy(store.rows("relation_graph"))
        store._tables["relation_graph"] = copy.deepcopy(self.original_rows)

    def tearDown(self) -> None:
        store._tables["relation_graph"] = self.original_rows

    def _publish_edge(self, target: str = "Beta", relation: str = "对比关系") -> dict:
        edge = endpoints.display_edge(
            edge_id="edge-0001",
            source_entity="Alpha",
            target_entity=target,
            relation_type=relation,
            evidence_page="https://pages.example/1",
            evidence="抽取自 https://pages.example/1",
            strength="0.77",
        )
        store.rows("relation_graph").append({"id": 900, "status": "已验证", **edge})
        return edge

    # ---- 两种格式 ---------------------------------------------------------
    def test_display_format_keeps_fields_and_order(self) -> None:
        built = self.relation.build_relations(ENTITIES)
        self.assertTrue(built["ok"])
        self.assertEqual(built["relation_count"], 3)
        for relation in built["relations"]:
            self.assertEqual(tuple(relation), endpoints.DISPLAY_FIELDS)

    def test_handover_format_keeps_fields_and_order(self) -> None:
        edge = self._publish_edge()
        feed = self.relation.feed_knowledge_link([edge["边ID"]])
        self.assertEqual(feed["found_relations"], 1)
        handed = feed["relations"][0]
        self.assertEqual(tuple(handed), endpoints.HANDOVER_FIELDS)
        self.assertEqual(handed["source_entity"], "Alpha")
        self.assertEqual(handed["target_entity"], "Beta")

    def test_projection_and_handover_share_one_rule(self) -> None:
        edge = self._publish_edge()
        feed = self.relation.feed_knowledge_link([edge["边ID"]])
        projection = self.relation.build_endpoint_projection([edge["边ID"]])
        self.assertEqual(projection["edge_count"], 1)
        row = projection["edges"][0]
        self.assertEqual(row["source_entity"], feed["relations"][0]["source_entity"])
        self.assertEqual(row["target_entity"], feed["relations"][0]["target_entity"])
        roles = {endpoint["role"]: endpoint["entity"] for endpoint in projection["endpoints"]}
        self.assertEqual(roles, {"source": "Alpha", "target": "Beta"})
        self.assertEqual(
            {endpoint["version"] for endpoint in projection["endpoints"]},
            {row["version"]},
        )

    def test_direction_is_preserved_for_reversed_edges(self) -> None:
        forward = endpoints.canonical_edge({
            "edge_id": "e", "source_entity": "A", "target_entity": "B",
            "relation_type": "引用关系", "evidence_page": "p",
        })
        backward = endpoints.canonical_edge({
            "edge_id": "e", "source_entity": "B", "target_entity": "A",
            "relation_type": "引用关系", "evidence_page": "p",
        })
        self.assertEqual((forward["source_entity"], forward["target_entity"]), ("A", "B"))
        self.assertEqual((backward["source_entity"], backward["target_entity"]), ("B", "A"))
        self.assertNotEqual(forward["version"], backward["version"])

    def test_link_ids_unchanged_for_legacy_input(self) -> None:
        consumed = self.link.consume_relations([
            {"edge_id": "x", "source_entity": "A", "target_entity": "B",
             "relation_type": "t", "evidence_page": "p"},
        ])
        self.assertEqual(consumed["link_count"], 2)
        self.assertEqual(
            [link["链接ID"] for link in consumed["links"]],
            ["1ccf27badce5", "6e476135ea7d"],
        )
        self.assertEqual(consumed["links"][0]["外部来源"], "relation_graph_pipeline")

    # ---- 端点解释收拢 -----------------------------------------------------
    def test_two_formats_read_to_same_canonical_edge(self) -> None:
        display = {
            "边ID": "e", "源实体": "A", "目标实体": "B", "关系类型": "引用关系",
            "抽取页面": "p", "原文证据": "evidence", "关联强度": "0.5",
        }
        handover = {
            "edge_id": "e", "source_entity": "A", "target_entity": "B",
            "relation_type": "引用关系", "evidence_page": "p",
        }
        self.assertEqual(
            endpoints.canonical_edge(display)["version"],
            endpoints.canonical_edge(handover)["version"],
        )

    def test_mixed_sources_are_rejected(self) -> None:
        with self.assertRaises(endpoints.MixedEdgeFormatError):
            endpoints.canonical_edge({
                "边ID": "e", "源实体": "A", "目标实体": "B",
                "source_entity": "A", "target_entity": "C",
            })
        with self.assertRaises(endpoints.MixedEdgeFormatError):
            endpoints.canonical_edge(
                {"边ID": "e", "源实体": "A", "目标实体": "B"},
                fmt=endpoints.HANDOVER,
            )

    def test_stitched_version_is_rejected(self) -> None:
        edge = endpoints.canonical_edge({
            "edge_id": "e", "source_entity": "A", "target_entity": "B",
            "relation_type": "引用关系", "evidence_page": "p",
        })
        stitched = {**edge, "target_entity": "C", "version": edge["version"]}
        with self.assertRaises(endpoints.EndpointVersionMismatch):
            endpoints.verify_edge_version(stitched)

    # ---- 交接冲突、重投与回滚 ---------------------------------------------
    def test_recheck_conflict_and_resume_reuse_receipt(self) -> None:
        edge = self._publish_edge()
        first = self.relation.feed_knowledge_link([edge["边ID"]])
        manifest_id = endpoints.handover_ledger.latest_manifest_id(open_only=True)
        old = endpoints.canonical_edge(edge, fmt=endpoints.DISPLAY)

        # 先确认旧版出处，外链结果回写清单。
        old_candidates = self.link.handover_candidates(manifest_id, [edge["边ID"]])
        self.assertEqual(old_candidates["candidates"][0]["version"], old["version"])
        old_receipt = endpoints.handover_ledger.receipt(edge["边ID"], old["version"])
        self.assertIsNotNone(old_receipt)
        self.assertEqual(old_receipt["source_entity"], "Alpha")
        self.assertEqual(old_receipt["target_entity"], "Beta")

        # 下一次复核先于外链候选确认到达：目标端被改写，同一 edge_id 出现第二版。
        store.rows("relation_graph")[-1]["目标实体"] = "Gamma-复核"
        store.rows("relation_graph")[-1]["关系类型"] = "引用关系"
        second = self.relation.feed_knowledge_link([edge["边ID"]])
        self.assertEqual(second["found_relations"], 1)
        # 交付格式按历史字段原样输出，版本只存在于台账里的候选与回执。
        self.assertEqual(tuple(second["relations"][0]), endpoints.HANDOVER_FIELDS)
        new = endpoints.canonical_edge(second["relations"][0], fmt=endpoints.HANDOVER)
        status = self.relation.handover_status(manifest_id)
        self.assertEqual(len(status["conflicts"]), 1)
        self.assertEqual(status["conflicts"][0]["known_version"], old["version"])
        self.assertEqual(status["candidates"][1]["version"], new["version"])

        # 交接中断后重投：沿用同一清单，旧回执继续可复用。
        self.relation.interrupt_handover(manifest_id)
        self.relation.resume_handover([edge["边ID"]])
        self.assertEqual(
            endpoints.handover_ledger.latest_manifest_id(open_only=True),
            manifest_id,
        )
        reused = endpoints.handover_ledger.record_receipt(manifest_id, old, old_receipt["links"])
        self.assertEqual(reused["sequence"], old_receipt["sequence"])
        self.assertEqual(reused["version"], old["version"])

        # 外链候选确认：每条候选的回执版本与其端点版本一致，不跨版本拼接。
        confirmed = self.link.handover_candidates(manifest_id)
        self.assertEqual(confirmed["candidate_count"], 2)
        for candidate in confirmed["candidates"]:
            receipt = endpoints.handover_ledger.receipt(
                candidate["edge_id"], candidate["version"])
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["version"], candidate["version"])
            self.assertEqual(receipt["source_entity"], candidate["source_entity"])
            self.assertEqual(receipt["target_entity"], candidate["target_entity"])

        # 已发布边的语义仍可查回，旧版本不因复核消失。
        published = self.relation.published_edge_semantics(edge["边ID"])
        self.assertEqual(
            {snapshot["version"] for snapshot in published["edges"]},
            {old["version"], new["version"]},
        )

    def test_failed_handover_rolls_back_candidates_and_receipts(self) -> None:
        edge = self._publish_edge()
        self.relation.feed_knowledge_link([edge["边ID"]])
        manifest_id = endpoints.handover_ledger.latest_manifest_id(open_only=True)
        candidate = endpoints.canonical_edge(edge, fmt=endpoints.DISPLAY)

        self.link.handover_candidates(manifest_id, [edge["边ID"]])
        self.assertIsNotNone(
            endpoints.handover_ledger.receipt(edge["边ID"], candidate["version"]))

        result = self.link.fail_handover(manifest_id)
        self.assertTrue(result["ok"])
        self.assertIsNone(endpoints.handover_ledger.manifest(manifest_id))
        self.assertIsNone(endpoints.handover_ledger.candidate(edge["边ID"], candidate["version"]))
        self.assertIsNone(endpoints.handover_ledger.receipt(edge["边ID"], candidate["version"]))
        self.assertEqual(endpoints.handover_ledger.published_edges(edge["边ID"]), [])

    def test_projection_rejects_stitched_rows(self) -> None:
        edge = endpoints.canonical_edge({
            "edge_id": "e", "source_entity": "A", "target_entity": "B",
            "relation_type": "引用关系", "evidence_page": "p",
        })
        stitched = {**edge, "target_entity": "C"}
        with self.assertRaises(endpoints.EndpointVersionMismatch):
            endpoints.build_projection([stitched])


if __name__ == "__main__":
    unittest.main()
