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
import queue
import re
import urllib.error
import urllib.request
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SAVING = threading.Lock()

ROOT = PAGES = None
CONF = {}


def init(root, conf):
    global ROOT, PAGES, CONF
    ROOT, PAGES, CONF = root, conf["pages"], conf


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


SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def bib_append(entry, key, doi=""):
    """Add a citation unless the file already has it. Matched on the doi first and the key
    second: the same paper arriving from doi.org carries a different key than one keyed by
    hand, and two entries for one paper is worse than a refused import."""
    path = CONF["bibliography"]
    old = path.read_text() if path.exists() else ""
    if doi:
        m = re.search(r"@\w+\s*\{\s*([^,\s]+)[^@]*?doi\s*=\s*[{\"]\s*" +
                      re.escape(doi) + r"\s*[}\"]", old, re.I)
        if m:
            return None, m.group(1)
    if re.search(r"@\w+\s*\{\s*" + re.escape(key) + r"\s*,", old, re.I):
        return None, key
    entry = re.sub(r"^(@\w+\s*\{\s*)[^,\s]+", lambda m: m.group(1) + key, entry.strip(), count=1)
    path.write_text((old.rstrip() + "\n\n" + entry + "\n") if old.strip() else entry + "\n")
    return key, key


def doi_bibtex(doi):
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip())
    req = urllib.request.Request("https://doi.org/" + doi,
                                 headers={"Accept": "application/x-bibtex",
                                          "User-Agent": "sloop-library"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8").strip()


def spawn_librarian(key):
    """Detached on purpose: reading a paper takes a minute, and the page picks the result up
    over live reload rather than holding the request open for it."""
    subprocess.Popen([sys.executable, "-m", "sloop.librarian", str(ROOT), key],
                     cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def install(src, dst):
    """Make the served tree match a finished build: copy everything over, then drop what the
    build no longer has. Nothing is ever missing from dst in between, so a request that
    lands mid-rebuild still answers."""
    src, dst = Path(src), Path(dst)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    keep = {p.relative_to(src) for p in src.rglob("*")}
    for p in sorted(dst.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if p.relative_to(dst) in keep:
            continue
        if p.is_file():
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()


class Handler(http.server.SimpleHTTPRequestHandler):
    edit = False
    flags = []

    # live/, one long-lived response per open page. The build id changes after every
    # successful rebuild, which is the only thing a viewer needs to know to follow along.
    build_id = "0"
    waiters = []
    waiters_lock = threading.Lock()

    @classmethod
    def announce(cls):
        cls.build_id = str(time.time_ns())
        with cls.waiters_lock:
            for q in list(cls.waiters):
                q.put(cls.build_id)

    def stream_builds(self):
        q = queue.Queue()
        with Handler.waiters_lock:
            Handler.waiters.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(f"data: {Handler.build_id}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=20)}\n\n".encode())
                except queue.Empty:
                    # a proxy will drop a connection that says nothing for long enough
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass            # the page was closed or reloaded, which is the ordinary ending
        finally:
            with Handler.waiters_lock:
                if q in Handler.waiters:
                    Handler.waiters.remove(q)

    def send_response(self, *a, **k):
        self._cached = False
        super().send_response(*a, **k)

    def send_header(self, keyword, value):
        if keyword.lower() == "cache-control":
            self._cached = True
        super().send_header(keyword, value)

    def end_headers(self):
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
        self.send_header("Referrer-Policy", "no-referrer")
        # Revalidate every time. Without this the page is heuristically cacheable, and a
        # viewer can sit on a copy old enough to predate the live/ client and never hear
        # that anything changed.
        if not getattr(self, "_cached", False):
            self.send_header("Cache-Control", "no-cache")
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
        # the library's pdfs live beside the pages, not in the build output: one copy on disk
        path = self.path.split("?")[0]
        # A page is a directory, and a request for it without the slash gets a redirect. The
        # standard one is absolute, which loses any path the site is mounted under, so this
        # one is relative to the request and survives the mount.
        if (not path.endswith("/") and "." not in Path(path).name and ".." not in path.split("/")
                and (Path(self.directory) / path.lstrip("/") / "index.html").is_file()):
            self.send_response(301)
            self.send_header("Location", Path(path).name + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/library/pdf/") and path.endswith(".pdf"):
            name = Path(path).name
            f = ROOT / "library" / "pdf" / name
            if name != path[len("/library/pdf/"):] or not f.is_file():
                return self.reply(404, {"error": "no such pdf"})
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(f.stat().st_size))
            self.end_headers()
            self.wfile.write(f.read_bytes())
            return
        if path.rstrip("/") == "/shares":
            return self.shares_api("GET", None)
        if path.rstrip("/") == "/live":
            return self.stream_builds()
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

    def library_post(self, path):
        """doi/ adds a citation. pdf/<key> stores a paper and starts reading it."""
        if not self.edit or self.headers.get("Tailscale-Funnel-Request"):
            return self.reply(404, {"error": "importing is off"})
        n = int(self.headers.get("Content-Length", 0))
        if n > 80 * 1024 * 1024:
            return self.reply(413, {"error": "over 80MB"})
        raw = self.rfile.read(n)

        if path == "/library/doi":
            try:
                doi = json.loads(raw)["doi"]
            except (ValueError, KeyError):
                return self.reply(400, {"error": "expected {doi}"})
            try:
                entry = doi_bibtex(doi)
            except (urllib.error.URLError, OSError) as e:
                return self.reply(502, {"error": f"doi.org said: {e}"})
            m = re.match(r"\s*@\w+\s*\{\s*([^,\s]+)\s*,", entry)
            if not m:
                return self.reply(502, {"error": "doi.org did not return bibtex"})
            key = m.group(1).lower()
            if not SAFE_KEY.match(key):
                return self.reply(502, {"error": f"unusable key {key!r}"})
            clean = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip())
            with SAVING:
                added, key = bib_append(entry, key, clean)
            return self.reply(200, {"key": key, "added": bool(added),
                                    "note": "cited" if added else "already cited"})

        key = path[len("/library/pdf/"):]
        if not SAFE_KEY.match(key):
            return self.reply(400, {"error": "the key must be letters, digits, . _ or -"})
        if not raw.startswith(b"%PDF"):
            return self.reply(400, {"error": "that is not a pdf"})
        d = ROOT / "library" / "pdf"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.pdf").write_bytes(raw)
        spawn_librarian(key)
        return self.reply(200, {"key": key, "reading": True})

    def shares_api(self, method, page):
        """GET shares lists them; POST and DELETE shares/<page> share a page and take it back.
        Writes, so gated like editing, and never answered through Funnel."""
        if not self.edit or self.headers.get("Tailscale-Funnel-Request"):
            return self.reply(404, {"error": "sharing is off"})
        from . import build as builder
        if method == "GET":
            base = str(CONF["share"].get("url") or "").rstrip("/")
            return self.reply(200, {"shares": [
                {"token": t, "page": pg, "when": w, "url": f"{base}/{t}/" if base else ""}
                for t, pg, w in builder.shares()]})
        if not page or not SAFE_KEY.match(page) or not (PAGES / page).is_dir():
            return self.reply(404, {"error": f"no page {page}"})
        with SAVING:
            if method == "POST":
                token, url = builder.share(page)
                return self.reply(200, {"token": token, "url": url, "page": page})
            try:
                builder.unshare(page)
            except SystemExit as e:
                return self.reply(404, {"error": str(e)})
            return self.reply(200, {"page": page, "removed": True})

    def do_DELETE(self):
        p = self.path.split("?")[0]
        if p.startswith("/shares/"):
            return self.shares_api("DELETE", p[len("/shares/"):].strip("/"))
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith("/shares/"):
            return self.shares_api("POST", p[len("/shares/"):].strip("/"))
        if p == "/library/doi" or p.startswith("/library/pdf/"):
            return self.library_post(p)
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
                install(tmp, self.directory)
                Handler.announce()
        self.reply(200, {"hash": digest(text)})


class ShareHandler(http.server.BaseHTTPRequestHandler):
    """The public face, and all of it.

    One shared page per token and nothing else: no listing, no index, no other path, no
    method but GET and HEAD. Funnel points at this port and at nothing else on the machine,
    so what this refuses, the internet cannot reach.
    """
    root = None
    TOKEN = re.compile(r"^/share/([A-Za-z0-9_-]{8,64})/(?:index\.html)?$")

    def do_GET(self):
        self.answer(True)

    def do_HEAD(self):
        self.answer(False)

    def answer(self, with_body):
        m = self.TOKEN.match(self.path.split("?")[0])
        f = (self.root / "share" / m.group(1) / "index.html") if m else None
        self.send_response(200 if f and f.is_file() else 404)
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if not (f and f.is_file()):
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = f.read_bytes()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if with_body:
            self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def share_server(root, conf):
    port = int(conf["share"]["port"])
    ShareHandler.root = root
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), ShareHandler)
    print(f"  http://127.0.0.1:{port}/share/<token>/   serving {root / 'share'}", flush=True)
    srv.serve_forever()


def fingerprint(root, conf):
    """What a rebuild depends on, cheaply.

    .git/logs/HEAD is in here because a page's Updated stamp comes from git rather than from
    mtimes, and committing touches no watched file. Without it every stamp sits one build
    behind, from the edit until someone rebuilds by hand.

    The reading library is in here too, so a new summary, pdf or reordering shows without a
    rebuild by hand. Its pdfs are fingerprinted by size and mtime, not read: there are dozens
    of them and the poll runs every second.
    """
    h = hashlib.sha256()
    watched = sorted(p for p in conf["pages"].rglob("*") if p.is_file())
    for f in watched + [conf["bibliography"], root / "notebook.yml", root / ".git/logs/HEAD"]:
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"-")
        h.update(str(f).encode())
    lib = root / "library"
    for f in sorted(p for p in lib.rglob("*") if p.is_file()) if lib.is_dir() else []:
        try:
            if f.suffix == ".pdf":
                st = f.stat()
                h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
            else:
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
            install(tmp, site)
            Handler.announce()
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
