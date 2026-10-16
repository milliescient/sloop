#!/usr/bin/env python3
"""Build the notebook, a page's assets, or a page's artifacts.

Run from a notebook directory, which is any directory holding a notebook.yml. See cli.py
for the commands and paths.py for what the config holds.

Building the notebook reads assets only, so a clone builds with no checkout at all.

Each entry is a file whose header is `key: value` lines terminated by `---`, then a body.
A .md body is rendered as Markdown, a .html body is passed through. Markdown keeps raw
HTML, so a table that needs per-cell classes stays HTML inside a .md entry.
The header is YAML, terminated by `---`. Required: title, date, tags. A page whose numbers
come from scripts also declares `steps:` and perhaps `checks:`, which lock.py reads and runs;
this module only renders, and asks lock.py whether a page's record still holds.

A page directory's name is its shortname, and that is its identity: it is the slug in a URL
and the selector `sloop assets` matches on. It carries no number and no title, so renaming a
page is free and retitling one changes nothing on disk. Display order is by date, newest
first, and the date derivation follows renames.

A page is a directory holding its `page.md`, whatever its steps need beside them, and the
outputs they write there, which are committed with it. That is why a clone renders without any
checkout. A `{{key}}` anywhere in the page is replaced by the value a step wrote for it, so a
number cannot drift from the computation behind it: rerun the step and the prose follows. A
`{{key}}` with no value stops the render, as does an embedded file that is missing. render.py
has what else a page can write.

Routes: #/ everything, #/tag/<tag> filtered, #/entry/<slug>.

Writes one self-contained page to site/index.html, which works at any mount path.
"""
import json
import html
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from string import Template

import yaml

from . import deps as D
from . import lock as L
from . import render as R

# init() fills these in from the notebook's config: the builder is installed once, and
# every path it touches comes from the directory it was pointed at.
ROOT = PAGES = SITE = BIB = None
SOURCES, PALETTE = [], {}
CONF, TITLE, REPO, BRANCH = {}, "notebook", "", "main"


def init(root, conf):
    global ROOT, PAGES, SOURCES, SITE, CONF, TITLE, REPO, BRANCH, BIB, PALETTE
    ROOT, CONF = root, conf
    PAGES, SITE = conf["pages"], conf["out"]
    SOURCES = conf.get("sources") or []
    TITLE, REPO, BRANCH = conf["title"], conf["repo"], conf["branch"]
    BIB = R.Bibliography(conf["bibliography"])
    PALETTE = {R.colour(str(k)): str(v).lstrip("-") for k, v in (conf.get("figure_colors") or {}).items()}
    D.init(root, conf)
    L.init(root, conf)

META_KEYS = ("title", "date", "tags")
MONTHS = [None, "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def stamp_text(iso):
    """2026-08-02T14:29:03-05:00 -> 2026-08-02 14:29. Seconds and offset are noise here."""
    return iso[:10] + " " + iso[11:16] if len(iso) >= 16 else iso[:10]


def last_activity():
    """When each page directory's contents last changed.

    A page is a directory, so git already tracks one history per page. One walk of the log
    is cheaper than a call per page, and a page with no commit yet simply has none.

    The walk follows renames, because a page's directory name is not its identity: renaming
    one must not reset its date. An exact rename moved no content, so it is not activity, and
    commits under the old name are credited to the name the page carries now.

    A directory rename is read off the PAGE FILE alone. Rename detection pairs files by
    content, and the per-page scaffold files are byte-identical across pages, so git will
    happily pair one page's .env.default with another's and the alias comes out crossed. A
    page file is unique to its page, so pairing it cannot cross.

    The timestamp marker is @ rather than a letter, since --name-status writes a copy as
    C100<TAB>old<TAB>new and any status letter would be read as a date.

    Only pages that exist now are returned. The log also holds pages since deleted, and loose
    files from before a page was a directory, and a key that no page can ever be looked up by
    is noise in anything that prints the result.
    """
    try:
        out = subprocess.run(["git", "log", "--format=@%cI", "--name-status", "-M",
                              "--", PAGES.name],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}

    def page_of(path):
        parts = path.split("/", 2)
        return parts[1] if len(parts) > 1 and parts[0] == PAGES.name and parts[1] else None

    def is_page_file(path):
        return path.rsplit("/", 1)[-1] in ("page.md", "page.html")

    # a page is a directory holding a page file, which is also what the render walks
    try:
        live = {d.name for d in PAGES.iterdir()
                if d.is_dir() and any((d / f).exists() for f in ("page.md", "page.html"))}
    except OSError:
        return {}

    seen, renamed_to, when = {}, {}, None

    def now_called(name):
        # the log is newest first, so a rename is read before the commits under the old name
        chain = set()
        while name in renamed_to and name not in chain:
            chain.add(name)
            name = renamed_to[name]
        return name

    for line in out.stdout.splitlines():
        if line.startswith("@"):
            when = line[1:]
            continue
        if not when or "\t" not in line:
            continue
        fields = line.split("\t")
        if fields[0].startswith("R") and len(fields) == 3:
            old_name, new_name = page_of(fields[1]), page_of(fields[2])
            if (old_name and new_name and old_name != new_name
                    and is_page_file(fields[1]) and is_page_file(fields[2])):
                renamed_to[old_name] = now_called(new_name)
            if fields[0] == "R100":
                continue                                  # a pure rename is not an update
            page = new_name
        else:
            page = page_of(fields[-1])
        if page:
            seen.setdefault(now_called(page), when)
    return {name: when for name, when in seen.items() if name in live}


def written(page_dir):
    """When a page with no commit yet was last written, in the form the log gives."""
    try:
        newest = max(p.stat().st_mtime for p in page_dir.iterdir() if p.is_file())
    except (OSError, ValueError):
        return ""
    return datetime.fromtimestamp(newest).astimezone().isoformat(timespec="seconds")


def is_draft(path):
    """Read only the header. A draft must not block a build, so a broken one is left
    for parse() to report rather than raising here."""
    head, _, _ = path.read_text().partition("\n---\n")
    try:
        h = yaml.safe_load(head) or {}
        return bool(h.get("draft", False) or h.get("scratch", False))
    except yaml.YAMLError:
        return False


def parse(path):
    head, _, body = path.read_text().partition("\n---\n")
    name = path.parent.name
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{name}: header is not valid YAML\n  {e}")
    # a scratch page carries no tags, so it is not held to them
    required = [k for k in META_KEYS if k != "tags" or not meta.get("scratch")]
    missing = [k for k in required if k not in meta]
    if missing:
        raise SystemExit(f"{name}: missing {', '.join(missing)}")
    meta.setdefault("tags", [])
    meta["tags"] = [str(x).strip() for x in meta["tags"]] if isinstance(meta["tags"], list) \
        else [t.strip() for t in str(meta["tags"]).split(",") if t.strip()]
    meta["date"] = str(meta["date"])
    try:
        page = R.Page(name, path.parent, R.read_values(path.parent), BIB, PALETTE)
        # a .html page is passed through, and still has its values filled
        meta["body"] = page.render(body.strip()) if path.suffix == ".md" else page.fill(body.strip())
    except R.Failure as e:
        raise SystemExit(f"{name}: {e}")
    # a scratch page is reachable and never listed: no index row, no tag or date counts,
    # and never published
    meta["scratch"] = bool(meta.get("scratch", False))
    meta["draft"] = bool(meta.get("draft", False)) or meta["scratch"]
    meta["slug"] = name
    meta["path"] = str(path)
    if not meta["tags"] and not meta["scratch"]:
        raise SystemExit(f"{name}: needs at least one tag")
    return meta


def provenance(page_dir):
    """The block a reader can open: each step's versions, inputs and outputs, as links.
    The hashes stay in the lock.

    Everything here is read from the lock, so it says what was recorded where the work was
    done, and it is on the page whether or not this machine could check any of it.
    """
    plan = L.Plan(page_dir, strict=False)
    lock = L.read_lock(page_dir)

    def use(u):
        name, _, ver = u.partition("@")
        dep = plan.deps.get(name)
        href = D.link(dep, ver) if dep else ""
        label = html.escape(u)
        return (f'<a class="ref" href="{html.escape(href)}" rel="noreferrer">{label}</a>' if href
                else f'<span class="ref">{label}</span>')

    def uses(us):
        return "".join(use(u) for u in dict.fromkeys(us))

    def href(p, pinned):
        """Where a reader can open a path: the notebook's own files, and a pinned repository's."""
        first, _, rest = p.partition("/")
        dep = plan.deps.get(first)
        if dep is None:
            try:
                rel = (page_dir / p).resolve().relative_to(ROOT.resolve())
            except ValueError:
                return ""
            return f"{REPO}/blob/{BRANCH}/{rel.as_posix()}" if REPO else ""
        ver = re.sub(r"(\+uncommitted|-dirty)$", "", pinned.get(first, ""))
        if rest and "github.com" in dep["url"] and re.fullmatch(r"[0-9a-f]{6,40}", ver):
            return f'{dep["url"].rstrip("/")}/blob/{ver}/{rest}'
        return ""

    def row(label, cell):
        return f'<dt>{label}</dt><dd>{cell}</dd>' if cell else ""

    block = row("uses", uses(plan.rests_on))
    block = f'<dl class="page">{block}</dl>' if block else ""
    for name in plan.order:
        rec = lock["steps"].get(name) or {}
        made = rec.get("made_with") or {}
        pinned = dict(u.split("@", 1) for u in rec.get("uses") or [] if "@" in u)

        def paths(side):
            out = ""
            for p in rec.get(side) or {}:
                to = href(p, pinned)
                label = f"<code>{html.escape(p)}</code>"
                label = f'<a href="{html.escape(to)}" rel="noreferrer">{label}</a>' if to else label
                out += f'<div class="path">{label}</div>'
            return out

        # a step that states no version still rests on the ones that made what it reads
        through = []
        for p in rec.get("inputs") or {}:
            # and what a manifest beside a path says made it: shown and linked, never checked
            by, _ = plan.producer(p)
            if by and by != name:
                through += (lock["steps"].get(by) or {}).get("uses") or []
            through += made.get(p) or []
        stated = rec.get("uses") or []
        through = [u for u in dict.fromkeys(through) if u not in stated]
        used = uses(stated) + (f'<span class="via">through its inputs</span>{uses(through)}' if through else "")
        note = (" <em>frozen</em>" if plan.steps[name]["frozen"] else "") + ("" if rec else " <em>no record</em>")
        block += (f'<section class="step"><p class="stepname">{html.escape(name)}{note}</p><dl>'
                  + row("uses", used) + row("input", paths("inputs")) + row("output", paths("outputs"))
                  + '</dl></section>')
    if plan.checks:
        block += '<ul class="checks">'
        for name, c in plan.checks.items():
            got = (lock["checks"].get(name) or {}).get("result")
            ok = got is not None and str(got) == c["expect"]
            block += (f'<li class="{"ok" if ok else "bad"}">check: {html.escape(name)} '
                      f'&mdash; expected <code>{html.escape(c["expect"])}</code>, '
                      f'got <code>{html.escape(str(got))}</code></li>')
        block += "</ul>"
    if lock["accepted"]:
        block += '<ul class="accepted">'
        for a in lock["accepted"]:
            what = a.get("path") or a.get("step") or a.get("uses") or ""
            if a.get("step"):
                what = f'{a["step"]}: {", ".join(a.get("uses") or [])}'.rstrip(": ")
            moved = f' ({html.escape(str(a["was"]))} &rarr; {html.escape(str(a["now"]))})' if a.get("was") else ""
            block += (f'<li>accepted: <code>{html.escape(str(what))}</code>{moved} '
                      f'&mdash; {html.escape(str(a.get("why", "")))}</li>')
        block += "</ul>"
    return (f'<details class="prov"><summary><span class="ref">provenance</span></summary>'
            f'<div class="provbody">{block}</div></details>') if block else ""


def read_sources():
    """The reading list, as [name, kind, url, note] rows. It lives in notebook.yml."""
    rows = []
    for r in SOURCES:
        if not isinstance(r, dict) or not r.get("name"):
            raise SystemExit("notebook.yml: each of sources needs a name, and may have kind, url, note")
        rows.append([str(r.get(k) or "") for k in ("name", "kind", "url", "note")])
    return rows


def sidebar_sources(rows):
    """What the sidebar lists: anything with a URL, minus the ref-only rows that exist just
    to resolve a citation. The namesake sits last however the file is ordered."""
    listed = [r for r in rows if r[2] and r[1] != "ref-only"]
    return sorted(listed, key=lambda r: r[1] == "namesake")



def library_markup():
    """The reading library, rendered into the pane rather than into a page.

    Every key in references.bib with a pdf beside it is a paper. The pane lists them, and
    picking one puts its notes there and its pdf in the main area. There is no bibliography
    page: the citation list is a way to reach a paper, not somewhere to read.
    """
    refs = BIB.load()
    if not refs:
        return None
    lib = ROOT / "library"
    pdfs = {f.stem for f in (lib / "pdf").glob("*.pdf")} if (lib / "pdf").is_dir() else set()
    notes = {}
    for f in lib.glob("*.json") if lib.is_dir() else []:
        try:
            notes[f.stem] = json.loads(f.read_text())
        except (ValueError, OSError):
            notes[f.stem] = {"error": f"{f.name} could not be read"}

    def notes_html(key):
        n = notes.get(key) or {}
        out = ""
        if n.get("one_line"):
            out += f'<p class="libone">{html.escape(str(n["one_line"]))}</p>'
        if n.get("summary"):
            out += f'<p>{html.escape(str(n["summary"]))}</p>'
        pts = n.get("points") or []
        if pts:
            out += "<h4>Points</h4><ul>" + "".join(
                f"<li>{html.escape(str(x))}</li>" for x in pts) + "</ul>"
        if n.get("age_uncertainty"):
            out += f'<h4>Age uncertainty</h4><p>{html.escape(str(n["age_uncertainty"]))}</p>'
        con = n.get("connection") or []
        if con:
            out += "<h4>Connection</h4><ul>" + "".join(
                f"<li>{html.escape(str(x))}</li>" for x in con) + "</ul>"
        for q in (n.get("quotes") or [])[:3]:
            where = f' <span class="qwhere">{html.escape(str(q.get("where", "")))}</span>' if q.get("where") else ""
            out += f'<blockquote>{html.escape(str(q.get("text", "")))}{where}</blockquote>'
        if n.get("status") == "reading":
            out = "<p>Being read now. This fills in when it is done.</p>"
        elif n.get("error"):
            out = f'<p class="liberr">{html.escape(str(n["error"]))}</p>'
        return out or "<p>Nothing written yet.</p>"

    def row(key, starred=False):
        n = notes.get(key) or {}
        has_pdf = key in pdfs
        head = (f'<a class="libpdf" href="library/pdf/{html.escape(key)}.pdf" '
                f'target="_blank" rel="noreferrer">pdf</a>' if has_pdf
                else '<span class="libpdf none">no pdf</span>')
        one = f'<p class="libone">{html.escape(str(n["one_line"]))}</p>' if n.get("one_line") else ""
        body = ""
        if n.get("summary"):
            body += f'<p>{html.escape(str(n["summary"]))}</p>'
        for pt in (n.get("points") or [])[:8]:
            body += f'<li>{html.escape(str(pt))}</li>'
        if body and "<li>" in body:
            body = body.replace("<li>", "<ul><li>", 1) + "</ul>"
        if not has_pdf:
            return ""
        title = str((refs.get(key) or {}).get("title") or key)
        return (f'<a class="librow{" start" if starred else ""}" href="#/paper/{html.escape(key)}">'
                f'<span class="libwho">{html.escape(BIB.names(key))} {html.escape(BIB.year(key))}'
                + ('<span class="libstart" title="start here">&#9679;</span>' if starred else "")
                + '</span>'
                f'<span class="libtitle">{html.escape(title.rstrip("."))}</span>'
                + (f'<span class="libone">{html.escape(str(n["one_line"]))}</span>' if n.get("one_line")
                   else ' <span class="libstate">unread</span>')
                + '</a>')

    order = sorted(refs, key=lambda k: (str(refs[k].get("year") or ""), k), reverse=True)
    # the notebook's own reading order. A key left out of it still shows, under Elsewhere,
    # so an import is never hidden by not having been curated yet
    # the order lives beside the papers, not in notebook.yml: nothing under library/ is
    # tracked, so the curation travels with the thing it curates
    conf = {}
    ordering = lib / "order.yml"
    if ordering.is_file():
        try:
            conf = yaml.safe_load(ordering.read_text()) or {}
        except yaml.YAMLError as e:
            raise SystemExit(f"library/order.yml is not valid YAML\n  {e}")
    start = set(conf.get("start") or [])
    rows, placed = "", set()
    for g in conf.get("groups") or []:
        keys = [k for k in (g.get("keys") or []) if k in refs and k in pdfs]
        if not keys:
            continue
        placed.update(keys)
        rows += (f'<h3 class="libgroup">{html.escape(str(g.get("title", "")))}</h3>'
                 + (f'<p class="libnote">{html.escape(str(g["note"]))}</p>' if g.get("note") else "")
                 + "".join(row(k, k in start) for k in keys))
    rest = [k for k in order if k in pdfs and k not in placed]
    if rest:
        rows += ('<h3 class="libgroup">Elsewhere</h3>'
                 + "".join(row(k, k in start) for k in rest))
    # every paper's notes, kept out of the flow: the notes panel shows one at a time
    store = "".join(
        f'<div class="papernotes" id="pn-{html.escape(k)}" data-title="{html.escape(BIB.names(k))} '
        f'{html.escape(BIB.year(k))}" data-pdf="{"1" if k in pdfs else ""}" '
        f'data-doi="{html.escape(str((refs.get(k) or {}).get("doi") or ""))}">{notes_html(k)}</div>'
        for k in order)
    n_pdf = len(pdfs & set(refs))
    up = ('<svg viewBox="0 0 14 14" width="13" height="13" fill="none" aria-hidden="true">'
          '<path d="M7 9.5V2.5M7 2.5 4.2 5.3M7 2.5l2.8 2.8" stroke="currentColor"'
          ' stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
          '<path d="M2.2 10v1.8h9.6V10" stroke="currentColor" stroke-width="1.3"'
          ' stroke-linecap="round"/></svg>')
    lead = ('<form class="libadd" id="libadd" onsubmit="return false">'
            '<input id="libdoi" type="text" placeholder="paste a doi" autocomplete="off">'
            '<button id="libdoibtn" type="button">cite</button>'
            f'<label class="libdrop" for="libpdf" title="add a pdf">{up}</label>'
            '<input id="libpdf" type="file" accept="application/pdf" multiple hidden>'
            '</form><p class="libsay" id="libsay"></p>'
            f'<p class="libcount">{n_pdf} papers of {len(refs)} citations.</p>')
    return (f'<div id="liblist">{lead}{rows}</div>'
            f'<div id="pnstore" hidden>{store}</div>')


def build(site=None, drafts=True):
    files = sorted(p for d in sorted(PAGES.iterdir()) if d.is_dir()
                   for p in (d / "page.md", d / "page.html") if p.exists())
    held = [] if drafts else [p for p in files if is_draft(p)]
    files = [p for p in files if p not in held]
    entries = [parse(p) for p in files]
    if not entries:
        held_note = f", {len(held)} held as drafts" if held else ""
        raise SystemExit(f"nothing to build: no publishable pages{held_note}")
    activity = last_activity()
    unsettled = {}
    for e in entries:
        # the timestamp orders, the date displays: two pages committed the same day
        # still sort in the order they were touched
        page_dir = Path(e["path"]).parent
        e["stamp"] = activity.get(e["slug"]) or written(page_dir) or e["date"]
        e["updated"] = stamp_text(e["stamp"])
        e["why"] = L.gate(page_dir)
        e["prov"] = provenance(page_dir)
        if e["why"]:
            unsettled[e["slug"]] = e["why"]
    # A page whose record does not hold is shown as such while it is being worked on, and is
    # never published: that build fails, rather than quietly including or leaving out the page.
    if unsettled and not drafts:
        lines = "".join(f"\n  {slug}: {w}" for slug, why in unsettled.items() for w in why)
        raise SystemExit("not published. These pages are provisional, and none is marked "
                         f"draft:{lines}")
    recent = sorted(entries, key=lambda e: (e["stamp"], e["slug"]), reverse=True)
    listed = [e for e in entries if not e["scratch"]]
    counts = Counter(t for e in listed for t in e["tags"])
    rows = read_sources()

    months = Counter(e["date"][:7] for e in listed)
    dates = "".join(
        f'<button class="tag" data-date="{m}" aria-pressed="false">'
        f'<span>{MONTHS[int(m[5:7])]} {m[:4]}</span><span class="n">{n}</span></button>'
        for m, n in sorted(months.items(), reverse=True))

    filters = "".join(
        f'<button class="tag" data-tag="{html.escape(t)}" aria-pressed="false">'
        f'<span>{html.escape(t)}</span><span class="n">{n}</span></button>'
        for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    scratch = "".join(
        f'<a class="scratchlink" href="#/entry/{e["slug"]}">{html.escape(e["title"])}</a>'
        for e in recent if e["scratch"])

    scratch_slug = next((e["slug"] for e in recent if e["scratch"]), "")
    panes = [r for r in ((CONF.get("serve") or {}).get("panes") or []) if r.get("url")]
    term = next((r for r in panes if r.get("label") == "terminal"), None)
    LIB = library_markup()
    rail = (
        '<div id="rail">'
        '<button id="panebtn" aria-pressed="false" title="pane  ctrl+`">'
        '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">'
        '<path d="M3.5 1.5 L8 6 L3.5 10.5" stroke="currentColor" stroke-width="1.6"'
        ' stroke-linecap="round" stroke-linejoin="round"/></svg></button>'
        + '<div id="nav" role="radiogroup" aria-label="pane">'
        '<button class="navbtn" id="navnotebook" role="radio" aria-checked="true"'
        ' data-view="notebook" title="notebook">notebook</button>'
        + "".join(
            f'<button class="scratchbtn" data-slug="{html.escape(e["slug"])}"'
            f' aria-pressed="false" title="{html.escape(e["title"])}">'
            '<svg viewBox="0 0 10 10" width="8" height="8" aria-hidden="true">'
            '<rect x="1.2" y="1.2" width="7.6" height="7.6" rx="1.4" fill="none"'
            ' stroke="currentColor" stroke-width="1.4"/></svg></button>'
            for e in recent if e["scratch"])
        + ('<button class="navbtn" id="navlibrary" role="radio" aria-checked="false"'
           ' data-view="library" title="library">library</button>' if LIB else "")
        + '</div>'
        + '</div>'
        f'<div id="pane" data-term="{html.escape(str(term["url"])) if term else ""}">'
        '<div id="termbody"></div>'
        '<div id="libwrap"><div id="notesbar"><span id="notestitle"></span>'
        '<button id="notesback" title="back to the papers">&#8592;</button></div>'
        f'<div id="libbody">{LIB}<div id="notesbody"></div></div></div></div>'
        '<div id="panesplit" role="separator" tabindex="0" aria-label="resize"></div>'
        '<div id="paperwrap"><iframe id="paper" title="paper"></iframe></div>')

    toc = ""
    body = ""
    for e in recent:
        if not e["scratch"]:
            toc += (
                f'<a class="tocrow" href="#/entry/{e["slug"]}" '
                f'data-tags="{html.escape("|".join(e["tags"]))}" data-month="{e["date"][:7]}" '
                f'title="{html.escape(e["title"])}">'
                f'<span class="toctitle">{html.escape(e["title"])}</span>'
                f'<span class="tocdate">{e["updated"]}</span></a>')
        tags = "".join(
            f'<button class="tag sm" data-tag="{html.escape(t)}" aria-pressed="false">'
            f'{html.escape(t)}</button>' for t in e["tags"])
        hist = (f'<a class="ref" href="{REPO}/tree/{BRANCH}/{PAGES.name}/{e["slug"]}"'
                f' rel="noreferrer">github</a>') if REPO else ""
        banner = ""
        if e["why"]:
            banner = ('<div class="provisional"><p><strong>Provisional.</strong> These numbers are '
                      'not settled:</p><ul>'
                      + "".join(f"<li>{html.escape(w)}</li>" for w in e["why"]) + "</ul></div>")
        body += f"""
      <section class="entry" id="{e['slug']}" data-tags="{html.escape('|'.join(e['tags']))}"
               data-month="{e['date'][:7]}">
        <div class="text">
        <p class="label">{e['updated']} {tags}</p>
        <h2 class="etitle"><a class="anchor" href="#/entry/{e['slug']}">{html.escape(e['title'])}</a></h2>
        {banner}
        {e['body']}
        </div>
        <div class="foot">{hist}{e['prov']}</div>
      </section>"""

    # every dependency with a url that some page ran, then the reading list
    ref_only = {r[2].rstrip("/") for r in rows if r[1] == "ref-only"}
    named = {r[2].rstrip("/"): r[0] for r in rows if r[2]}
    cited = {}
    for e in entries:
        plan = L.Plan(Path(e["path"]).parent, strict=False)
        for step in plan.steps.values():
            for name, _ in step["uses"]:
                u = plan.deps[name]["url"].rstrip("/")
                if u and "{version}" not in u and u not in ref_only:
                    cited.setdefault(u, named.get(u, name))
    listed = [(name, url) for url, name in cited.items()]
    for r in sidebar_sources(rows):                       # extras no page ran
        if r[2].rstrip("/") not in cited:
            listed.append((r[0], r[2]))
    srcs = "".join(
        f'<a class="src" href="{u}" rel="noreferrer">{html.escape(n)}</a>'
        for n, u in listed)

    page = Template(TEMPLATE).safe_substitute(
        title=TITLE, filters=filters, dates=dates, toc=toc, entries=body, scratch=scratch, rail=rail,
        sources=srcs,
        n=len(entries), latest=recent[0]["updated"],
        )
    emit(page, site)
    L.HASHER.save()
    for p in held:
        print(f"  draft, not published: {p.parent.name}")
    for slug, why in unsettled.items():
        print(f"  provisional: {slug}: {why[0]}" + (f" (and {len(why) - 1} more)" if len(why) > 1 else ""))
    print(f"{len(entries)} pages, {len(counts)} tags, "
          f"{len(sidebar_sources(rows))} sources listed of {len(rows)}")
    print(f"  newest: {recent[0]['slug']} ({recent[0]['date']}) {recent[0]['title'][:52]}")


def emit(page, site=None):
    """One self-contained file.

    Splitting out style.css and app.js broke the moment the site was mounted at a path:
    a browser at /notebook resolves href="style.css" to /style.css, which is not there.
    Inlining costs 8 KB and makes the page correct at any mount depth, with or without a
    trailing slash, and over file:// too.
    """
    site = site or SITE
    site.mkdir(parents=True, exist_ok=True)
    # Jekyll would otherwise swallow anything whose name starts with _ or .
    (site / ".nojekyll").write_text("")
    out = site / "index.html"
    out.write_text(page)
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"  {shown}  {out.stat().st_size // 1024} KB")


TEMPLATE = r"""<meta name="robots" content="noindex, nofollow, noarchive, nosnippet" />
<title>$title</title>
<style>
  :root {
    /* Warm neutrals pulled down from Wilf's #996633 ground and #cc9933 highlight, with
       his #3333ff on the wordmark, his #ff6600 tamed for links and his #551a8b for
       visited. The semantic colours stay separate from the accent so a green chip never
       has to mean "link". */
    --ground:#EFE7DA; --panel:#F7F2E8; --ink:#1E1611; --muted:#6B5B4A; --rule:#D8CBB6;
    --accent:#A8480A; --visited:#551A8B; --blue:#2B2BB8;
    --ok:#2C6B4E; --amber:#8A5A12; --oxide:#9C3A30; --chip-bg:#FFFCF6; --sel:#EBDCC2;
    /* two figure series, checked for colour-blind separation against --panel */
    --fig1:#2A78D6; --fig2:#EB6834;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
    --measure:64ch; --s1:.4rem; --s2:.8rem; --s3:1.4rem; --s4:2.2rem; --s5:3.4rem;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) figure.plain img, :root:not([data-theme="light"]) figure.plain svg {
      filter:invert(1) hue-rotate(180deg); }
:root { --ground:#17120E; --panel:#201913; --ink:#E9E0D2; --muted:#A08D77; --rule:#3A2E24;
      --accent:#E08A4A; --visited:#B9A0E0; --blue:#8C99FF;
      --ok:#5FB48C; --amber:#D2A05A; --oxide:#D77C6E; --chip-bg:#241C15; --sel:#3A2C1C; --fig1:#4F93E6; --fig2:#E06A36; }
  }
  :root[data-theme="dark"] { --ground:#17120E; --panel:#201913; --ink:#E9E0D2; --muted:#A08D77; --rule:#3A2E24;
      --accent:#E08A4A; --visited:#B9A0E0; --blue:#8C99FF;
      --ok:#5FB48C; --amber:#D2A05A; --oxide:#D77C6E; --chip-bg:#241C15; --sel:#3A2C1C; --fig1:#4F93E6; --fig2:#E06A36; }
  :root[data-theme="dark"] figure.plain img, :root[data-theme="dark"] figure.plain svg {
    filter:invert(1) hue-rotate(180deg); }
  :root[data-theme="light"] { --ground:#EFE7DA; --panel:#F7F2E8; --ink:#1E1611;
    --muted:#6B5B4A; --rule:#D8CBB6; --accent:#A8480A; --visited:#551A8B;
    --blue:#2B2BB8; --ok:#2C6B4E; --amber:#8A5A12; --oxide:#9C3A30;
    --chip-bg:#FFFCF6; --sel:#EBDCC2; --fig1:#2A78D6; --fig2:#EB6834; }

  body { background:var(--ground); color:var(--ink); font-family:var(--serif);
         font-size:17px; line-height:1.62; margin:0; }
  p { margin:0 0 var(--s2); } p:last-child { margin-bottom:0; }
  a { color:var(--accent); }
  a:visited { color:var(--visited); }
  code, .mono { font-family:var(--mono); font-size:.86em; font-variant-numeric:tabular-nums; }
  h1, h2 { text-wrap:balance; }
  :focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

  .topbar { position:sticky; top:0; z-index:5; background:var(--ground);
    border-bottom:1px solid var(--rule); }
  .topbar .inner { max-width:92rem; margin:0 auto; padding:var(--s2) var(--s3);
    display:grid; grid-template-columns:12rem minmax(0,1fr) 11rem; gap:var(--s5); }
  .topbar .head { grid-column:2; display:flex; flex-direction:column; gap:.15rem;
    max-width:var(--measure); }
  .wordmark { font-size:clamp(1.35rem,2.8vw,1.75rem); font-weight:600; line-height:1.15;
    letter-spacing:-.02em; overflow-wrap:anywhere; }
  .wordmark a { color:var(--blue); text-decoration:none; }
  .wordmark a:hover { text-decoration:underline; }
  .provenance { font-family:var(--mono); font-size:.7rem; color:var(--muted);
    display:flex; flex-wrap:wrap; gap:var(--s1) var(--s3); }
  .provenance b { color:var(--ink); font-weight:600; }
  .provenance a { text-decoration:none; }
  .provenance a:hover { text-decoration:underline; }

  .shell { display:grid; grid-template-columns:12rem minmax(0,1fr) 11rem; gap:var(--s5);
    max-width:92rem; margin:0 auto; padding:var(--s4) var(--s3) var(--s5); align-items:start; }
  .side { position:sticky; top:6.4rem; display:flex; flex-direction:column; gap:var(--s2);
    max-height:calc(100vh - 8rem); overflow-y:auto; }

  /* Two views over one pane and one main area. The notebook view pairs the terminal with
     the pages, the paper view pairs the notes with the pdf, and collapsing the pane gives
     whatever is in the main area the full width either way. */
  :root { --rail:30px; --pane:26rem; --shellbg:#1c1b19; }
  body:has(#rail) { padding-left:var(--rail); }
  body:has(#rail).paneopen { padding-left:calc(var(--rail) + var(--pane)); }
  body.paperview #page { display:none; }

  #rail { position:fixed; left:0; top:0; bottom:0; width:var(--rail); z-index:70;
    background:var(--shellbg); display:flex; flex-direction:column; align-items:center;
    gap:2px; padding-top:10px; }
  #rail button { appearance:none; border:0; background:none; color:#6f6859; cursor:pointer;
    width:100%; padding:6px 0; display:flex; align-items:center; justify-content:center;
    transition:color .15s ease; }
  #rail button:hover, #rail button:focus-visible { color:#ede7dc; outline:0; }
  #panebtn svg { display:block; transition:transform .22s ease; }
  body.paneopen #panebtn svg { transform:rotate(180deg); }
  #nav { margin-top:auto; padding-bottom:12px; display:flex; flex-direction:column;
    align-items:center; width:100%; }
  #rail .navbtn { font-family:var(--mono); font-size:.62rem; letter-spacing:.08em;
    writing-mode:vertical-rl; padding:7px 0; }
  #rail .navbtn[aria-checked="true"] { color:#ede7dc; }
  /* scratch belongs to the notebook, so it sits under it and is not one of the two views */
  #rail .scratchbtn { width:auto; padding:3px 0 7px; color:#4f4a40; }
  #rail .scratchbtn[aria-pressed="true"] { color:var(--accent, #d4714c); }
  #rail .scratchbtn svg { display:block; }

  #pane { position:fixed; left:var(--rail); top:0; bottom:0; width:0; z-index:65;
    overflow:hidden; background:var(--shellbg); visibility:hidden; }
  body.paneopen #pane { width:var(--pane); visibility:visible; }
  #termbody, #libwrap { position:absolute; inset:0; display:none; }
  body:not(.libview) #termbody { display:block; }
  body.libview #libwrap { display:flex; flex-direction:column; background:var(--panel); }
  body.libview #termbody { display:none; }
  #termbody iframe { width:100%; height:100%; border:0; display:block; }
  #notesbar { flex:0 0 auto; display:flex; align-items:baseline; justify-content:space-between;
    gap:var(--s2); padding:var(--s2) var(--s3) var(--s1); border-bottom:1px solid var(--rule); }
  #notestitle { font-family:var(--mono); font-size:.68rem; letter-spacing:.06em; color:var(--muted); }
  #notesback { border:0; background:none; padding:0; cursor:pointer;
    color:var(--muted); font-size:.9rem; line-height:1; }
  #notesback:hover { color:var(--accent); }
  #libbody { flex:1; min-height:0; overflow-y:auto; padding:var(--s2) var(--s3) var(--s4);
    font-size:.92rem; color:var(--ink); }
  body.paperview #liblist, body:not(.paperview) #notesbody { display:none; }
  body:not(.paperview) #notesbar { visibility:hidden; height:0; padding:0; border:0; }
  .librow { display:block; text-decoration:none; color:inherit; padding:var(--s2) 0;
    border-bottom:1px solid var(--rule); }
  .librow:hover { background:var(--ground, rgba(0,0,0,.03)); }
  .libwho { display:block; font-family:var(--mono); font-size:.64rem; letter-spacing:.05em;
    color:var(--muted); }
  .libtitle { display:block; color:var(--ink); }
  .libone { display:block; color:var(--muted); font-size:.86rem; margin-top:.15rem; }
  /* the import row reads as one of the rows, not as a form pasted above them */
  .libadd { display:flex; align-items:center; gap:var(--s2); padding:var(--s2) 0;
    border-bottom:1px solid var(--rule); }
  .libadd input[type=text] { flex:1; min-width:0; border:0; background:none; padding:0;
    font-family:var(--mono); font-size:.64rem; letter-spacing:.05em; color:var(--ink); }
  .libadd input[type=text]::placeholder { color:var(--muted); }
  .libadd input[type=text]:focus { outline:0; }
  .libadd button, .libdrop { font-family:var(--mono); font-size:.64rem; letter-spacing:.05em;
    border:0; background:none; padding:0; color:var(--muted); cursor:pointer;
    display:flex; align-items:center; }
  .libadd button:hover, .libdrop:hover { color:var(--accent); }
  .libdrop svg { display:block; }
  .libsay { font-family:var(--mono); font-size:.62rem; letter-spacing:.04em; color:var(--muted);
    margin:var(--s1) 0 0; min-height:1em; }
  .libsay.err { color:var(--oxide, var(--accent)); }
  .libgroup { font-family:var(--mono); font-size:.64rem; letter-spacing:.1em;
    text-transform:uppercase; color:var(--muted); margin:var(--s3) 0 .2rem; font-weight:400; }
  .libnote { color:var(--muted); font-size:.82rem; margin:0 0 var(--s1); }
  .libstart { color:var(--accent); font-size:.6em; vertical-align:middle; margin-left:.5em; }
  .libcount { font-family:var(--mono); font-size:.62rem; letter-spacing:.04em;
    color:var(--muted); margin:.2rem 0 var(--s2); }
  #notesbody { }
  #notesbody h4 { font-family:var(--mono); font-size:.64rem; letter-spacing:.1em;
    text-transform:uppercase; color:var(--muted); margin:var(--s2) 0 .3rem; }
  #notesbody ul { padding-left:1.1em; }
  #notesbody blockquote { border-left:2px solid var(--rule); margin:var(--s1) 0; padding-left:.8em;
    color:var(--muted); }
  #notesbody .qwhere { font-family:var(--mono); font-size:.62rem; }

  #panesplit { position:fixed; top:0; bottom:0; width:6px; z-index:66; cursor:col-resize;
    background:#35322d; left:var(--rail); display:none; touch-action:none; }
  body.paneopen #panesplit { display:block; left:calc(var(--rail) + var(--pane)); }
  #panesplit::before { content:""; position:absolute; inset:0 -7px; }
  #panesplit::after { content:""; position:absolute; top:50%; left:50%; width:2px; height:28px;
    margin:-14px 0 0 -1px; border-radius:2px; background:#5f594e; }

  /* an iframe is a replaced element, so width:auto takes its intrinsic 300x150 rather than
     stretching to the insets. The wrapper is what gets positioned. */
  #paperwrap { position:fixed; left:var(--rail); right:0; top:0; bottom:0; z-index:40;
    display:none; background:var(--panel); }
  body.paneopen #paperwrap { left:calc(var(--rail) + var(--pane)); }
  body.paperview #paperwrap { display:block; }
  #paperwrap iframe { width:100%; height:100%; border:0; display:block; }
  body.dragpanel { cursor:col-resize; user-select:none; }
  body.dragpanel iframe { pointer-events:none; }

  /* the tag and date rail is not worth the width while the pane is open. Three tracks are
     kept and the first zeroed rather than dropped: .topbar .head sits in column 2 explicitly,
     so two tracks would land the title in the sources rail. */
  @container page (min-width:70.01rem) {
    body.paneopen .shell > aside.side:not(.right) { visibility:hidden; }
    body.paneopen .shell, body.paneopen .topbar .inner {
      grid-template-columns:0 minmax(0,1fr) 11rem; }
  }
  @container page (max-width:70rem) {
    body.paneopen .shell > aside.side:not(.right) { display:none; }
  }
  /* The pane narrows the page without narrowing the window, so the stacking point has to be
     the space actually left rather than the viewport. The container sits inside body so the
     fixed rail is not caught by its containment. */
  #page { container-type:inline-size; container-name:page; }

  .libopen { font-family:var(--mono); font-size:.66rem; letter-spacing:.06em;
    color:var(--accent); text-decoration:none; }
  .libopen:hover { text-decoration:underline; }
  .liberr { color:var(--oxide, var(--accent)); }
  .filterhead { display:flex; align-items:baseline; justify-content:space-between; gap:var(--s2); }
  /* a scratch page is reachable but unlisted, so the sidebar is the only way in */
  .scratchlink { display:block; font-family:var(--mono); font-size:.72rem; letter-spacing:.04em;
    color:var(--muted); text-decoration:none; padding-bottom:var(--s2);
    border-bottom:1px solid var(--rule); margin-bottom:var(--s1); }
  .scratchlink:hover { color:var(--accent); }
  .filters { display:flex; flex-direction:column; gap:.2rem; }
  .main { min-width:0; display:flex; flex-direction:column; gap:var(--s4); }
  /* the measure is on the text an entry holds, so its footer can run wider */
  .entry > .text, .empty { max-width:var(--measure); }

  .label { font-family:var(--mono); font-size:.68rem; text-transform:uppercase;
    letter-spacing:.11em; color:var(--muted); margin:0; }
  .tag { font-family:var(--mono); font-size:.72rem; letter-spacing:.03em; color:var(--muted);
    background:none; border:0; border-left:2px solid transparent; border-radius:0;
    padding:.2em .5em; cursor:pointer; display:flex; align-items:baseline; gap:var(--s2);
    justify-content:space-between; text-align:left; width:100%; }
  .tag:hover { color:var(--ink); background:var(--panel); }
  .tag[aria-pressed="true"] { color:var(--accent); border-left-color:var(--accent);
    background:var(--panel); }
  .tag .n { font-size:.9em; opacity:.6; }
  .tag.sm { display:inline-flex; width:auto; border:1px solid var(--rule); border-radius:2px;
    font-size:.66rem; padding:.18em .5em; background:var(--chip-bg); }
  .tag.sm[aria-pressed="true"] { border-color:var(--accent); background:var(--sel); }
  .ref { font-family:var(--mono); font-size:.66rem; color:var(--muted);
    border:1px dashed var(--rule); border-radius:2px; padding:.18em .5em; text-decoration:none; }
  a.ref:hover { color:var(--accent); border-color:var(--accent); }
  #reset { font-family:var(--mono); font-size:.68rem; color:var(--accent); background:none;
    border:0; cursor:pointer; padding:0; display:none; }
  #reset.on { display:inline; }

  .entry, .entry > .text { display:flex; flex-direction:column; gap:var(--s2); }
  .entry { scroll-margin-top:7rem;
    padding-bottom:var(--s3); border-bottom:2px inset var(--rule); }
  h2.etitle { font-size:1.4rem; line-height:1.25; margin:0; font-weight:600; letter-spacing:-.01em; }
  h2.etitle a.anchor { color:inherit; text-decoration:none; }
  h2.etitle a.anchor:hover { color:var(--accent); }
  /* a heading inside a body is a section caption, not a second title */
  .entry h2:not(.etitle) { font-family:var(--mono); font-size:.7rem; font-weight:600;
    text-transform:uppercase; letter-spacing:.11em; color:var(--muted); margin:var(--s2) 0 0; }
  .entry ul { margin:0; padding-left:1.15rem; }
  .entry li { margin-bottom:var(--s1); }
  .entry li:last-child { margin-bottom:0; }
  .tags { display:flex; flex-wrap:wrap; gap:var(--s1); margin:0; }
  .tochead { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:var(--s3);
    align-items:baseline; padding:0 .3rem .35rem; }
  .sortbtn { font-family:var(--mono); font-size:.68rem; text-transform:uppercase;
    letter-spacing:.11em; color:var(--muted); background:none; border:0; cursor:pointer;
    padding:0; display:inline-flex; gap:.4em; align-items:baseline; }
  .sortbtn:hover { color:var(--accent); }
  .sortbtn .arrow { font-size:.8em; }
  .toc { display:flex; flex-direction:column; border-top:1px solid var(--rule); }
  .tocrow { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:var(--s3);
    align-items:baseline; padding:.55rem .3rem; text-decoration:none;
    color:var(--ink); border-bottom:1px solid var(--rule); }
  .tocrow:visited { color:var(--ink); }
  .tocrow:hover { background:var(--panel); }
  .tocrow:hover .toctitle { color:var(--accent); }
  .tocdate { font-family:var(--mono); font-size:.7rem; color:var(--muted);
    font-variant-numeric:tabular-nums; }
  /* a title that still will not fit is clipped rather than wrapped; the row carries it
     in a title attribute */
  .toctitle { font-size:1rem; line-height:1.3; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; }
  /* overflow-x alone makes the equation scroll on both axes, and one a few pixels taller
     than its box then takes the wheel from the page, so the other axis is closed */
  math.eq { display:block; overflow-x:auto; overflow-y:hidden; margin:var(--s2) 0; font-size:1.05rem; }
  .eqn { display:flex; align-items:center; gap:var(--s2); }
  .eqn math.eq { flex:1; } .eqn .eqnum { color:var(--muted); font-size:.9rem; }
  .scroll { overflow-x:auto; background:var(--panel); border:1px solid var(--rule); border-radius:2px; }
  /* a figure is inline SVG from make-assets, drawn in currentColor and the --fig tokens */
  figure.fig { margin:0; background:var(--panel); border:1px solid var(--rule); border-radius:2px;
    padding:var(--s2); overflow-x:auto; }
  figure.fig svg { display:block; width:100%; height:auto; min-width:34rem; }
  figure.fig figcaption, figure.tbl figcaption, p.tblcap {
    font-size:.86rem; color:var(--muted); margin-top:var(--s1); line-height:1.45; }
  figure.fig img { display:block; max-width:100%; height:auto; }
  figure.tbl { margin:0; } figure.tbl figcaption { margin:0 0 var(--s1); }
  span.hi { color:var(--accent); font-weight:600; } span.lo { color:var(--oxide); }
  figure.tbl.compact th, figure.tbl.compact td { padding:.2em .6em; }
  .capnum { color:var(--ink); font-weight:600; }
  mark { background:var(--sel); color:inherit; padding:0 .15em; border-radius:2px; }
  pre.out { font-family:var(--mono); font-size:.76rem; line-height:1.5; background:var(--panel);
    border:1px solid var(--rule); border-radius:2px; padding:var(--s2); overflow-x:auto; margin:0; }
  a.xref, a.cite { text-decoration:none; }
  .refs { margin-top:var(--s3); font-size:.9rem; } .refs ol { padding-left:1.2em; }
  .refs li { margin-bottom:var(--s1); }
  .provisional { border:1px solid var(--amber); border-left-width:4px; border-radius:2px;
    padding:var(--s1) var(--s2); margin:var(--s2) 0; font-size:.9rem; color:var(--amber); }
  .provisional p, .provisional ul { margin:.2em 0; }
  .entry > .foot { display:flow-root; }
  .foot > a.ref { float:left; margin-right:var(--s1); line-height:1.5; }
  details.prov { font-size:.86rem; color:var(--muted); }
  details.prov summary { cursor:pointer; list-style:none; line-height:1.5; }
  details.prov summary::-webkit-details-marker { display:none; }
  details.prov summary .ref { display:inline-block; }
  details.prov summary:hover .ref, details.prov[open] summary .ref {
    color:var(--accent); border-color:var(--accent); }
  /* open, it is a footer across the text column and the sources rail, and covers the rail */
  details.prov[open] { margin-right:calc(-11rem - var(--s5)); }
  .provbody { clear:left; position:relative; z-index:1; background:var(--ground);
    margin-top:var(--s2); padding:var(--s2) 0; border-top:1px solid var(--rule);
    display:flex; flex-direction:column; gap:var(--s3); font-family:var(--mono); font-size:.72rem; }
  .provbody .stepname { color:var(--ink); font-weight:600; margin:0 0 var(--s1); }
  .provbody em { font-weight:400; color:var(--muted); }
  .provbody dl { display:grid; grid-template-columns:4rem minmax(0,1fr); gap:var(--s1) var(--s2); margin:0; }
  .provbody dt { text-transform:uppercase; letter-spacing:.06em; font-size:.68rem; font-weight:600; }
  .provbody dd { margin:0; display:flex; flex-wrap:wrap; gap:.3rem var(--s1); align-items:baseline; }
  .provbody .path { flex-basis:100%; display:flex; flex-wrap:wrap; gap:.2rem var(--s2); align-items:baseline; }
  .provbody .path code { font-size:1em; overflow-wrap:anywhere; }
  .provbody .path a { text-decoration:none; } .provbody .path a:hover { text-decoration:underline; }
  .provbody a.ref { color:var(--accent); }
  .provbody .via { font-size:.66rem; }
  .provbody ul { margin:0; padding-left:1.2em; font-family:var(--serif); font-size:.86rem; }
  .provbody li.bad { color:var(--oxide); }
  .fig svg { color:var(--ink); }
  .fig .grid { stroke:var(--rule); } .fig .base { stroke:var(--muted); stroke-opacity:.5; }
  .fig .band { fill:currentColor; fill-opacity:.07; }
  .fig .ref { stroke:var(--muted); stroke-dasharray:4 4; }
  .fig .tick, .fig .val { font-family:var(--mono); font-size:11px; fill:var(--muted); }
  .fig .val { fill:var(--ink); font-weight:500; }
  .fig .cat, .fig .axt, .fig .name, .fig .leg, .fig .note {
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
  .fig .cat { font-size:12.5px; fill:var(--ink); font-weight:500; }
  .fig .axt, .fig .note { font-size:11.5px; fill:var(--muted); }
  .fig .name { font-size:13px; fill:var(--muted); font-weight:500; }
  .fig .leg { font-size:12.5px; fill:var(--ink); font-weight:500; }
  .fig .greek { font-family:var(--serif); font-size:21px; fill:var(--ink); }
  .fig .s1 { fill:var(--fig1); stroke:var(--panel); stroke-width:1.5; }
  .fig .s2 { fill:var(--fig2); stroke:var(--panel); stroke-width:1.5; }
  .fig .l1, .fig .l2 { fill:none; stroke-width:2; stroke-opacity:.45; }
  .fig .l1, .fig .e1 { stroke:var(--fig1); } .fig .l2, .fig .e2 { stroke:var(--fig2); }
  .fig .e1, .fig .e2 { stroke-width:1.5; }
  /* the reversible-jump diagram: drawn wider than the rank panels, so its text runs larger */
  .fig svg.rj .leg { font-size:17px; } .fig svg.rj .name, .fig svg.rj .cat { font-size:17px; }
  .fig .tailfill { fill:var(--fig1); fill-opacity:.16; }
  .fig .tailline { fill:none; stroke:var(--fig1); stroke-width:2.5; }
  .fig .massline { stroke:var(--ok); stroke-width:3.5; } .fig .massdot { fill:var(--ok); stroke:var(--panel); stroke-width:2; }
  .fig .arrow { stroke:var(--ink); stroke-width:1.8; } .fig .arrowhead { fill:var(--ink); }
  table { border-collapse:collapse; font-family:var(--mono); font-size:.76rem;
    font-variant-numeric:tabular-nums; width:100%; }
  th, td { padding:.42em .75em; text-align:right; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  thead th { color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.06em;
    font-size:.68rem; border-bottom:1px solid var(--rule); }
  tbody tr + tr td { border-top:1px solid var(--rule); }
  td.hi { color:var(--accent); font-weight:600; } td.lo { color:var(--oxide); }
  table.prose td { white-space:normal; font-family:var(--serif); font-size:.92rem; }
  pre.code { font-family:var(--mono); font-size:.76rem; line-height:1.5; background:var(--panel);
    border:1px solid var(--rule); border-radius:2px; padding:var(--s2); overflow-x:auto; margin:0; }

  /* tags ride the date line, so they need to sit on the baseline with it */
  .label .tag.sm { margin-left:var(--s1); vertical-align:baseline; }

  .datehead { margin-top:var(--s2); }
  .sources { display:flex; flex-direction:column; gap:.2rem; }
  .src { font-family:var(--mono); font-size:.72rem; letter-spacing:.03em; color:var(--muted);
    text-decoration:none; padding:.2em .5em; border-left:2px solid transparent; }
  .src:hover { color:var(--accent); background:var(--panel); border-left-color:var(--rule); }
  .hidden { display:none !important; }
  .empty { color:var(--muted); font-style:italic; display:none; }
  .empty.on { display:block; }

  /* Below this the three rails stack. The page is built for a desktop reading width and
     is not otherwise tuned for small screens. */
  @container page (max-width:70rem) {
    .shell, .topbar .inner { grid-template-columns:minmax(0,1fr); gap:var(--s3); }
    .topbar .inner { gap:0; }
    .topbar .head { grid-column:1; }
    .shell { padding:var(--s3); }
    .side { position:static; border-bottom:1px solid var(--rule); padding-bottom:var(--s2); }
    details.prov[open] { margin-right:0; }
    .side.right { border-bottom:0; border-top:1px solid var(--rule); padding-top:var(--s2); }
    .filters, .sources { flex-direction:row; flex-wrap:wrap; }
    .tag { width:auto; border-left:0; border-bottom:2px solid transparent; }
    .tag[aria-pressed="true"] { border-left:0; border-bottom-color:var(--accent); }
  }
  /* the editor exists only where the server answers edit/, so a static copy never shows it */
  .editbtn { font-family:var(--mono); font-size:.66rem; color:var(--accent); background:none;
    border:1px solid var(--rule); border-radius:2px; padding:.18em .5em; cursor:pointer;
    margin-left:var(--s1); }
  .editbtn:hover { border-color:var(--accent); }
  dialog.editor { width:min(64rem, calc(100vw - 2rem)); height:calc(100vh - 3rem); max-height:none;
    padding:0; border:1px solid var(--rule); border-radius:2px; background:var(--panel);
    color:var(--ink); }
  dialog.editor::backdrop { background:rgba(0,0,0,.35); }
  dialog.editor form { display:flex; flex-direction:column; height:100%; margin:0; }
  .edhead { display:flex; align-items:center; gap:var(--s2); padding:var(--s1) var(--s2);
    border-bottom:1px solid var(--rule); font-family:var(--mono); font-size:.72rem; }
  .edhead .file { color:var(--muted); flex:1; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; }
  .edhead button { font-family:var(--mono); font-size:.72rem; padding:.3em .8em; cursor:pointer;
    border:1px solid var(--rule); border-radius:2px; background:var(--chip-bg); color:var(--ink); }
  .edhead button.save { border-color:var(--accent); color:var(--accent); font-weight:600; }
  dialog.editor textarea { flex:1; resize:none; border:0; outline:0; padding:var(--s2);
    background:var(--ground); color:var(--ink); font-family:var(--mono); font-size:.82rem;
    line-height:1.55; tab-size:2; }
  .edstatus { font-family:var(--mono); font-size:.72rem; color:var(--muted); padding:var(--s1) var(--s2);
    border-top:1px solid var(--rule); white-space:pre-wrap; max-height:12rem; overflow:auto; }
  .edstatus.err { color:var(--oxide); }
  @media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
</style>

$rail
<div id="page">
<header class="topbar">
  <div class="inner">
    <div class="head">
    <span class="wordmark"><a href="#/">$title</a></span>
    <div class="provenance">
      <span><b>Updated</b> $latest</span>
      <span><b>Pages</b> $n</span>
    </div>
    </div>
  </div>
</header>

<div class="shell">
  <aside class="side">
    $scratch
    <div class="filterhead">
      <p class="label">Tags</p>
      <button id="reset" type="button">clear</button>
    </div>
    <div class="filters">$filters</div>
    <p class="label datehead">Dates</p>
    <div class="filters">$dates</div>
  </aside>

  <main class="main">
    <p class="empty" id="empty">Nothing matches every filter selected.</p>
    <div id="tocwrap">
      <div class="tochead">
        <span class="label">Page</span>
        <button class="sortbtn" id="sortdate" type="button">
          Updated <span class="arrow" id="sortarrow">&darr;</span></button>
      </div>
      <nav class="toc" id="toc">$toc</nav>
    </div>
    $entries

  </main>

  <aside class="side right">
    <p class="label">Sources</p>
    <div class="sources">$sources</div>
  </aside>
</div>
</div>


<script>
(function () {
  var tags = new Set(), months = new Set(), only = null;
  var entries = [].slice.call(document.querySelectorAll(".entry[data-tags]"));
  var rows = [].slice.call(document.querySelectorAll(".tocrow"));
  var toc = document.getElementById("toc");
  var tocwrap = document.getElementById("tocwrap");
  var sortarrow = document.getElementById("sortarrow");
  var newestFirst = true;
  var reset = document.getElementById("reset");
  var empty = document.getElementById("empty");
  var suppress = false;

  function matches(el) {
    var t = (el.getAttribute("data-tags") || "").split("|"), ok = true;
    tags.forEach(function (x) { if (t.indexOf(x) === -1) ok = false; });
    if (months.size && !months.has(el.getAttribute("data-month"))) ok = false;
    return ok;
  }

  // the contents list is the landing view; an entry is reached by its own route
  function paint() {
    var shown = 0;
    tocwrap.classList.toggle("hidden", !!only);
    entries.forEach(function (el) {
      el.classList.toggle("hidden", el.id !== only);
    });
    if (!only) {
      rows.forEach(function (el) {
        var show = matches(el);
        if (show) shown++;
        el.classList.toggle("hidden", !show);
      });
    }
    empty.classList.toggle("on", !only && shown === 0);
    document.querySelectorAll("[data-tag]").forEach(function (b) {
      b.setAttribute("aria-pressed", tags.has(b.getAttribute("data-tag")) ? "true" : "false");
    });
    document.querySelectorAll("[data-date]").forEach(function (b) {
      b.setAttribute("aria-pressed", months.has(b.getAttribute("data-date")) ? "true" : "false");
    });
    reset.classList.toggle("on", tags.size > 0 || months.size > 0);
  }

  // #/ the contents, #/entry/<slug> one entry, and #/tag/a+b/date/2026-08 composing
  function sync() {
    var h = "#/";
    // Array.from, not slice.call: a Set has no length, so slice hands back nothing
    if (tags.size) h += "tag/" + encodeURIComponent(Array.from(tags).join("+")) + "/";
    if (months.size) h += "date/" + encodeURIComponent(Array.from(months).join("+")) + "/";
    h = h.replace(/\/$/, "") || "#/";
    if (location.hash !== h) { suppress = true; location.hash = h; }
  }

  function route() {
    if (suppress) { suppress = false; return; }
    var parts = (location.hash || "#/").replace(/^#\/?/, "").split("/").filter(Boolean);
    tags.clear(); months.clear(); only = null;

    if (parts[0] === "paper" && parts[1]) { return; }   // the paper panels own this route
    if (parts[0] === "entry" && parts[1]) {
      only = decodeURIComponent(parts[1]);
      paint();
      window.scrollTo(0, 0);
      return;
    }
    for (var i = 0; i + 1 < parts.length; i += 2) {
      var vals = decodeURIComponent(parts[i + 1]).split("+").filter(Boolean);
      if (parts[i] === "tag") vals.forEach(function (v) { tags.add(v); });
      if (parts[i] === "date") vals.forEach(function (v) { months.add(v); });
    }
    paint();
    window.scrollTo(0, 0);
  }

  document.addEventListener("click", function (ev) {
    // a reference to a figure, a table, an equation or a citation: the address bar holds the
    // route, so the jump is made here rather than by changing the hash
    var x = ev.target.closest ? ev.target.closest("[data-xref]") : null;
    if (x) {
      ev.preventDefault();
      var to = document.getElementById(x.getAttribute("data-xref"));
      if (to) to.scrollIntoView({ block: "center" });
      return;
    }
    var b = ev.target.closest ? ev.target.closest("[data-tag],[data-date]") : null;
    if (b) {
      only = null;
      var t = b.getAttribute("data-tag"), d = b.getAttribute("data-date");
      if (t) { tags.has(t) ? tags.delete(t) : tags.add(t); }
      if (d) { months.has(d) ? months.delete(d) : months.add(d); }
      paint();
      sync();
      return;
    }
    if (ev.target.id === "reset") { tags.clear(); months.clear(); only = null; paint(); sync(); }
    if (ev.target.closest && ev.target.closest("#sortdate")) {
      newestFirst = !newestFirst;
      sortarrow.innerHTML = newestFirst ? "&darr;" : "&uarr;";
      rows.slice().reverse().forEach(function (el) { toc.appendChild(el); });
      rows.reverse();
    }
  });

  window.addEventListener("hashchange", route);
  route();
})();
</script><script>
(function () {
  // served at any mount depth, so edit/ is resolved against the page, not the host root
  var base = location.pathname.replace(/index\.html$/, "");
  if (!/\/$/.test(base)) base += "/";
  var dlg, ta, fileEl, statusEl, slug = null, hash = null, clean = "";

  function status(msg, err) {
    statusEl.textContent = msg; statusEl.classList.toggle("err", !!err);
  }

  function make() {
    dlg = document.createElement("dialog");
    dlg.className = "editor";
    dlg.innerHTML = '<form method="dialog"><div class="edhead"><span class="file"></span>' +
      '<button type="button" class="save">save</button><button type="button" class="close">close</button></div>' +
      '<textarea spellcheck="true"></textarea><div class="edstatus"></div></form>';
    document.body.appendChild(dlg);
    ta = dlg.querySelector("textarea"); fileEl = dlg.querySelector(".file");
    statusEl = dlg.querySelector(".edstatus");
    dlg.querySelector(".save").addEventListener("click", save);
    dlg.querySelector(".close").addEventListener("click", close);
    dlg.addEventListener("cancel", function (ev) { ev.preventDefault(); close(); });
    ta.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") { ev.preventDefault(); save(); }
    });
    ta.addEventListener("input", function () {
      status(ta.value === clean ? "" : "unsaved changes", false);
    });
  }

  function close() {
    if (ta.value !== clean && !confirm("Discard unsaved changes?")) return;
    dlg.close();
  }

  function open(s) {
    if (!dlg) make();
    slug = s; ta.value = ""; fileEl.textContent = s; status("loading", false);
    dlg.showModal();
    fetch(base + "edit/" + encodeURIComponent(s), { cache: "no-store" })
      .then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (res) {
        if (!res[0]) throw new Error(res[1].error);
        fileEl.textContent = res[1].file; hash = res[1].hash;
        ta.value = clean = res[1].text; status("ctrl+s saves, esc closes", false);
        ta.focus(); ta.setSelectionRange(0, 0); ta.scrollTop = 0;
      })
      .catch(function (e) { status(String(e.message || e), true); });
  }

  function save() {
    if (!slug || hash === null) return;
    status("saving and rebuilding", false);
    var text = ta.value;
    fetch(base + "edit/" + encodeURIComponent(slug), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, hash: hash }) })
      .then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (res) {
        if (!res[0]) throw new Error(res[1].error);
        clean = text;
        location.reload();
      })
      .catch(function (e) { status("not saved: " + String(e.message || e), true); });
  }

  fetch(base + "edit/", { cache: "no-store" }).then(function (r) {
    if (!r.ok) return;
    document.querySelectorAll(".entry[id]").forEach(function (el) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "editbtn"; b.textContent = "edit";
      b.addEventListener("click", function () { open(el.id); });
      el.querySelector(".label").appendChild(b);
    });
  }).catch(function () {});
})();
</script>
<script>
(function () {
  /* Live reload. The server announces a new build id after every successful rebuild, so a
     page follows what is on disk without anyone refreshing. A static copy answers no such
     endpoint, and this gives up rather than retrying at it forever. */
  var base = location.pathname.replace(/[^/]*$/, "");
  var id = null, held = false, fails = 0, es = null;

  function editing() { return !!document.querySelector("dialog.editor[open]"); }

  function reload() {
    try { sessionStorage.setItem("sloop.scroll", String(window.scrollY)); } catch (e) {}
    location.reload();
  }

  function arrived(data) {
    if (id === null) { id = data; return; }   /* the id held at connect, not a change */
    if (data === id) return;
    id = data;
    if (editing()) { held = true; return; }   /* never discard what is being typed */
    reload();
  }

  /* take a held rebuild once the editor is out of the way */
  setInterval(function () { if (held && !editing()) { held = false; reload(); } }, 500);

  try {
    var y = sessionStorage.getItem("sloop.scroll");
    if (y !== null) {
      sessionStorage.removeItem("sloop.scroll");
      addEventListener("load", function () { window.scrollTo(0, parseFloat(y) || 0); });
    }
  } catch (e) {}

  try { es = new EventSource(base + "live"); } catch (e) { return; }
  es.onopen = function () { fails = 0; };
  es.onmessage = function (ev) { arrived(ev.data); };
  es.onerror = function () { if (++fails >= 3) es.close(); };
})();
</script>
<script>
(function () {
  /* The companion pane. Its contents are someone else's problem: this only opens, closes,
     sizes and focuses it. The frame has no src until first opened, so a reader who never
     opens it never starts whatever is on the other end. */
  var body = document.body, root = document.documentElement;
  var rail = document.getElementById("rail");
  if (!rail) return;
  var pane = document.getElementById("pane");
  var paper = document.getElementById("paper");
  var btn = document.getElementById("panebtn");
  var TERM = pane.dataset.term;

  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  /* the terminal frame is built once, the first time the pane is opened on the notebook
     view, and then kept: switching to a paper and back must not drop what it is attached to */
  var termFrame = null;
  function ensureTerm() {
    if (termFrame || !TERM) return;
    termFrame = document.createElement("iframe");
    termFrame.src = TERM;
    termFrame.title = "terminal";
    document.getElementById("termbody").appendChild(termFrame);
  }
  function xterm() {
    try {
      var d = termFrame && termFrame.contentDocument;
      return d && d.querySelector(".xterm-helper-textarea");
    } catch (e) { return null; }
  }
  function focusTerm() {
    if (body.classList.contains("libview")) return;
    var ta = xterm();
    if (ta) ta.focus();
    else if (termFrame) { try { termFrame.contentWindow.focus(); } catch (e) {} }
  }

  function setOpen(open, focus) {
    body.classList.toggle("paneopen", open);
    btn.setAttribute("aria-pressed", String(open));
    set("sloop.paneopen", open ? "1" : "0");
    if (!open) return;
    if (!body.classList.contains("libview")) {
      ensureTerm();
      if (focus !== false) setTimeout(focusTerm, 240);
    }
  }
  btn.addEventListener("click", function () { setOpen(!body.classList.contains("paneopen")); });
  document.addEventListener("keydown", function (ev) {
    if (!ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key !== "`" && ev.code !== "Backquote") return;
    setOpen(!body.classList.contains("paneopen")); ev.preventDefault();
  });

  /* typing belongs to the terminal whenever it is the thing reading keys, so this does
     nothing in the paper view and nothing while text is selected */
  document.addEventListener("mouseup", function (ev) {
    if (!body.classList.contains("paneopen") || !xterm()) return;
    if (document.querySelector("dialog.editor[open]")) return;
    var t = ev.target;
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable], .editbtn, dialog.editor, #rail, #pane, #panesplit")) return;
    var sel = document.getSelection();
    if (sel && !sel.isCollapsed) return;
    focusTerm();
  });

  /* ---- the views ----
     libview puts the library in the pane: the list of papers, or one paper's notes.
     paperview additionally puts that paper's pdf in the main area. Neither is a page. */
  function showPaper(key) {
    var src = document.getElementById("pn-" + key);
    if (!src || !src.dataset.pdf) return false;
    document.getElementById("notesbody").innerHTML = src.innerHTML;
    document.getElementById("notestitle").textContent = src.dataset.title || key;
    paper.src = "library/pdf/" + encodeURIComponent(key) + ".pdf";
    body.classList.add("libview", "paperview");
    set("sloop.libview", "1");
    setOpen(true, false);                 /* a paper arrives with its notes beside it */
    return true;
  }
  /* The library is a state of the pane, not a route: navigating the notebook must not close
     it, and closing it must not move the notebook. Only a paper is a route, because a paper
     takes over the main area. */
  var want = null;                        /* a doi waiting for the list to come up */
  var backTo = "#/";                      /* where the notebook was before a paper took over */

  function primeBox() {
    if (!want) return;
    var box = document.getElementById("libdoi"), say = document.getElementById("libsay");
    if (box) {
      box.value = want.doi;
      if (say) {
        say.className = "libsay";
        say.textContent = want.doi
          ? "no pdf for " + want.key + ". Its doi is in the box, or add the pdf."
          : "no pdf for " + want.key + ", and no doi on record.";
      }
      if (want.doi) box.focus();
    }
    want = null;
  }

  function setLib(open) {
    body.classList.toggle("libview", open);
    set("sloop.libview", open ? "1" : "0");
    if (open) { setOpen(true, false); primeBox(); }
    else if (body.classList.contains("paperview")) location.hash = backTo;
    if (!open && body.classList.contains("paneopen")) ensureTerm();
    reflect();
  }

  /* Two views, exactly one of them on: notebook puts the terminal in the pane, library puts
     the papers there. Scratch is not one of them, it only moves the notebook. */
  var navs = [].slice.call(rail.querySelectorAll(".navbtn"));
  var scratches = [].slice.call(rail.querySelectorAll(".scratchbtn"));
  function reflect() {
    var lib = body.classList.contains("libview");
    navs.forEach(function (b) {
      b.setAttribute("aria-checked", String((b.dataset.view === "library") === lib));
    });
    var h = location.hash || "";
    scratches.forEach(function (b) {
      b.setAttribute("aria-pressed", String(h === "#/entry/" + b.dataset.slug));
    });
  }

  function view() {
    var h = location.hash || "";
    var m = h.match(/^#\/paper\/(.+)$/);
    if (!m) backTo = h || "#/";           /* the last place the notebook was */
    if (m && showPaper(decodeURIComponent(m[1]))) { reflect(); return; }
    paper.removeAttribute("src");
    body.classList.remove("paperview");   /* the list comes back, not stale notes */
    if (m) {
      /* cited but not on disk: show the list with its doi ready to act on */
      var src = document.getElementById("pn-" + decodeURIComponent(m[1]));
      want = { key: decodeURIComponent(m[1]), doi: src ? (src.dataset.doi || "") : "" };
      location.hash = backTo;             /* the notebook stays where it was */
      setLib(true);
      return;
    }
    if (body.classList.contains("paneopen") && !body.classList.contains("libview")) ensureTerm();
    reflect();
  }

  navs.forEach(function (b) {
    b.addEventListener("click", function () {
      var lib = b.dataset.view === "library";
      if (lib === body.classList.contains("libview")) { setOpen(true); return; }
      setLib(lib);                        /* leaving the library also closes any paper */
      if (!lib) focusTerm();
    });
  });
  scratches.forEach(function (b) {
    b.addEventListener("click", function () {
      var to = "#/entry/" + b.dataset.slug;
      location.hash = location.hash === to ? "#/" : to;
    });
  });
  addEventListener("hashchange", view);

  /* ---- dragging the pane ---- */
  var on = false, grab = 0, req = 0, want = 0;
  var split = document.getElementById("panesplit");
  function railPx() { return parseFloat(getComputedStyle(root).getPropertyValue("--rail")) || 30; }
  function apply(px) {
    var max = Math.max(200, window.innerWidth - railPx() - 420);
    root.style.setProperty("--pane", Math.min(max, Math.max(200, px)) + "px");
  }
  var saved = parseFloat(get("sloop.pane"));
  if (isFinite(saved)) apply(saved);
  split.addEventListener("pointerdown", function (ev) {
    on = true; body.classList.add("dragpanel");
    grab = ev.clientX - split.getBoundingClientRect().left;
    /* xterm refits and resizes its pty on every width change, so it is frozen for the drag */
    if (termFrame) termFrame.style.width = termFrame.getBoundingClientRect().width + "px";
    split.setPointerCapture(ev.pointerId); ev.preventDefault();
  });
  split.addEventListener("pointermove", function (ev) {
    if (!on) return;
    want = ev.clientX - grab - railPx();
    if (req) return;
    req = requestAnimationFrame(function () { req = 0; apply(want); });
  });
  function stop() {
    if (!on) return;
    on = false;
    if (req) { cancelAnimationFrame(req); req = 0; }
    body.classList.remove("dragpanel");
    if (termFrame) termFrame.style.width = "";
    set("sloop.pane", String(parseFloat(getComputedStyle(root).getPropertyValue("--pane")) || 0));
  }
  /* every way a drag can end without the splitter hearing */
  split.addEventListener("pointerup", stop);
  split.addEventListener("pointercancel", stop);
  split.addEventListener("lostpointercapture", stop);
  addEventListener("pointerup", stop);
  addEventListener("blur", stop);
  addEventListener("pointermove", function (ev) { if (on && !ev.buttons) stop(); });
  document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); });

  document.getElementById("notesback").addEventListener("click", function () {
    location.hash = backTo;               /* the paper closes, the list stays */
  });

  if (get("sloop.paneopen") === "1") setOpen(true, false);
  if (get("sloop.libview") === "1") body.classList.add("libview");
  view();
})();
</script>
<script>
(function () {
  /* The library's import controls. They exist only where the server answers them, which is
     the same rule the editor follows, so a static copy shows nothing. */
  function wire() {
    var form = document.getElementById("libadd");
    if (!form || form.dataset.wired) return;
    form.dataset.wired = "1";
    var base = location.pathname.replace(/[^/]*$/, "");
    var doi = document.getElementById("libdoi");
    var say = document.getElementById("libsay");
    var file = document.getElementById("libpdf");

    function tell(msg, bad) { say.textContent = msg; say.className = "libsay" + (bad ? " err" : ""); }
    function post(url, body, type) {
      var h = type ? { "Content-Type": type } : {};
      return fetch(base + url, { method: "POST", headers: h, body: body })
        .then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); });
    }

    document.getElementById("libdoibtn").addEventListener("click", function () {
      var v = doi.value.trim();
      if (!v) return;
      tell("asking doi.org");
      post("library/doi", JSON.stringify({ doi: v }), "application/json")
        .then(function (res) {
          if (!res[0]) throw new Error(res[1].error);
          doi.value = "";
          tell(res[1].key + " " + res[1].note + ". Add its pdf to have it read.");
        })
        .catch(function (e) { tell(String(e.message || e), true); });
    });
    doi.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); document.getElementById("libdoibtn").click(); }
    });

    function send(f) {
      var key = f.name.replace(/\.pdf$/i, "").replace(/[^A-Za-z0-9_.-]+/g, "");
      if (!key) { tell("that filename has no usable key in it", true); return Promise.resolve(); }
      tell("uploading " + key);
      return post("library/pdf/" + encodeURIComponent(key), f, "application/pdf")
        .then(function (res) {
          if (!res[0]) throw new Error(res[1].error);
          tell(key + " is being read. It appears here when it is done.");
        })
        .catch(function (e) { tell(key + ": " + String(e.message || e), true); });
    }
    file.addEventListener("change", function () {
      var fs = [].slice.call(file.files);
      file.value = "";
      fs.reduce(function (p, f) { return p.then(function () { return send(f); }); }, Promise.resolve());
    });

    /* the whole page takes a drop, since the target is otherwise a small word */
    ["dragover", "drop"].forEach(function (t) {
      document.addEventListener(t, function (e) {
        if (!e.dataTransfer || [].indexOf.call(e.dataTransfer.types, "Files") < 0) return;
        e.preventDefault();
        if (t !== "drop") return;
        [].slice.call(e.dataTransfer.files)
          .filter(function (f) { return /\.pdf$/i.test(f.name); })
          .reduce(function (p, f) { return p.then(function () { return send(f); }); }, Promise.resolve());
      });
    });
  }
  fetch(location.pathname.replace(/[^/]*$/, "") + "edit/", { cache: "no-store" })
    .then(function (r) { if (r.ok) { wire(); addEventListener("hashchange", wire); } })
    .catch(function () {});
})();
</script>
"""
