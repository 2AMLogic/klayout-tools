#!/usr/bin/env python3
"""A stateful, recording stand-in for the Gitea REST surface the Builder lease
touches (#14941). Run as `python3 -I gitea-lease-server.py STATE_DIR`; it
binds a loopback port, writes the port to STATE_DIR/port, and serves until
killed. Everything is plain files in STATE_DIR so the test can seed and
inspect independently of the code under test:

  comments.json   the issue's comments (list; ids/updated_at assigned here)
  state           the issue state word (default "open")
  user            the login `GET /user` reports and POSTs are attributed to
  post-fail       when present, POST comments answers 403
  patch-fail      when present, PATCH comment answers 500
  requests.log    one line per request: "METHOD PATH"
"""
import calendar
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

D = sys.argv[1]
NWO = "loomtest/loom-consumer"
FMT = "%Y-%m-%dT%H:%M:%SZ"


def p(name):
    return os.path.join(D, name)


def read(name, default):
    try:
        with open(p(name)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def comments():
    try:
        with open(p("comments.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save(items):
    with open(p("comments.json"), "w") as f:
        json.dump(items, f)


def now_iso():
    return time.strftime(FMT, time.gmtime())


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def handle_any(self, method):
        path = self.path.split("?")[0]
        with open(p("requests.log"), "a") as f:
            f.write(f"{method} {path}\n")
        base = f"/api/v1/repos/{NWO}"
        if method == "GET" and path == f"{base}":
            return self.send(200, {"full_name": NWO, "permissions": {"admin": False, "push": True, "pull": True}})
        if method == "GET" and path == "/api/v1/user":
            return self.send(200, {"login": read("user", "loomtest")})
        if method == "GET" and path.startswith(f"{base}/issues/") and path.endswith("/comments"):
            return self.send(200, comments())
        if method == "GET" and path.startswith(f"{base}/issues/") and path.count("/") == 6:
            return self.send(200, {"state": read("state", "open")})
        if method == "POST" and path.startswith(f"{base}/issues/") and path.endswith("/comments"):
            if os.path.exists(p("post-fail")):
                return self.send(403, {"message": "permission denied"})
            items = comments()
            cid = max([c["id"] for c in items] + [100]) + 1
            c = {"id": cid, "body": self.body()["body"], "user": {"login": read("user", "loomtest")},
                 "created_at": now_iso(), "updated_at": now_iso()}
            items.append(c)
            save(items)
            return self.send(201, {**c, "html_url": f"http://gitea.test/{NWO}/issues/1#issuecomment-{cid}"})
        if method == "PATCH" and path.startswith(f"{base}/issues/comments/"):
            if os.path.exists(p("patch-fail")):
                return self.send(500, {"message": "boom"})
            cid = int(path.rsplit("/", 1)[1])
            items = comments()
            for c in items:
                if c["id"] == cid:
                    c["body"] = self.body()["body"]
                    # the forge assigns updated_at; advance it strictly so a
                    # same-second renewal is still observable
                    prev = calendar.timegm(time.strptime(c.get("updated_at", "1970-01-01T00:00:00Z"), FMT))
                    c["updated_at"] = time.strftime(FMT, time.gmtime(max(int(time.time()), prev + 1)))
                    save(items)
                    return self.send(200, c)
            return self.send(404, {"message": "not found"})
        return self.send(404, {"message": f"unhandled {method} {path}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")


srv = HTTPServer(("127.0.0.1", 0), H)
with open(p("port"), "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
