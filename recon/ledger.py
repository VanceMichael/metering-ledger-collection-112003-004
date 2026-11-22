"""核销领域逻辑：版本化资料解释、片段归类与分表窗口归集。

设计约束（来自联调规则）：

* 只有运营商的有效桥接凭据能把两个来源片段归为一次交易；撤回后只拆相关交易，
  不按内容相似度自动合并。
* 施工单决定枪位在某时段归哪块表（表号+装表世代）；``replaces`` 后的更正单若
  给出 ``interval_uncertain``，覆盖该闭区间的交易在无独立表端明细时不归任何一侧。
* 旧表清零与新表读数属不同装表世代，绝不能拼成一个累计段。
* 核销结果必须能区分：已能归到枪的用量、只可确认在共表的用量、仍无法确认的部分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .amounts import D, q2


# ---------------------------------------------------------------------------
# 版本化资料（桥接、施工单）
# ---------------------------------------------------------------------------

def latest_known(records: list[dict], shift: int, key: str = "id") -> dict:
    """取同一标识在 shift 时已知的最新版本。"""
    known = [r for r in records if r["known"] <= shift]
    result: dict = {}
    for record in known:
        result[record[key]] = record
    return result


def active_bridges(bridge_versions: list[dict], shift: int) -> dict[str, dict]:
    """shift 时仍有效的桥接（最新版为 active）。"""
    return {bid: rec for bid, rec in latest_known(bridge_versions, shift).items()
            if rec.get("state") == "active"}


def effective_switch(switch_versions: list[dict], shift: int) -> list[dict]:
    """当前生效的施工单：被 replaces 引用的旧单让位于更正单。"""
    newest = latest_known(switch_versions, shift).values()
    replaced: set[str] = set()
    for record in newest:
        replaced.update(record.get("replaces", []))
    return [r for r in newest if r["id"] not in replaced]


def _covering_switch(rules: list[dict], port: str, start: str, end: str,
                     window: str) -> dict | None:
    """找完整覆盖 [start, end) 的确定性接入单（无 interval_uncertain）。"""
    for rule in rules:
        if rule["port"] != port or rule.get("window") != window or "interval_uncertain" in rule:
            continue
        if rule["from"] <= start and end <= rule["until"]:
            return rule
    return None


# ---------------------------------------------------------------------------
# 片段
# ---------------------------------------------------------------------------

@dataclass
class Piece:
    """一条可计量的用电片段（整条账片或账片内的一个 split 分段）。"""
    operator: str
    fragment_id: str          # 运营商内账片号
    day: str
    port: str
    start: str
    end: str
    kwh: Decimal
    meter: str | None = None
    epoch: str | None = None
    status: str = "attributed"   # attributed / meter_only / unconfirmed
    reason: str = ""
    bridge: str | None = None
    source: str = ""             # provenance 标识，见 ledger_sources
    split_index: int | None = None

    def as_dict(self) -> dict:
        data = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}
        return data


def classify_fragment(operator: str, row: dict, rules: list[dict]) -> list[Piece]:
    """把一条账片归成一个或多个 :class:`Piece`，并依据施工单判定可归属性。

    账片自带 split（独立表端明细）时按分段表号/世代归属；与不确定接线区间相交的
    分段不归属任何一侧。无 split 的整条账片只有在确定性施工单完整覆盖时才归表。
    """
    pieces: list[Piece] = []
    if row.get("split"):
        for index, (meter, epoch, start, end, kwh) in enumerate(row["split"]):
            piece = Piece(operator, row["id"], row["day"], row["port"], start, end, D(kwh),
                          meter=meter, epoch=epoch, split_index=index)
            uncertain = [r for r in rules
                         if r["port"] == row["port"] and r.get("window") == row["day"]
                         and "interval_uncertain" in r
                         and start <= r["interval_uncertain"][1]
                         and r["interval_uncertain"][0] < end]
            if uncertain:
                piece.status = "unconfirmed"
                piece.reason = (f"接线时间不确定区间 {uncertain[0]['interval_uncertain']}"
                                f"（施工单 {uncertain[0]['id']}），无独立表端明细可判定侧别")
            pieces.append(piece)
        return pieces

    rule = _covering_switch(rules, row["port"], row["start"], row["end"], row["day"])
    piece = Piece(operator, row["id"], row["day"], row["port"], row["start"], row["end"],
                  D(row["kwh"]))
    if rule:
        piece.meter, piece.epoch = rule["meter"], rule["epoch"]
    else:
        piece.status = "unconfirmed"
        piece.reason = "无确定性施工单完整覆盖该账片区间，不能凭共表余额猜测归表"
    pieces.append(piece)
    return pieces


# ---------------------------------------------------------------------------
# 分表窗口归集
# ---------------------------------------------------------------------------

@dataclass
class WindowReport:
    meter: str
    epoch: str
    window: str
    reading_revision: str | None = None
    window_total: Decimal | None = None       # 同世代首末累积之差
    attributed: Decimal = Decimal("0")        # 已能归到枪
    meter_only: Decimal = Decimal("0")        # 只可确认在共表
    unconfirmed: Decimal = Decimal("0")       # 仍无法确认（已落在该表上的声称量）
    balanced: bool = True
    issue: str = ""
    pieces: list[dict] = field(default_factory=list)
    unattached: list[dict] = field(default_factory=list)  # 无法归到任何表的片段

    def as_dict(self) -> dict:
        data = dict(self.__dict__)
        for key in ("window_total", "attributed", "meter_only", "unconfirmed"):
            if data[key] is not None:
                data[key] = q2(data[key])
        return data


def reading_delta(reading: dict | None) -> Decimal | None:
    """同一装表世代的首末累积之差；读数缺失或口径异常时为 None。"""
    if not reading or not reading.get("bottoms"):
        return None
    bottoms = reading["bottoms"]
    start = D(bottoms[0][1])
    end = D(bottoms[-1][1])
    if end < start:
        # 旧表清零后跨世代读数不能相减，出现负差说明口径/世代不匹配。
        return None
    return end - start


def aggregate_window(meter: str, epoch: str, window: str, pieces: list[Piece],
                     reading: dict | None, unattached: list[Piece]) -> WindowReport:
    """把落在某分表窗口上的片段与表底读数核对成三桶报告。"""
    report = WindowReport(meter, epoch, window,
                          reading_revision=reading.get("revision") if reading else None,
                          window_total=reading_delta(reading))
    claimed = Decimal("0")
    for piece in pieces:
        if piece.meter != meter or piece.epoch != epoch:
            continue
        report.pieces.append(piece.as_dict())
        claimed += piece.kwh
        if piece.status == "attributed":
            report.attributed += piece.kwh
        elif piece.status == "unconfirmed":
            report.unconfirmed += piece.kwh
    report.unattached = [p.as_dict() for p in unattached]

    if report.window_total is None:
        report.balanced = False
        report.issue = "缺经施工确认的读数或累计口径不匹配（注意跨世代不能相减）"
        return report

    if claimed > report.window_total:
        report.balanced = False
        report.issue = (f"账片声称 {q2(claimed)} 超过表底窗口总量 {q2(report.window_total)}，"
                        "总量对不平，先标待核")
        report.meter_only = report.window_total - report.attributed
        return report

    # 共表余量 = 窗口总量 - 已归枪 - 无法确认部分；未归枪的余量留在共表窗口。
    report.meter_only = report.window_total - report.attributed - report.unconfirmed
    return report


def effective_rows(day_state: dict) -> list[dict]:
    """取一个运营商-营业日当前有效的账片（剔除撤回、优先归档版）。"""
    rows: dict[str, dict] = {}
    # 实时/流水来源
    for row in day_state.get("live_rows", []):
        rows[row["id"]] = row
    for ref in day_state.get("retracted", []):
        rows.pop(ref, None)
    # 归档清单是完整日账的权威来源，到达后覆盖实时视图。
    if day_state.get("archive_complete"):
        rows = {row["id"]: row for row in day_state.get("archive_rows", [])}
    return list(rows.values())


def provenance_of(day_state: dict, seal: dict | None = None) -> str:
    if day_state.get("archive_complete"):
        return f"archive:{day_state['archive_edition']}"
    if day_state.get("live_complete"):
        return f"live:{day_state.get('live_edition')}"
    if seal is not None:
        return f"live:{seal['edition']}"   # 新运营商由有效封账确认的实时完整日账
    return "incomplete"


def bridge_groups(bridge_versions: list[dict], shift: int) -> list[dict]:
    """按桥接凭据列示跨网交易分组及当前有效性；不做任何内容相似合并。"""
    active = active_bridges(bridge_versions, shift)
    newest = latest_known(bridge_versions, shift)
    groups: dict[str, list[dict]] = {}
    for bridge_id, record in newest.items():
        groups[bridge_id] = [list(link) for link in record["links"]]
    return [
        {"bridge": bid, "links": links, "state": "active" if bid in active else "withdrawn"}
        for bid, links in sorted(groups.items())
    ]
