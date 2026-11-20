import json
from hashlib import sha256
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from source_service import create_source


class StationTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def exchange(self, path, payload=None, headers=None):
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.headers["Content-Type"].startswith("application/json"):
                body = json.loads(body)
            return response.code, response.headers, body

    def advance(self, shift):
        self.assertEqual(self.exchange("/time", {"shift": shift})[0], 200)

    def test_live_cursor_fails_after_revision_and_archive_shares_adjacent_day(self):
        day = "2026-11-20"
        first = self.exchange(f"/legacy/day?day={day}")[2]
        self.advance(1)
        cursor = f"/legacy/day?day={day}&edition={first['edition']}&cursor={first['next']}"
        self.assertEqual(self.exchange(cursor)[0], 409)
        self.advance(3)
        self.assertEqual(self.exchange(f"/legacy/day?day={day}")[0], 410)
        request = {"operator": "legacy", "day": day, "token": "J-A"}
        self.assertEqual(self.exchange("/archive/jobs", request)[0], 202)
        self.assertEqual(self.exchange("/archive/jobs", request)[0], 200)
        self.assertEqual(self.exchange("/archive/jobs", {**request, "token": "J-B"})[0], 429)
        self.advance(4)
        result = self.exchange("/archive/job/J-A")[2]
        self.assertEqual(result["state"], "forming")
        self.assertIn("2026-11-19", result["includes"])
        self.assertEqual(self.exchange("/archive/content/legacy-20b.ndjson?job=J-A")[0], 404)
        self.advance(5)
        part = self.exchange("/archive/job/J-A")[2]["parts"][-1]
        status, headers, raw = self.exchange("/archive/content/" + part["file"] + "?job=J-A")
        self.assertEqual(status, 200)
        self.assertEqual(part["sha256"], sha256(raw).hexdigest())
        self.assertEqual(headers["X-Content-SHA256"], part["sha256"])

    def test_compacted_feed_does_not_claim_a_complete_day(self):
        first = self.exchange("/new/feed?after=0")[2]
        self.assertEqual([event["id"] for event in first["events"]], [1, 2])
        self.advance(4)
        self.assertEqual(self.exchange("/new/feed?after=0")[2]["affected_days"], ["2026-11-20"])
        resumed = self.exchange("/new/feed?after=2")[2]
        self.assertEqual([event["action"] for event in resumed["events"]], ["seal", "record", "retract"])
        self.assertTrue(resumed["more"])

    def test_certification_and_handoff_advance_independently(self):
        url = "/meter?meter=NEW&epoch=g19&window=2026-11-20"
        self.assertEqual(self.exchange(url)[0], 202)
        self.advance(2)
        self.assertEqual(self.exchange(url)[2]["bottoms"][0], ["11:30", "0.10"])
        record = {"request":"PK1", "meter":"EAST", "window":"2026-11-20", "type":"initial",
                  "energy":"2.30", "documents":["M4","g1-r1"]}
        self.assertEqual(self.exchange("/account/handoff", record)[0], 503)
        self.assertEqual(self.exchange("/account/result/PK1")[0],404)
        self.advance(3)
        self.assertEqual(self.exchange("/account/result/PK1")[2]["state"], "received")
        self.advance(4)
        self.assertEqual(self.exchange("/account/result/PK1")[2]["state"], "posted")
        now = self.exchange("/meter?meter=EAST&epoch=g1&window=2026-11-20")[2]
        self.assertEqual(now["revision"], "2")
        self.assertEqual(self.exchange("/account/result/PK1")[2]["input"]["energy"], "2.30")
