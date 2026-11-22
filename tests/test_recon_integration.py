"""收件核销服务端到端测试：完整轮次时序、重启续做与会计回条处理。"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from source_service import create_source

from recon.engine import ReconciliationEngine


class SagaTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        self.thread = Thread(target=self.server.serve_forever,
                             kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.mkdtemp(prefix="recon-it-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read())

    def advance(self, shift):
        self.post("/time", {"shift": shift})

    def engine(self):
        return ReconciliationEngine(self.base, self.tmp)

    def window(self, status, meter):
        return status["windows"][f"{meter}|g18|2026-11-20"] if meter == "OLD" else (
            status["windows"][f"{meter}|g19|2026-11-20"] if meter == "NEW" else
            status["windows"][f"{meter}|g1|2026-11-20"])

    def test_full_saga_with_restart_lost_replies_and_adjustment(self):
        # ---- shift 0：实时/流水首次收件，均未完整 ----
        self.advance(0)
        status = self.engine().run_once()
        self.assertEqual(status["days"]["legacy|2026-11-20"]["complete"], False)
        self.assertEqual(status["days"]["new|2026-11-20"]["complete"], False)
        self.assertEqual(status["archive_jobs"], {})

        # ---- shift 1：旧站修订升级，游标 409 后重读新版；新站封账 N20-1 ----
        self.advance(1)
        self.engine().run_once()

        # ---- shift 2：旧站同修订读到末页；NEW 读数可领；此时不申请任何归档 ----
        self.advance(2)
        status = self.engine().run_once()
        self.assertTrue(status["days"]["legacy|2026-11-20"]["live_complete"])
        self.assertEqual(status["days"]["legacy|2026-11-20"]["live_edition"], "L20-2")
        self.assertEqual(status["archive_jobs"], {})

        # ---- shift 3：旧站 410、桥接撤回、新站撤回 n41 使封账失效；EAST 截账交件 ----
        self.advance(3)
        status = self.engine().run_once()
        # 每轮只有一个归档名额：本轮先申请 close 必需且已过期的旧运营商
        self.assertEqual(set(status["archive_jobs"]), {"ARC-legacy-2026-11-20"})
        east = self.window(status, "EAST")
        self.assertEqual(east["missing_evidence"], [])   # 独立表不被另两家阻塞
        handoff = status["handoffs"]["HK-EAST-2026-11-20-r1"]
        self.assertEqual(handoff["state"], "unknown")    # 503：回条断开
        self.assertEqual(handoff["body"]["energy"], "2.30")  # 按 rev1 原始读数
        self.assertIn("HK-EAST-2026-11-20-r1", status["in_flight"])
        # 桥接撤回后不再把 m17/n41 视为同一笔
        self.assertEqual(status["bridges"][0]["state"], "withdrawn")

        # 模拟进程重启：新引擎从落盘状态继续，归档任务与 in-flight 不丢、不重发
        restarted = self.engine()
        self.assertEqual(restarted.state["in_flight"],
                         {"HK-EAST-2026-11-20-r1": {"since_shift": 3}})
        self.advance(4)
        status = restarted.run_once()
        # 本轮名额用于新运营商（撤回使封账失效），任务集合补齐为两家
        self.assertEqual(set(status["archive_jobs"]),
                         {"ARC-legacy-2026-11-20", "ARC-new-2026-11-20"})
        # 回条恢复：GET /account/result 直接查到会计已收件，无需重发且不产生重复交接
        self.assertEqual(status["in_flight"], {})
        self.assertEqual(status["handoffs"]["HK-EAST-2026-11-20-r1"]["state"], "received")
        self.assertEqual(status["handoffs"]["HK-EAST-2026-11-20-r1"]["attempts"], 1)
        # M5 不确定区间生效：m17 两侧 split 都不能归枪
        old = self.window(status, "OLD")
        new = self.window(status, "NEW")
        self.assertEqual(old["attributed"], "0.00")
        self.assertEqual(new["attributed"], "0.00")
        self.assertEqual(old["chase"][0]["fragment"], "m17")

        # ---- shift 5：两个归档包齐套确认，OLD/NEW 初始交接；EAST 已 posted ----
        self.advance(5)
        status = self.engine().run_once()
        legacy_day = status["days"]["legacy|2026-11-20"]
        new_day = status["days"]["new|2026-11-20"]
        self.assertEqual(legacy_day["provenance"], "archive:LA20-2")
        self.assertEqual(new_day["provenance"], "archive:NA20-2")
        self.assertEqual(new_day["effective_fragments"], ["n42"])  # n41 已撤回
        self.assertEqual(legacy_day["effective_fragments"], ["m17", "z8"])
        # 分片经过 sha256/行数核对
        legacy_job = status["archive_jobs"]["ARC-legacy-2026-11-20"]
        self.assertEqual(set(legacy_job["fetched"]),
                         {"legacy-20a.ndjson", "legacy-20b.ndjson"})  # 不领相邻日 19 分片
        content_dir = Path(self.tmp) / "content"
        self.assertTrue((content_dir / "legacy-20b.ndjson").exists())

        old = self.window(status, "OLD")
        new = self.window(status, "NEW")
        # OLD：总量 3.50，m17 的 1.50 因接线不确定无法归枪，余量 2.00 留共表
        self.assertEqual((old["window_total"], old["meter_only"], old["unconfirmed"]),
                         ("3.50", "2.00", "1.50"))
        # NEW：n42 的 1.20 有 M3 确定性覆盖可归枪；m17 的 3.30 无法确认；
        # 共表余量 = 5.30 - 1.20 - 3.30 = 0.80，绝不拿共表余额猜枪用量
        self.assertEqual((new["window_total"], new["attributed"], new["meter_only"],
                          new["unconfirmed"]),
                         ("5.30", "1.20", "0.80", "3.30"))
        self.assertEqual(old["missing_evidence"], [])
        self.assertEqual(new["missing_evidence"], [])
        self.assertEqual(status["handoffs"]["HK-OLD-2026-11-20-r1"]["state"], "returned")
        self.assertEqual(set(status["handoffs"]["HK-OLD-2026-11-20-r1"]["check"]),
                         {"11:20", "11:30"})
        self.assertEqual(status["handoffs"]["HK-NEW-2026-11-20-r1"]["state"], "received")
        self.assertEqual(status["handoffs"]["HK-EAST-2026-11-20-r1"]["state"], "posted")
        # 回条恢复后只确认了一次收件，没有把已确认用量送两次
        self.assertEqual(status["handoffs"]["HK-EAST-2026-11-20-r1"]["attempts"], 1)
        # 已冻结的初始交接仍按原 2.30 查询，不被 rev2 改写
        self.assertEqual(
            status["handoffs"]["HK-EAST-2026-11-20-r1"]["body"]["energy"], "2.30")
        # 审批（known=6）未到前，人工确认调账必须被拒绝
        with self.assertRaises(RuntimeError):
            self.engine().confirm_adjustment("EAST", "2026-11-20")

        # 相邻营业日 21：实时封账 N21-1 仍有效（无撤回/压实），由实时来源独立确认；
        # 20 日归档包虽含 new-21.ndjson，也不提前领取相邻日分片
        self.advance(6)
        status = self.engine().run_once()
        self.assertEqual(status["days"]["new|2026-11-21"]["provenance"], "live:N21-1")
        job20 = status["archive_jobs"]["ARC-new-2026-11-20"]
        self.assertEqual(job20["fetched"], ["new-20.ndjson"])
        self.assertNotIn("new-21.ndjson", job20["fetched"])

        # 读数更正只形成待人工确认的可追溯差额，未自动送调账
        pending = status["pending_adjustments"][0]
        self.assertEqual((pending["old_energy"], pending["new_energy"], pending["delta"]),
                         ("2.30", "2.50", "0.20"))
        self.assertEqual(pending["state"], "awaiting_human_confirmation")

        # ---- 人工确认调账：凭审批号发送并最终 posted，原交接仍可回看 ----
        self.advance(8)
        answer = self.engine().confirm_adjustment("EAST", "2026-11-20")
        # EAST 表回条断开：首送 503 记为待查回条，不能假定会计收到而重发
        self.assertEqual(answer["state"], "unknown")
        self.assertIn("ADJ-EAST-2026-11-20-r2", self.engine().state["in_flight"])
        self.advance(9)   # 下一轮先查回条：会计已收件
        status = self.engine().run_once()
        self.assertEqual(status["handoffs"]["ADJ-EAST-2026-11-20-r2"]["state"], "received")
        self.advance(10)  # received +2 轮后会计自动 posted
        status = self.engine().run_once()
        adjustment = status["handoffs"]["ADJ-EAST-2026-11-20-r2"]
        self.assertEqual(adjustment["state"], "posted")
        self.assertEqual(adjustment["body"]["approval"], "ADJ-EAST-20")
        self.assertEqual(adjustment["body"]["previous"], "HK-EAST-2026-11-20-r1")
        # 再次运行不会重复发送任何交接
        before = {k: v["attempts"] for k, v in status["handoffs"].items()}
        again = self.engine().run_once()
        self.assertEqual({k: v["attempts"] for k, v in again["handoffs"].items()}, before)

    def test_no_cross_edition_stitching_and_adjacent_day_needs_own_manifest(self):
        """相邻日不能靠别的站日清单确认；19 日必须有自己的归档任务。"""
        for shift in range(6):
            self.advance(shift)
            self.engine().run_once()
        status = self.engine().status()
        # 20 日包里虽含 legacy-19 分片，但 19 日没有自己的清单，仍不确认
        self.assertNotIn("legacy|2026-11-19",
                         {k for k, v in status["days"].items() if v["complete"]})
        engine = self.engine()
        engine.track_day("2026-11-19")
        self.advance(7)
        engine.run_once()   # 用 7 轮的名额申请 19 日
        self.advance(8)
        status = engine.run_once()  # 8 轮包就绪（ready=申请轮次+1）
        self.assertEqual(status["days"]["legacy|2026-11-19"]["provenance"], "archive:LA19-1")
        # 同参数任务幂等，重启后也只保留一个 19 日任务
        self.assertEqual(
            [t for t in status["archive_jobs"] if t == "ARC-legacy-2026-11-19"],
            ["ARC-legacy-2026-11-19"])


if __name__ == "__main__":
    unittest.main()
