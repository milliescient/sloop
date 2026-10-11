#!/usr/bin/env python3
"""Static server for site/, with noindex on every response.

The meta tag in the page covers HTML; this header covers everything else and does not
depend on a crawler parsing the document. Neither is access control: anyone holding the
link can read the page.

With --edit it also serves edit/<page>, which reads and writes that page's page.md or
page.html. A save is built into a scratch directory first and rolled back if the build
fails, so a bad edit never reaches the served page. Requests arriving through Funnel are
refused, since Funnel is the open internet.

It also watches the pages and rebuilds, so writing a page is the whole workflow and the
served copy follows within a second.

Started by `notebook serve`, which fills in the port, the site directory and whether
editing is on from the notebook's config.
"""
import functools
import hashlib
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SAVING = threading.Lock()

ROOT = PAGES = None


def init(root, conf):
    global ROOT, PAGES
    ROOT, PAGES = root, conf["pages"]


def page_file(slug):
    # only a directory that exists under pages/, never a path built from the request
    names = {d.name: d for d in PAGES.iterdir() if d.is_dir()}
    if slug not in names:
        return None
    for name in ("page.md", "page.html"):
        if (names[slug] / name).exists():
            return names[slug] / name
    return None


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def build(out, flags):
    """Out of process on purpose: a build that dies takes nothing in the server with it,
    and the served copy is only replaced once a build has succeeded."""
    r = subprocess.run([sys.executable, "-m", "sloop", "build", "--dir", str(ROOT),
                        *flags, "--out", str(out)],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode == 0, (r.stderr + r.stdout).strip()


class Handler(http.server.SimpleHTTPRequestHandler):
    edit = False
    flags = []

    def end_headers(self):
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def edit_slug(self):
        """The page an edit request names, or None if this is not an edit request."""
        path = self.path.split("?")[0]
        if not path.startswith("/edit/"):
            return None
        return path[len("/edit/"):].strip("/")

    def allowed(self):
        if not self.edit or self.headers.get("Tailscale-Funnel-Request"):
            self.reply(404, {"error": "editing is off"})
            return False
        return True

    def do_GET(self):
        slug = self.edit_slug()
        if slug is None:
            return super().do_GET()
        if not self.allowed():
            return
        if slug == "":
            return self.reply(200, {"edit": True})
        f = page_file(slug)
        if f is None:
            return self.reply(404, {"error": f"no page {slug}"})
        text = f.read_text()
        self.reply(200, {"file": f"{slug}/{f.name}", "text": text, "hash": digest(text)})

    def do_POST(self):
        slug = self.edit_slug()
        if slug is None:
            return self.reply(404, {"error": "not found"})
        if not self.allowed():
            return
        f = page_file(slug)
        if f is None:
            return self.reply(404, {"error": f"no page {slug}"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            text, base = req["text"], req["hash"]
        except (ValueError, KeyError):
            return self.reply(400, {"error": "expected {text, hash}"})
        with SAVING:
            old = f.read_text()
            if digest(old) != base:
                return self.reply(409, {"error": "the file changed on disk since you opened "
                                        "it. Copy your text out, close and reopen."})
            f.write_text(text)
            with tempfile.TemporaryDirectory() as tmp:
                ok, log = build(tmp, self.flags)
                if not ok:
                    f.write_text(old)
                    return self.reply(422, {"error": log[-2000:] or "the build failed"})
                shutil.copy(Path(tmp) / "index.html", Path(self.directory) / "index.html.new")
                (Path(self.directory) / "index.html.new").replace(
                    Path(self.directory) / "index.html")
        self.reply(200, {"hash": digest(text)})


def fingerprint(root, conf):
    """What a rebuild depends on, cheaply.

    .git/logs/HEAD is in here because a page's Updated stamp comes from git rather than from
    mtimes, and committing touches no watched file. Without it every stamp sits one build
    behind, from the edit until someone rebuilds by hand.
    """
    h = hashlib.sha256()
    watched = sorted(p for p in conf["pages"].rglob("*") if p.is_file())
    for f in watched + [conf["bibliography"], root / "notebook.yml", root / ".git/logs/HEAD"]:
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"-")
        h.update(str(f).encode())
    return h.hexdigest()


def watch(root, conf, site, flags, interval=1.0):
    """Poll, and rebuild when anything moves.

    A whole build is a few hundredths of a second, so hashing the inputs and rebuilding
    everything costs less than tracking which page changed, and needs no inotify. The build
    runs into a scratch directory, so a page that stops building leaves the served copy
    standing and prints why.
    """
    prev = fingerprint(root, conf)
    while True:
        time.sleep(interval)
        now = fingerprint(root, conf)
        if now == prev:
            continue
        prev = now
        print(f"[{time.strftime('%H:%M:%S')}] rebuilding", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            ok, log = build(tmp, flags)
            if not ok:
                print(log[-2000:] or "the build failed", flush=True)
                continue
            shutil.copy(Path(tmp) / "index.html", site / "index.html.new")
            (site / "index.html.new").replace(site / "index.html")
            print("  " + (log.strip().splitlines() or [""])[-1], flush=True)


def serve(root, conf, port=None, site=None, edit=None, flags=(), watching=True):
    init(root, conf)
    port = conf["serve"]["port"] if port is None else port
    site = Path(site or conf["out"])
    Handler.edit = conf["serve"]["edit"] if edit is None else edit
    Handler.flags = list(flags)
    if watching:
        threading.Thread(target=watch, args=(root, conf, site, list(flags)),
                         daemon=True).start()
    handler = functools.partial(Handler, directory=str(site))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"  http://127.0.0.1:{port}/   serving {site}"
          f"{'   watching' if watching else ''}"
          f"{'   editing on' if Handler.edit else ''}", flush=True)
    srv.serve_forever()
