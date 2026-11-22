"""收件核销引擎。

一次 ``run_once()`` 完成一轮收件：同步轮次 → 取静态资料 → 拉两家运营商日账 →
维护归档任务/分片 → 领分表读数 → 核销归集 → 推进会计交接。所有动作基于落盘
状态，进程重启后从同一状态目录继续，不重复申请归档、不重复发送交接。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .amounts import q2
from .client import SourceClient, SourceError
from .ledger import (Piece, active_bridges, aggregate_window, classify_fragment,
                     effective_rows, effective_switch, provenance_of)
from .store import StateStore


class ReconciliationEngine:
    def __init__(self, base_url: str, state_dir: str = "recon_state"):
        self.client = SourceClient(base_url)
        self.store = StateStore(state_dir)
        self.state = self.store.state

    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        self.sync_shift()
        self.refresh_static()
        self.discover_days()
        self.collect_legacy()
        self.collect_feed()
        self.manage_archives()
        self.collect_readings()
        reports = self.reconcile()
        self.advance_handoffs(reports)
        self.store.save()
        return self.status()

    # ------------------------------------------------------------------
    def sync_shift(self) -> None:
        info = self.client.get_json("/")
        self.state["shift"] = info["shift"]

    def refresh_static(self) -> None:
        self.state["bridges"] = self.client.get_json("/data/bridges.json")
        self.state["switch"] = self.client.get_json("/data/switch.json")
        self.state["close"] = self.client.get_json("/data/close.json")

    def discover_days(self) -> None:
        tracked = set(self.state.setdefault("tracked_days", []))
        for entry in self.state["close"]:
            tracked.add(entry["day"])
        for event in self.state["events"]:
            row = event.get("row")
            if row:
                tracked.add(row["day"])
            if event.get("day"):
                tracked.add(event["day"])
        for job in self.state["archive_jobs"].values():
            tracked.add(job["day"])
        self.state["tracked_days"] = sorted(tracked)

    def day_state(self, operator: str, day: str) -> dict:
        key = f"{operator}|{day}"
        return self.state["days"].setdefault(key, {
            "operator": operator, "day": day,
            "live_rows": [], "retracted": [], "seals": [],
            "live_complete": False, "live_edition": None,
            "compact_affected": False, "expired": False, "not_offered": False,
            "archive_complete": False, "archive_edition": None, "archive_rows": [],
            "missing_evidence": [],
        })

    # ------------------------------------------------------------------
    # 旧运营商：按修订/游标翻页，末页确认完整，409 重读新版，410 转归档
    # ------------------------------------------------------------------
    def collect_legacy(self) -> None:
        for day in self.state["tracked_days"]:
            ds = self.day_state("legacy", day)
            if ds["archive_complete"]:
                continue
            # 409 后在同一轮内重读新修订，保证末页确认不会被一次游标失效拖过窗口。
            for _attempt in range(4):
                cursor = ds.get("cursor", 0)
                edition = ds.get("edition")
                path = f"/legacy/day?day={day}"
                if edition is not None:
                    path += f"&edition={edition}&cursor={cursor}"
                try:
                    page = self.client.get_json(path)
                except SourceError as error:
                    if error.status == 409:
                        # 旧游标随修订失效：同一版页可以重读，但绝不能跨修订拼页。
                        ds["edition"] = None
                        ds["cursor"] = 0
                        ds["live_rows"] = []
                        ds["live_complete"] = False
                        self.store.append_log("legacy_cursor_stale", day=day,
                                              current=error.payload.get("edition"))
                        continue
                    if error.status == 410:
                        ds["expired"] = True
                        self.store.append_log("legacy_expired", day=day)
                        break
                    raise
                ds["edition"] = page["edition"]
                for row in page["rows"]:
                    if not any(existing["id"] == row["id"] for existing in ds["live_rows"]):
                        ds["live_rows"].append(row)
                if page["next"] is None:
                    # 同一修订读到末页，才确认该日实时日账完整。
                    ds["live_complete"] = True
                    ds["live_edition"] = page["edition"]
                    ds["cursor"] = 0
                    self.store.append_log("legacy_live_complete", day=day, edition=page["edition"])
                else:
                    ds["cursor"] = page["next"]
                break

    def track_day(self, day: str) -> None:
        """显式把相邻营业日纳入追踪（例如归档包 includes 中的相邻日）。"""
        tracked = set(self.state["tracked_days"])
        tracked.add(day)
        self.state["tracked_days"] = sorted(tracked)

    # ------------------------------------------------------------------
    # 新运营商：入账序变化流水，封账/撤回/压实
    # ------------------------------------------------------------------
    def collect_feed(self) -> None:
        while True:
            after = self.state["feed_after"]
            try:
                page = self.client.get_json(f"/new/feed?after={after}")
            except SourceError as error:
                if error.status != 410:
                    raise
                floor = error.payload["floor"]
                self.state["feed_floor"] = floor
                for day in error.payload.get("affected_days", []):
                    self.day_state("new", day)["compact_affected"] = True
                    if day not in self.state["feed_compacted_days"]:
                        self.state["feed_compacted_days"].append(day)
                self.state["feed_after"] = max(after, floor)
                self.store.append_log("feed_compacted", floor=floor,
                                      affected=error.payload.get("affected_days", []))
                continue
            events = page["events"]
            for event in events:
                self.state["events"].append(event)
                self._apply_feed_event(event)
            if events:
                self.state["feed_after"] = max(self.state["feed_after"], events[-1]["id"])
            if not page["more"]:
                break

    def _apply_feed_event(self, event: dict) -> None:
        action = event["action"]
        if action == "record":
            row = event["row"]
            ds = self.day_state("new", row["day"])
            ds["live_rows"] = [r for r in ds["live_rows"] if r["id"] != row["id"]]
            ds["live_rows"].append(row)
        elif action == "retract":
            ds = self.day_state("new", event["day"])
            if event["ref"] not in ds["retracted"]:
                ds["retracted"].append(event["ref"])
            ds["live_rows"] = [r for r in ds["live_rows"] if r["id"] != event["ref"]]
            self.store.append_log("feed_retract", day=event["day"], ref=event["ref"])
        elif action == "seal":
            ds = self.day_state("new", event["day"])
            ds["seals"].append({"edition": event["edition"], "through": event["through"],
                                "event_id": event["id"]})

    def _seal_valid(self, ds: dict) -> dict | None:
        """封账必须覆盖该日所有已知事实；封账后的 record/retract 使旧封账失效。

        ``through`` 覆盖的是事实序号（record/retract），不包含 seal 事件本身，
        因此只按事实事件判定：封账之后到达影响该日的事实即作废。
        """
        if ds["compact_affected"]:
            return None
        fact_ids = [e["id"] for e in self.state["events"]
                    if e["action"] in ("record", "retract")
                    and (e.get("day") or e.get("row", {}).get("day")) == ds["day"]]
        latest_fact = max(fact_ids, default=0)
        for seal in reversed(ds["seals"]):
            if seal["through"] >= latest_fact:
                return seal
        return None

    def _day_complete(self, ds: dict) -> bool:
        if ds["archive_complete"] or ds["live_complete"]:
            return True
        if ds["operator"] == "new":
            return self._seal_valid(ds) is not None
        return False

    # ------------------------------------------------------------------
    # 归档：名额、幂等申请、按需分片、行数/摘要核对、清单确认
    # ------------------------------------------------------------------
    def manage_archives(self) -> None:
        overview = self.client.get_json("/archive/jobs")
        remaining = overview["remaining"]
        self._poll_archive_jobs()
        if remaining <= 0:
            return
        needed = self._archive_needs()
        if not needed:
            return
        operator, day = needed[0]
        token = f"ARC-{operator}-{day}"
        if token in self.state["archive_jobs"]:
            return
        try:
            answer = self.client.post_json("/archive/jobs",
                                           {"operator": operator, "day": day, "token": token})
        except SourceError as error:
            if error.status == 429:
                return
            if error.status == 404:  # not_offered：该运营商未提供此日档案
                self.day_state(operator, day)["not_offered"] = True
                return
            raise
        self.state["archive_jobs"][token] = {
            "id": token, "operator": operator, "day": day, "ready": answer["ready"],
            "fetched": [], "state": "forming",
        }
        self.state["archive_applied"][f"{operator}|{day}"] = token
        self.store.append_log("archive_applied", operator=operator, day=day, token=token)

    def _archive_needs(self) -> list[tuple[str, str]]:
        required_pairs = {(op, entry["day"]) for entry in self.state["close"]
                          for op in entry["required"]}
        needs: list[tuple[str, str]] = []
        for key, ds in self.state["days"].items():
            if ds["archive_complete"] or ds["not_offered"]:
                continue
            operator, day = ds["operator"], ds["day"]
            if operator == "legacy":
                # 实时库一旦下线，即使手里有完整实时快照，也要追归档权威清单。
                wanted = ds["expired"]
            else:
                # 新站：压实丢段，或封账后的撤回/补交使旧封账失效，都只能靠日包确认。
                # 从未封过账的日期继续等实时，不提前申请归档。
                seal_broken = bool(ds["seals"]) and self._seal_valid(ds) is None
                wanted = ds["compact_affected"] or seal_broken
            if wanted and f"{operator}|{day}" not in self.state["archive_applied"]:
                needs.append((operator, day))
        needs.sort(key=lambda pair: (pair not in required_pairs, pair[0], pair[1]))
        return needs

    def _poll_archive_jobs(self) -> None:
        for token, job in list(self.state["archive_jobs"].items()):
            try:
                info = self.client.get_json(f"/archive/job/{token}")
            except SourceError as error:
                if error.status == 404:
                    continue
                raise
            job["state"] = info["state"]
            job["ready"] = info["ready"]
            job["edition"] = info.get("edition")
            job["includes"] = info.get("includes", [])
            for part in info.get("parts", []):
                self._fetch_part(token, job, part)
            target_parts = [p for p in info.get("parts", []) if p["day"] == job["day"]]
            if info["state"] == "ready" and target_parts and all(
                    p["file"] in job["fetched"] for p in target_parts):
                self._confirm_archive(job, target_parts)

    def _fetch_part(self, token: str, job: dict, part: dict) -> None:
        filename = part["file"]
        if filename in job["fetched"]:
            return
        # 可只领需要的分片：只领本任务目标日，或该运营商-营业日确实另有档案申请的分片。
        # 包内相邻营业日不能由本清单确认，在它自己的任务到达前不提前领取。
        if part["day"] != job["day"] and \
                f"{job['operator']}|{part['day']}" not in self.state["archive_applied"]:
            return
        try:
            headers, raw = self.client.get_bytes(f"/archive/content/{filename}?job={token}")
        except SourceError as error:
            if error.status == 404:
                return  # forming 中尚不可领，下轮再试
            raise
        digest = hashlib.sha256(raw).hexdigest()
        if digest != part["sha256"]:
            self.store.append_log("archive_hash_mismatch", file=filename, expected=part["sha256"],
                                  actual=digest)
            return
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        if len(lines) != part["rows"]:
            self.store.append_log("archive_rows_mismatch", file=filename, expected=part["rows"],
                                  actual=len(lines))
            return
        self.store.save_content(filename, raw)
        job["fetched"].append(filename)
        # 缓存解析行，供相邻日自己的清单到达时直接复用，但不据本清单确认相邻日。
        self.state.setdefault("shard_rows", {})[filename] = [json.loads(line) for line in lines]
        self.store.append_log("archive_part_fetched", file=filename, job=token, sha256=digest)

    def _confirm_archive(self, job: dict, target_parts: list[dict]) -> None:
        ds = self.day_state(job["operator"], job["day"])
        rows: list[dict] = []
        for part in target_parts:
            rows.extend(self.state["shard_rows"][part["file"]])
        ds["archive_complete"] = True
        ds["archive_edition"] = job["edition"]
        ds["archive_rows"] = rows
        self.store.append_log("archive_confirmed", operator=job["operator"], day=job["day"],
                              edition=job["edition"], parts=[p["file"] for p in target_parts])

    # ------------------------------------------------------------------
    # 分表读数：按表号+装表世代+窗口领取，保留修订历史
    # ------------------------------------------------------------------
    def collect_readings(self) -> None:
        for entry in self.state["close"]:
            key = f"{entry['meter']}|{entry['epoch']}|{entry['day']}"
            bucket = self.state["readings"].setdefault(key, {"revisions": [], "pending": False})
            path = (f"/meter?meter={entry['meter']}&epoch={entry['epoch']}"
                    f"&window={entry['day']}")
            try:
                reading = self.client.get_json(path)
            except SourceError as error:
                if error.status == 202:
                    bucket["pending"] = True
                    continue
                raise
            bucket["pending"] = False
            if not any(r.get("revision") == reading.get("revision") for r in bucket["revisions"]):
                bucket["revisions"].append(reading)
                self.store.append_log("meter_reading", key=key, revision=reading.get("revision"))

    def _latest_reading(self, meter: str, epoch: str, window: str) -> dict | None:
        bucket = self.state["readings"].get(f"{meter}|{epoch}|{window}")
        if not bucket or not bucket["revisions"]:
            return None
        return bucket["revisions"][-1]

    # ------------------------------------------------------------------
    # 核销：三桶归集 + 完整性判定
    # ------------------------------------------------------------------
    def reconcile(self) -> dict[str, dict]:
        shift = self.state["shift"]
        rules = effective_switch(self.state["switch"], shift)
        bridges = active_bridges(self.state["bridges"], shift)
        reports: dict[str, dict] = {}
        for entry in self.state["close"]:
            meter, epoch, window = entry["meter"], entry["epoch"], entry["day"]
            pieces: list[Piece] = []
            unattached: list[Piece] = []
            sources: list[str] = []
            for operator in ("legacy", "new"):
                ds = self.day_state(operator, window)
                if not self._day_complete(ds):
                    continue
                seal = self._seal_valid(ds) if ds["operator"] == "new" else None
                provenance = provenance_of(ds, seal)
                sources.append(f"{operator}:{provenance}")
                linked_ids = {link[1] for bridge in bridges.values() for link in bridge["links"]
                              if link[0] == operator}
                for row in effective_rows(ds):
                    for piece in classify_fragment(operator, row, rules):
                        piece.source = provenance
                        if row["id"] in linked_ids:
                            piece.bridge = next(
                                bid for bid, b in bridges.items()
                                if any(link == [operator, row["id"]] for link in b["links"]))
                        if piece.meter is None:
                            unattached.append(piece)
                        else:
                            pieces.append(piece)
            reading = self._latest_reading(meter, epoch, window)
            window_pieces = [p for p in pieces if p.meter == meter and p.epoch == epoch]
            report = aggregate_window(meter, epoch, window, window_pieces, reading,
                                      [p for p in unattached])
            report_dict = report.as_dict()
            report_dict["cutoff"] = entry["cutoff"]
            report_dict["required"] = entry["required"]
            report_dict["sources"] = sources
            report_dict["missing_evidence"] = self._missing_evidence(entry, reading)
            report_dict["chase"] = self._chase_targets(entry, window_pieces, unattached)
            reports[f"{meter}|{epoch}|{window}"] = report_dict
        return reports

    def _missing_evidence(self, entry: dict, reading: dict | None) -> list[dict]:
        missing: list[dict] = []
        for operator in entry["required"]:
            ds = self.day_state(operator, entry["day"])
            if not self._day_complete(ds):
                reason = "实时日账已过期，待归档清单确认" if ds["expired"] or ds["compact_affected"] \
                    else "日账尚未封账/未到末页"
                missing.append({"operator": operator, "day": entry["day"], "reason": reason})
        if reading is None:
            missing.append({"operator": "construction", "day": entry["day"],
                            "reason": "分表读数未经施工确认（202）或尚未领取"})
        return missing

    def _chase_targets(self, entry: dict, pieces: list[Piece],
                       unattached: list[Piece]) -> list[dict]:
        """回答“剩下那部分电量还要向哪家追单”。"""
        targets: list[dict] = []
        for piece in [*pieces, *unattached]:
            if piece.status == "unconfirmed":
                targets.append({"operator": piece.operator, "fragment": piece.fragment_id,
                                "window": f"{piece.start}-{piece.end}", "kwh": q2(piece.kwh),
                                "reason": piece.reason})
        return targets

    # ------------------------------------------------------------------
    # 会计交接状态机
    # ------------------------------------------------------------------
    def advance_handoffs(self, reports: dict[str, dict]) -> None:
        shift = self.state["shift"]
        # 先刷新所有已知交接的会计侧状态（含已送出但回条断开的请求）。
        for request_id, record in self.state["handoffs"].items():
            self._refresh_handoff(request_id, record)

        for entry in self.state["close"]:
            key = f"{entry['meter']}|{entry['epoch']}|{entry['day']}"
            report = reports[key]
            request_id = f"HK-{entry['meter']}-{entry['day']}-r1"
            existing = self.state["handoffs"].get(request_id)
            self._detect_adjustment(entry, existing)
            if existing:
                continue
            if not self._ready_to_send(entry, report, shift):
                continue
            body = self._initial_body(request_id, entry, report)
            self._send_handoff(request_id, body, report)

    def _refresh_handoff(self, request_id: str, record: dict) -> None:
        try:
            answer = self.client.get_json(f"/account/result/{request_id}")
        except SourceError as error:
            if error.status == 404:
                if request_id in self.state["in_flight"]:
                    # 会计台目前看不到：用完全相同的请求号与内容重发，避免双份交接。
                    self._send_handoff(request_id, record["body"], None, replaying=True)
                return
            raise
        self.state["in_flight"].pop(request_id, None)
        record["state"] = answer["state"]
        record["receipt"] = answer.get("receipt")
        record["check"] = answer.get("check", [])
        # 回条确认意味着会计实际收件：登记为一次成功送达，用于后续防重复核对。
        if answer["state"] in ("received", "posted", "returned") and not record.get("attempts"):
            record["attempts"] = 1

    def _ready_to_send(self, entry: dict, report: dict, shift: int) -> bool:
        if shift < entry["cutoff"]:
            return False
        if report["missing_evidence"]:
            return False
        if report["reading_revision"] is None:
            return False
        return True

    def _initial_body(self, request_id: str, entry: dict, report: dict) -> dict:
        reading = self._latest_reading(entry["meter"], entry["epoch"], entry["day"])
        documents = [f"meter:{entry['meter']}:{entry['epoch']}:rev{reading['revision']}"]
        for operator in entry["required"]:
            ds = self.day_state(operator, entry["day"])
            edition = ds["archive_edition"] if ds["archive_complete"] else ds["live_edition"]
            kind = "archive" if ds["archive_complete"] else "live"
            documents.append(f"ledger:{operator}:{entry['day']}:{edition}:{kind}")
        return {
            "request": request_id,
            "meter": entry["meter"],
            "window": entry["day"],
            "type": "initial",
            "energy": report["window_total"],
            "documents": documents,
        }

    def _send_handoff(self, request_id: str, body: dict, report: dict | None,
                      replaying: bool = False) -> None:
        try:
            answer = self.client.post_json("/account/handoff", body)
        except SourceError as error:
            if error.status == 503:
                # 会计台可能已收件：登记为待查回条，下轮先 GET /account/result。
                self.state["in_flight"][request_id] = {"since_shift": self.state["shift"]}
                self.state["handoffs"].setdefault(request_id, {
                    "request": request_id, "body": body, "state": "unknown",
                    "report_snapshot": report, "attempts": 0,
                })
                self.store.append_log("handoff_reply_lost", request=request_id)
                return
            raise
        record = self.state["handoffs"].setdefault(request_id, {
            "request": request_id, "body": body, "attempts": 0,
        })
        record["body"] = body
        record["state"] = answer["state"]
        record["receipt"] = answer.get("receipt")
        record["check"] = answer.get("check", [])
        record["report_snapshot"] = report
        record["attempts"] = record.get("attempts", 0) + 1
        self.state["in_flight"].pop(request_id, None)
        self.store.append_log("handoff_sent" if not replaying else "handoff_replayed",
                              request=request_id, state=answer["state"],
                              check=record["check"])

    def _detect_adjustment(self, entry: dict, existing: dict | None) -> None:
        """已 post 的交接遇读数更正：冻结原金额，只形成待人工确认的可追溯差额。"""
        if not existing or existing["state"] != "posted":
            return
        bucket = self.state["readings"].get(
            f"{entry['meter']}|{entry['epoch']}|{entry['day']}")
        if not bucket or len(bucket["revisions"]) < 2:
            return
        old_rev, new_rev = bucket["revisions"][-2], bucket["revisions"][-1]
        if "bottoms" not in old_rev or "bottoms" not in new_rev:
            return
        old_energy = q2(Decimal(old_rev["bottoms"][-1][1]) - Decimal(old_rev["bottoms"][0][1]))
        new_energy = q2(Decimal(new_rev["bottoms"][-1][1]) - Decimal(new_rev["bottoms"][0][1]))
        if old_energy == new_energy:
            return
        if any(p["meter"] == entry["meter"] and p["window"] == entry["day"]
               for p in self.state["pending_adjustments"]):
            return
        approval = entry.get("adjustment_approval", {})
        self.state["pending_adjustments"].append({
            "meter": entry["meter"], "epoch": entry["epoch"], "window": entry["day"],
            "previous": existing["request"], "old_revision": old_rev["revision"],
            "new_revision": new_rev["revision"], "old_energy": old_energy,
            "new_energy": new_energy,
            "delta": q2(Decimal(new_energy) - Decimal(old_energy)),
            "approval_code": approval.get("code"),
            "approval_known": approval.get("known"),
            "state": "awaiting_human_confirmation",
        })
        self.store.append_log("adjustment_proposed", meter=entry["meter"],
                              window=entry["day"], delta=q2(Decimal(new_energy) - Decimal(old_energy)))

    def confirm_adjustment(self, meter: str, window: str) -> dict:
        """值班/会计人工确认后，凭审批号发送调账（不自动发送）。"""
        pending = next((p for p in self.state["pending_adjustments"]
                        if p["meter"] == meter and p["window"] == window
                        and p["state"] == "awaiting_human_confirmation"), None)
        if pending is None:
            raise KeyError("没有待确认的调账")
        shift = self.state["shift"]
        if pending["approval_known"] is None or shift < pending["approval_known"]:
            raise RuntimeError("差额审批尚未签发，不能送调账")
        request_id = f"ADJ-{meter}-{window}-r{pending['new_revision']}"
        if request_id in self.state["handoffs"]:
            return self.state["handoffs"][request_id]
        body = {
            "request": request_id,
            "meter": meter,
            "window": window,
            "type": "adjustment",
            "energy": pending["new_energy"],
            "documents": [
                f"meter:{meter}:{pending['epoch']}:rev{pending['new_revision']}",
                f"adjustment:delta:{pending['delta']}",
                f"previous:{pending['previous']}",
            ],
            "previous": pending["previous"],
            "approval": pending["approval_code"],
        }
        self._send_handoff(request_id, body, None)
        if request_id in self.state["handoffs"]:
            pending["state"] = self.state["handoffs"][request_id]["state"]
            pending["request"] = request_id
        self.store.save()
        return self.state["handoffs"][request_id]

    # ------------------------------------------------------------------
    def status(self) -> dict:
        reports = self.reconcile()
        day_view = {}
        for key, ds in self.state["days"].items():
            seal = self._seal_valid(ds) if ds["operator"] == "new" else None
            day_view[key] = {
                "complete": self._day_complete(ds),
                "provenance": provenance_of(ds, seal),
                "live_complete": ds["live_complete"],
                "live_edition": ds["live_edition"],
                "seal": seal,
                "compact_affected": ds["compact_affected"],
                "expired": ds["expired"],
                "archive_complete": ds["archive_complete"],
                "archive_edition": ds["archive_edition"],
                "effective_fragments": [r["id"] for r in effective_rows(ds)],
                "retracted": ds["retracted"],
                "not_offered": ds["not_offered"],
            }
        return {
            "shift": self.state["shift"],
            "feed_after": self.state["feed_after"],
            "tracked_days": self.state["tracked_days"],
            "days": day_view,
            "archive_jobs": self.state["archive_jobs"],
            "archive_quota_applied": self.state["archive_applied"],
            "windows": reports,
            "handoffs": self.state["handoffs"],
            "in_flight": self.state["in_flight"],
            "pending_adjustments": self.state["pending_adjustments"],
            "bridges": self._bridge_view(),
        }

    def _bridge_view(self) -> list[dict]:
        from .ledger import bridge_groups
        return bridge_groups(self.state["bridges"], self.state["shift"])
