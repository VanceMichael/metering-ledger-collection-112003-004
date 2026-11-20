"""站房联调中的运营商、施工底数和会计端。"""
from argparse import ArgumentParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


def create_source(port=0, root=ROOT):
    def load(name):
        return json.loads((root / name).read_text())

    grid = load("station.json")
    jobs = {}
    accounts = {}
    archive_quota = {}
    shift = 0
    lock = Lock()

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, data, kind="application/json", headers=()):
            payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path)
            query = {key: value[0] for key, value in parse_qs(path.query).items()}
            try:
                with lock:
                    self.read(path.path, query)
            except (KeyError, ValueError, IndexError):
                self.respond(400, {"state": "invalid_request"})

        def read(self, path, q):
            if path == "/":
                return self.respond(200, {"shift": shift, "live": "/legacy/day", "feed": "/new/feed", "meter": "/meter"})
            if path in ("/data/bridges.json", "/data/switch.json", "/data/close.json"):
                return self.respond(200, load(path.lstrip("/")))
            if path == "/legacy/day":
                editions = [revision for revision in grid["live"].get(q["day"], []) if revision["known"] <= shift]
                if not editions or editions[-1]["until"] <= shift:
                    return self.respond(410, {"state": "archive_only", "day": q["day"]})
                current = editions[-1]
                if q.get("edition", current["edition"]) != current["edition"]:
                    return self.respond(409, {"state": "cursor_stale", "edition": current["edition"]})
                i = int(q.get("cursor", 0))
                if not 0 <= i < len(current["pages"]):
                    return self.respond(400, {"state": "invalid_cursor"})
                return self.respond(200, {"edition": current["edition"], "day": q["day"],
                                          "rows": current["pages"][i],
                                          "next": i + 1 if i + 1 < len(current["pages"]) else None})
            if path == "/new/feed":
                after = int(q.get("after", 0))
                if after < grid["feed_floor"].get(str(shift), 0):
                    return self.respond(410, {"state": "log_compacted", "floor": grid["feed_floor"][str(shift)],
                                              "affected_days": grid["feed_compacted_days"]})
                changes = [event for event in grid["feed"] if event["known"] <= shift and event["id"] > after]
                return self.respond(200, {"events": changes[:3], "more": len(changes) > 3})
            if path == "/archive/jobs":
                return self.respond(200, {"jobs": [{"id": name, **data} for name, data in jobs.items()],
                                          "remaining": grid["archive_per_shift"] - archive_quota.get(shift, 0)})
            if path.startswith("/archive/job/"):
                name = path.rsplit("/", 1)[-1]
                if name not in jobs:
                    return self.respond(404, {"state": "unknown"})
                job = jobs[name]
                if job["ready"] > shift:
                    return self.respond(202, {"state": "forming", **job}, headers=[("Retry-After", "1")])
                archive = grid["archives"][job["operator"]][job["day"]]
                available = [part for part in archive["parts"] if part["published"] <= shift]
                return self.respond(200, {"state": "ready" if len(available) == len(archive["parts"]) else "forming",
                                          **job, "edition": archive["edition"], "parts": available,
                                          "includes": archive["includes"]})
            if path.startswith("/archive/content/"):
                name = path.rsplit("/", 1)[-1]
                if q.get("job") not in jobs:
                    return self.respond(403, {"state": "job_required"})
                job = jobs[q["job"]]
                archive = grid["archives"][job["operator"]][job["day"]]
                part = next((p for p in archive["parts"] if p["file"] == name and p["published"] <= shift), None)
                if not part or job["ready"] > shift:
                    return self.respond(404, {"state": "not_ready"})
                actual = (root / "archive" / name).read_bytes()
                return self.respond(200, actual, "application/x-ndjson", [("X-Content-SHA256", sha256(actual).hexdigest())])
            if path == "/meter":
                records = [row for row in grid["reading"] if all(row[key] == q[key] for key in ("meter", "epoch", "window"))
                           and row["known"] <= shift]
                if not records:
                    return self.respond(202, {"state": "not_certified"})
                return self.respond(200, load("originals/meter/" + records[-1]["file"]))
            if path.startswith("/account/result/"):
                record = accounts.get(path.rsplit("/", 1)[-1])
                if not record or shift < record["visible"]:
                    return self.respond(404, {"state": "unknown"})
                value = {key: item for key, item in record.items() if key != "visible"}
                return self.respond(200, value)
            return self.respond(404, {"state": "unknown"})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    self.write(urlsplit(self.path).path, body)
            except (KeyError, ValueError, TypeError):
                self.respond(400, {"state": "invalid_request"})

        def write(self, path, body):
            nonlocal shift
            if path == "/time":
                target = body["shift"]
                if type(target) is not int or target < shift:
                    return self.respond(409, {"state": "cannot_rewind"})
                shift = target
                for record in accounts.values():
                    if record["state"] == "received" and record["received"] + 2 <= shift:
                        record["state"] = "posted"
                return self.respond(200, {"shift": shift})
            if path == "/archive/jobs":
                operator, day, token = (body[k] for k in ("operator", "day", "token"))
                if token in jobs:
                    saved = jobs[token]
                    same = saved["operator"] == operator and saved["day"] == day
                    return self.respond(200 if same else 409, {"id": token, **saved})
                if archive_quota.get(shift, 0) >= grid["archive_per_shift"]:
                    return self.respond(429, {"state": "daily_quota_reached"})
                if day not in grid["archives"].get(operator, {}):
                    return self.respond(404, {"state": "not_offered"})
                archive_quota[shift] = archive_quota.get(shift, 0) + 1
                jobs[token] = {"operator": operator, "day": day, "ready": shift + 1}
                return self.respond(202, {"id": token, **jobs[token]})
            if path == "/account/handoff":
                request = body["request"]
                if request in accounts:
                    result = accounts[request]
                    if result["visible"] > shift:
                        return self.respond(503, {"state": "unknown"})
                    return self.respond(200 if result["input"] == body else 409, result)
                metering = body["meter"]
                if body["type"] not in ("initial", "adjustment"):
                    return self.respond(422, {"state": "invalid_type"})
                if body["type"] == "adjustment":
                    prev = accounts.get(body.get("previous"))
                    if not prev or prev["state"] != "posted" or body.get("approval") != grid["adjustment_code"]:
                        return self.respond(409, {"state": "missing_posted_handoff_or_approval"})
                if body["type"] == "initial" and any(old["input"]["window"] == body["window"] and
                        old["input"]["meter"] == metering and old["state"] in ("received", "posted") and
                        old["input"]["type"] == "initial" for old in accounts.values()):
                    return self.respond(409, {"state": "window_already_received"})
                rejected = metering in grid["refused"]
                record = {"state": "returned" if rejected else "received", "received": shift,
                          "check": grid["refused"].get(metering, []),
                          "input": body, "visible": shift + int(metering in grid["lose_handoff_reply"]),
                          "receipt": f"PAY-{len(accounts) + 1}"}
                accounts[request] = record
                if record["visible"] > shift:
                    return self.respond(503, {"state": "unknown"})
                return self.respond(200, record)
            return self.respond(404, {"state": "unknown"})

    return ThreadingHTTPServer(("127.0.0.1", port), Source)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    with create_source(parser.parse_args().port) as server:
        print(f"分表联调服务在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
