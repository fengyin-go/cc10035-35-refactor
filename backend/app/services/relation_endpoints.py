"""关系边有向端点的统一组装、版本与交接台账。

关系图谱与知识链接原先各自解释一条边的两个端点：

* ``RelationGraphService.build_relations`` —— 建边与列表/导出使用的显示格式；
* ``RelationGraphService.feed_knowledge_link`` —— 交接给知识链接的交付格式；
* ``KnowledgeLinkService.consume_relations`` —— 旧外链输入，与交付格式同形。

三处字段名不同，但“哪一端是源、哪一端是目标”只能有一种解释。本模块把它收拢到
``canonical_edge`` 一个入口：两种历史格式都读成
``(edge_id, source_entity, target_entity, relation_type, evidence_page)``，
方向原样保留，并据此计算 ``version``。建边结果、图谱交接、端点投影数据集和旧外链
输入都只认这一份结果，不再各写一遍组装。

两种历史格式的字段与字段次序保持不变：

* ``DISPLAY_FIELDS``  —— 建边与列表的显示格式；
* ``HANDOVER_FIELDS`` —— 图谱交接清单、端点投影数据集与旧外链输入的交付格式。

``HandoverLedger`` 记录一次交接事务的候选、来源回执和已发布边语义快照。事务失败
时 ``rollback`` 把候选与回执一起回滚；交接中断（没有执行回滚）时，已确认的回执留在
台账里，重投复用同一版端点，不会拿旧源端和新目标端拼出一条边。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable


DISPLAY = "display"
HANDOVER = "handover"

# 历史显示格式：字段和次序都不能动。列表接口、CSV 导出都按这份次序生成。
DISPLAY_FIELDS = (
    "边ID",
    "源实体",
    "目标实体",
    "关系类型",
    "抽取页面",
    "原文证据",
    "关联强度",
)

# 历史交付格式：交接清单、端点投影与旧外链输入共用。字段和次序同样不能动。
HANDOVER_FIELDS = (
    "edge_id",
    "source_entity",
    "target_entity",
    "relation_type",
    "evidence_page",
)

DISPLAY_KEYS = frozenset(DISPLAY_FIELDS)
HANDOVER_KEYS = frozenset(HANDOVER_FIELDS)

# 两种格式归一后的公共字段，顺序固定，便于比较、排查和生成投影。
CANONICAL_FIELDS = (
    "edge_id",
    "source_entity",
    "target_entity",
    "relation_type",
    "evidence_page",
)


class EdgeFormatError(ValueError):
    """边记录无法按历史格式解释。"""


class UnknownEdgeFormatError(EdgeFormatError):
    """记录里没有任一种历史格式的字段。"""


class MixedEdgeFormatError(EdgeFormatError):
    """同一条边同时出现两种格式的字段，拒绝把两种来源拼成一条边。"""


class EndpointVersionMismatch(ValueError):
    """端点与其声明版本不一致，可能是旧源端和新目标端被拼在一起。"""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def detect_format(record: dict[str, Any]) -> str:
    """识别一条边使用的是哪种历史格式。

    两种格式的字段不会同时出现在同一条边里；一旦同时出现，说明调用方把两条
    来源拼在了一起，这里直接拒绝，避免下游拿到一条方向解释不清的边。
    """
    keys = set(record)
    has_display = bool(keys & DISPLAY_KEYS)
    has_handover = bool(keys & HANDOVER_KEYS)
    if has_display and has_handover:
        raise MixedEdgeFormatError(
            "边记录同时包含显示格式与交付格式字段，不能把两种来源拼成一条边"
        )
    if has_display:
        return DISPLAY
    if has_handover:
        return HANDOVER
    raise UnknownEdgeFormatError("边记录既不是显示格式也不是交付格式")


def edge_version(edge: dict[str, Any]) -> str:
    """计算一条有向边的端点版本。

    版本覆盖边标识、源端、目标端和关系类型，方向参与计算：``A→B`` 与 ``B→A``
    是两条不同的边。同一 ``edge_id`` 只要任一端被复核改写，版本就会变化。
    """
    parts = (
        _text(edge.get("edge_id")),
        _text(edge.get("source_entity")),
        _text(edge.get("target_entity")),
        _text(edge.get("relation_type")),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def verify_edge_version(edge: dict[str, Any]) -> str:
    """复查边自带的 ``version`` 是否仍由当前这对端点算得。

    候选、回执在交接期间带着版本流转；一旦有人只替换了其中一端而没有重算版本，
    这里会立即拒绝。返回重新计算出的版本。
    """
    actual = edge_version(edge)
    declared = _text(edge.get("version"))
    if declared and declared != actual:
        raise EndpointVersionMismatch(
            f"边 {_text(edge.get('edge_id'))} 的端点版本 {declared} 与端点不符（应为 {actual}）"
        )
    return actual


def canonical_edge(
    record: dict[str, Any],
    *,
    fmt: str | None = None,
    allow_blank: bool = False,
) -> dict[str, Any]:
    """把任一种历史格式读成同一份关系边输入。

    显示格式的 ``原文证据``/``关联强度`` 不属于交付格式，但会作为附加字段带在
    结果里，方便需要时按显示格式原样回显。源端与目标端绝不交换。
    """
    detected = detect_format(record)
    if fmt is None:
        fmt = detected
    elif fmt not in (DISPLAY, HANDOVER):
        raise EdgeFormatError(f"未知的边格式：{fmt}")
    elif fmt != detected:
        # 声明格式与记录内容不一致，同样是拼接来源，不能继续。
        raise MixedEdgeFormatError(
            f"边记录声明为 {fmt} 格式，实际字段属于 {detected} 格式"
        )

    if fmt == DISPLAY:
        edge_id = record.get("边ID")
        source = record.get("源实体")
        target = record.get("目标实体")
        relation = record.get("关系类型")
        page = record.get("抽取页面")
    else:
        edge_id = record.get("edge_id")
        source = record.get("source_entity")
        target = record.get("target_entity")
        relation = record.get("relation_type")
        page = record.get("evidence_page")

    if not allow_blank:
        unfinished = [
            name
            for name, value in (("边标识", edge_id), ("源实体", source), ("目标实体", target))
            if not _text(value).strip()
        ]
        if unfinished:
            raise EdgeFormatError(f"边记录缺少{'、'.join(unfinished)}")

    edge: dict[str, Any] = {
        "edge_id": edge_id,
        "source_entity": source,
        "target_entity": target,
        "relation_type": relation,
        "evidence_page": page,
    }
    edge["version"] = edge_version(edge)
    edge["format"] = fmt
    if fmt == DISPLAY:
        # 仅显示格式拥有的两列，按原值携带，不参与版本计算。
        edge["evidence"] = record.get("原文证据")
        edge["strength"] = record.get("关联强度")
    return edge


def render_edge(edge: dict[str, Any], fmt: str) -> dict[str, Any]:
    """按历史格式的字段次序回显一条归一后的边。

    交付格式刻意不含版本等内部字段，保持旧接口输出逐字段不变。
    """
    if fmt == DISPLAY:
        return {
            "边ID": edge.get("edge_id"),
            "源实体": edge.get("source_entity"),
            "目标实体": edge.get("target_entity"),
            "关系类型": edge.get("relation_type"),
            "抽取页面": edge.get("evidence_page"),
            "原文证据": edge.get("evidence"),
            "关联强度": edge.get("strength"),
        }
    if fmt == HANDOVER:
        return {
            "edge_id": edge.get("edge_id"),
            "source_entity": edge.get("source_entity"),
            "target_entity": edge.get("target_entity"),
            "relation_type": edge.get("relation_type"),
            "evidence_page": edge.get("evidence_page"),
        }
    raise EdgeFormatError(f"未知的边格式：{fmt}")


def display_edge(
    *,
    edge_id: Any,
    source_entity: Any,
    target_entity: Any,
    relation_type: Any,
    evidence_page: Any,
    evidence: Any,
    strength: Any,
) -> dict[str, Any]:
    """按历史显示格式的字段次序构造一条边。"""
    return {
        "边ID": edge_id,
        "源实体": source_entity,
        "目标实体": target_entity,
        "关系类型": relation_type,
        "抽取页面": evidence_page,
        "原文证据": evidence,
        "关联强度": strength,
    }


def endpoint_pair(edge: dict[str, Any]) -> tuple[str, str]:
    """返回带方向的两个端点，供冲突提示与回执核对使用。"""
    return _text(edge.get("source_entity")), _text(edge.get("target_entity"))


def project_edge(edge: dict[str, Any]) -> dict[str, Any]:
    """把一条已校验的边投影成端点数据集里的一行。

    投影行同时带上边版本与两个方向明确的端点，知识链接只从这里取端点，
    不再自己去 ``source_entity``/``target_entity`` 之外另拼一份。
    """
    version = verify_edge_version(edge)
    return {
        "edge_id": edge.get("edge_id"),
        "version": version,
        "source_entity": edge.get("source_entity"),
        "target_entity": edge.get("target_entity"),
        "relation_type": edge.get("relation_type"),
        "evidence_page": edge.get("evidence_page"),
    }


def build_projection(edges: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """生成关系端点投影数据集：每条边一行、两个端点成套且属于同一版本。"""
    projected: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    for edge in edges:
        row = project_edge(edge)
        projected.append(row)
        for role, entity in (("source", row["source_entity"]), ("target", row["target_entity"])):
            endpoints.append({
                "edge_id": row["edge_id"],
                "version": row["version"],
                "role": role,
                "entity": entity,
            })
    return {
        "ok": True,
        "edge_count": len(projected),
        "endpoint_count": len(endpoints),
        "edges": projected,
        "endpoints": endpoints,
    }


def same_version(edge: dict[str, Any], other: dict[str, Any]) -> bool:
    """两条记录是否描述同一版有向边（边标识、方向和端点全部一致）。"""
    return (
        _text(edge.get("edge_id")) == _text(other.get("edge_id"))
        and _text(edge.get("source_entity")) == _text(other.get("source_entity"))
        and _text(edge.get("target_entity")) == _text(other.get("target_entity"))
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandoverLedger:
    """边交接台账：候选、来源回执、已发布边语义快照。

    台账只保存归一后的边，不依赖存储行；因此存储里的边被复核更新后，
    已发布版本的语义仍然可以按 ``published_edges`` 查回。
    """

    def __init__(self) -> None:
        self._candidates: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        self._receipts: dict[tuple[str, str], dict[str, Any]] = {}
        self._published: dict[str, list[dict[str, Any]]] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._closed: set[str] = set()
        self._checkpoints: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._manifest_sequence = 0

    # ---- 查询 -------------------------------------------------------------
    def candidate(self, edge_id: str, version: str) -> dict[str, Any] | None:
        for candidates in self._candidates.values():
            edge = candidates.get((edge_id, version))
            if edge is not None:
                return edge
        return None

    def candidates_for(self, edge_id: str) -> list[dict[str, Any]]:
        return [
            edge
            for candidates in self._candidates.values()
            for (rid, _version), edge in candidates.items()
            if rid == edge_id
        ]

    def receipt(self, edge_id: str, version: str) -> dict[str, Any] | None:
        return self._receipts.get((edge_id, version))

    def receipts_for(self, edge_id: str) -> list[dict[str, Any]]:
        return [
            receipt for (rid, _version), receipt in self._receipts.items()
            if rid == edge_id
        ]

    def latest_receipt(self, edge_id: str) -> dict[str, Any] | None:
        receipts = self.receipts_for(edge_id)
        if not receipts:
            return None
        return max(receipts, key=lambda item: int(item.get("sequence", 0)))

    def manifest(self, manifest_id: str) -> dict[str, Any] | None:
        return self._manifests.get(manifest_id)

    def latest_manifest_id(self, *, open_only: bool = False) -> str | None:
        """取最近一次交接清单；``open_only`` 时只看未关闭的。"""
        manifests = [
            manifest for manifest in self._manifests.values()
            if not open_only or manifest.get("status") != "closed"
        ]
        if not manifests:
            return None
        return max(
            manifests,
            key=lambda item: int(str(item["manifest_id"]).rsplit("-", 1)[-1]),
        )["manifest_id"]

    def find_open_manifest(self, edge_ids: Iterable[str]) -> str | None:
        """找包含这些边的未关闭清单，供中断后重投继续同一个事务。"""
        wanted = {_text(edge_id) for edge_id in edge_ids}
        if not wanted:
            return None
        matched = [
            manifest for manifest in self._manifests.values()
            if manifest.get("status") != "closed"
            and any(_text(edge.get("edge_id")) in wanted for edge in manifest["candidates"])
        ]
        if not matched:
            return None
        return max(
            matched,
            key=lambda item: int(str(item["manifest_id"]).rsplit("-", 1)[-1]),
        )["manifest_id"]

    def is_closed(self, manifest_id: str) -> bool:
        return manifest_id in self._closed

    def published_edges(self, edge_id: str | None = None) -> list[dict[str, Any]]:
        """返回已发布边的语义快照；不传 ``edge_id`` 时返回全部。"""
        if edge_id is None:
            snapshots = [
                snapshot
                for versions in self._published.values()
                for snapshot in versions
            ]
            return sorted(snapshots, key=lambda item: int(item.get("sequence", 0)))
        return list(self._published.get(edge_id, []))

    # ---- 交接事务 ---------------------------------------------------------
    def next_manifest_id(self) -> str:
        self._manifest_sequence += 1
        return f"handover-{self._manifest_sequence:06d}"

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def begin(self, manifest_id: str, candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """开启一次交接：先留检查点，再登记候选，供失败时整体回滚。"""
        self.checkpoint(manifest_id)
        manifest: dict[str, Any] = {
            "manifest_id": manifest_id,
            "status": "open",
            "candidates": [],
            "receipts": [],
            "published": [],
            "conflicts": [],
        }
        self._manifests[manifest_id] = manifest
        self._candidates[manifest_id] = {}
        self.extend(manifest_id, candidates)
        return manifest

    def extend(self, manifest_id: str, candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """向已有交接清单补充候选。

        同一 ``edge_id`` 若已是另一个版本，登记为端点冲突并保留两版候选：交接期间
        两版有向端点都可能存在，但每条候选自身仍成套属于同一个版本。
        """
        manifest = self._manifests[manifest_id]
        bucket = self._candidates.setdefault(manifest_id, {})
        for edge in candidates:
            edge = canonical_edge(edge, allow_blank=False)
            previous = [
                candidate
                for (rid, _version), candidate in bucket.items()
                if rid == edge["edge_id"]
            ]
            if previous and all(candidate["version"] != edge["version"] for candidate in previous):
                self.record_conflict(manifest_id, edge["edge_id"], previous[0], edge)
            bucket[(edge["edge_id"], edge["version"])] = copy.deepcopy(edge)
            if all(
                candidate["version"] != edge["version"]
                for candidate in manifest["candidates"]
            ):
                manifest["candidates"].append(copy.deepcopy(edge))
        return manifest

    def checkpoint(self, manifest_id: str) -> None:
        """在事务开始前留一份台账快照，失败时整体回滚。"""
        self._checkpoints[manifest_id] = copy.deepcopy({
            "candidates": self._candidates,
            "receipts": self._receipts,
            "published": self._published,
            "manifests": self._manifests,
            "closed": self._closed,
        })

    def rollback(self, manifest_id: str) -> bool:
        """同一交接事务失败：候选与来源记录一起回到检查点。"""
        state = self._checkpoints.pop(manifest_id, None)
        if state is None:
            return False
        self._candidates = state["candidates"]
        self._receipts = state["receipts"]
        self._published = state["published"]
        self._manifests = state["manifests"]
        self._closed = state["closed"]
        return True

    def commit(self, manifest_id: str) -> None:
        """交接确认完成：丢弃检查点并关闭清单。"""
        self._checkpoints.pop(manifest_id, None)
        self._closed.add(manifest_id)
        manifest = self._manifests.get(manifest_id)
        if manifest is not None:
            manifest["status"] = "closed"

    def interrupt(self, manifest_id: str) -> None:
        """交接中断：保留候选与已确认回执，等待重投；检查点留给重投复用。"""
        manifest = self._manifests.get(manifest_id)
        if manifest is not None:
            manifest["status"] = "interrupted"

    def record_conflict(
        self,
        manifest_id: str,
        edge_id: str,
        known: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        """记录同一 ``edge_id`` 的两版有向端点，供建边入口同步显示。"""
        conflict = {
            "edge_id": edge_id,
            "known_version": known.get("version"),
            "known_source_entity": known.get("source_entity"),
            "known_target_entity": known.get("target_entity"),
            "incoming_version": incoming.get("version"),
            "incoming_source_entity": incoming.get("source_entity"),
            "incoming_target_entity": incoming.get("target_entity"),
        }
        manifest = self._manifests.get(manifest_id)
        if manifest is not None:
            manifest["conflicts"].append(conflict)
        return conflict

    def record_receipt(
        self,
        manifest_id: str,
        edge: dict[str, Any],
        links: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """写下来源回执并把外链处理结果回写到交接清单。

        同一 ``(edge_id, version)`` 的回执一旦确认就复用，重投时不会重新解释出处。
        """
        key = (edge["edge_id"], edge["version"])
        existing = self._receipts.get(key)
        if existing is not None:
            manifest = self._manifests.get(manifest_id)
            if manifest is not None and existing not in manifest["receipts"]:
                manifest["receipts"].append(existing)
            return existing

        link_list = [dict(link) for link in links]
        receipt = {
            "edge_id": edge.get("edge_id"),
            "version": edge.get("version"),
            "source_entity": edge.get("source_entity"),
            "target_entity": edge.get("target_entity"),
            "relation_type": edge.get("relation_type"),
            "evidence_page": edge.get("evidence_page"),
            "link_ids": [link.get("链接ID") for link in link_list],
            "links": link_list,
            "manifest_id": manifest_id,
            "sequence": self.next_sequence(),
            "confirmed_at": utc_now(),
        }
        self._receipts[key] = receipt
        manifest = self._manifests.get(manifest_id)
        if manifest is not None:
            manifest["receipts"].append(receipt)
        return receipt

    def publish(self, manifest_id: str, edge: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        """保存一条已发布边的语义快照，按 ``(edge_id, version)`` 幂等。"""
        versions = self._published.setdefault(edge["edge_id"], [])
        for snapshot in versions:
            if snapshot["version"] == edge["version"]:
                return snapshot
        snapshot = {
            "edge_id": edge.get("edge_id"),
            "version": edge.get("version"),
            "source_entity": edge.get("source_entity"),
            "target_entity": edge.get("target_entity"),
            "relation_type": edge.get("relation_type"),
            "evidence_page": edge.get("evidence_page"),
            "link_ids": list(receipt.get("link_ids", [])),
            "external_links": [dict(link) for link in receipt.get("links", [])],
            "manifest_id": manifest_id,
            "sequence": self.next_sequence(),
            "published_at": utc_now(),
        }
        versions.append(snapshot)
        manifest = self._manifests.get(manifest_id)
        if manifest is not None:
            manifest["published"].append(snapshot)
        return snapshot


# 进程内共享台账：关系图谱写候选、知识链接写回执，两侧看到同一份交接状态。
handover_ledger = HandoverLedger()


def reset_handover_ledger() -> None:
    """测试与重放时重置台账；正常启动不需要调用。"""
    global handover_ledger
    handover_ledger = HandoverLedger()
