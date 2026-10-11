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
import html
import re
import subprocess
from collections import Counter
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


def is_draft(path):
    """Read only the header. A draft must not block a build, so a broken one is left
    for parse() to report rather than raising here."""
    head, _, _ = path.read_text().partition("\n---\n")
    try:
        return bool((yaml.safe_load(head) or {}).get("draft", False))
    except yaml.YAMLError:
        return False


def parse(path):
    head, _, body = path.read_text().partition("\n---\n")
    name = path.parent.name
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{name}: header is not valid YAML\n  {e}")
    missing = [k for k in META_KEYS if k not in meta]
    if missing:
        raise SystemExit(f"{name}: missing {', '.join(missing)}")
    meta["tags"] = [str(x).strip() for x in meta["tags"]] if isinstance(meta["tags"], list) \
        else [t.strip() for t in str(meta["tags"]).split(",") if t.strip()]
    meta["date"] = str(meta["date"])
    try:
        page = R.Page(name, path.parent, R.read_values(path.parent), BIB, PALETTE)
        # a .html page is passed through, and still has its values filled
        meta["body"] = page.render(body.strip()) if path.suffix == ".md" else page.fill(body.strip())
    except R.Failure as e:
        raise SystemExit(f"{name}: {e}")
    meta["draft"] = bool(meta.get("draft", False))
    meta["slug"] = name
    meta["path"] = str(path)
    if not meta["tags"]:
        raise SystemExit(f"{name}: needs at least one tag")
    return meta


def provenance(page_dir):
    """(the versions a page's steps ran as links, the block a reader can open).

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

    seen, pins = list(plan.rests_on), "".join(use(u) for u in plan.rests_on)
    rows = ""
    for name in plan.order:
        rec = lock["steps"].get(name) or {}
        for u in rec.get("uses") or []:
            if u not in seen:
                seen.append(u)
                pins += use(u)
        # what a manifest beside a path says made it: shown and linked, never checked
        with_ = lambda p: "".join(
            f'<div class="made">made with {"".join(use(u) for u in us)}</div>'
            for us in [(rec.get("made_with") or {}).get(p)] if us)
        paths = lambda side: "".join(
            f'<div><code>{html.escape(p)}</code> <span class="hash">{html.escape(str(h))}</span></div>'
            + with_(p) for p, h in (rec.get(side) or {}).items())
        rows += (f'<tr><td>{html.escape(name)}{" <em>frozen</em>" if plan.steps[name]["frozen"] else ""}'
                 f'{"" if rec else " <em>no record</em>"}</td>'
                 f'<td>{"".join(use(u) for u in rec.get("uses") or [])}</td>'
                 f'<td>{paths("inputs")}</td><td>{paths("outputs")}</td></tr>')
    block = ('<table><thead><tr><th>step</th><th>ran</th><th>read</th><th>wrote</th></tr></thead>'
             f'<tbody>{rows}</tbody></table>') if rows else ""
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
    return pins, f'<details class="prov"><summary>provenance</summary>{block}</details>' if block else ""


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
        e["stamp"] = activity.get(e["slug"], e["date"])
        e["updated"] = stamp_text(e["stamp"])
        page_dir = Path(e["path"]).parent
        e["why"] = L.gate(page_dir)
        e["pins"], e["prov"] = provenance(page_dir)
        if e["why"]:
            unsettled[e["slug"]] = e["why"]
    # A page whose record does not hold is shown as such while it is being worked on, and is
    # never published: that build fails, rather than quietly including or leaving out the page.
    if unsettled and not drafts:
        lines = "".join(f"\n  {slug}: {w}" for slug, why in unsettled.items() for w in why)
        raise SystemExit("not published. These pages are provisional, and none is marked "
                         f"draft:{lines}")
    recent = sorted(entries, key=lambda e: (e["stamp"], e["slug"]), reverse=True)
    counts = Counter(t for e in entries for t in e["tags"])
    rows = read_sources()

    months = Counter(e["date"][:7] for e in entries)
    dates = "".join(
        f'<button class="tag" data-date="{m}" aria-pressed="false">'
        f'<span>{MONTHS[int(m[5:7])]} {m[:4]}</span><span class="n">{n}</span></button>'
        for m, n in sorted(months.items(), reverse=True))

    filters = "".join(
        f'<button class="tag" data-tag="{html.escape(t)}" aria-pressed="false">'
        f'<span>{html.escape(t)}</span><span class="n">{n}</span></button>'
        for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    toc = ""
    body = ""
    for e in recent:
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
        <p class="label">{e['updated']} {tags}</p>
        <h2 class="etitle"><a class="anchor" href="#/entry/{e['slug']}">{html.escape(e['title'])}</a></h2>
        {banner}
        {e['body']}
        <p class="tags">{hist}{e['pins']}</p>
        {e['prov']}
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
        title=TITLE, filters=filters, dates=dates, toc=toc, entries=body, sources=srcs,
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
  .topbar .inner { max-width:82rem; margin:0 auto; padding:var(--s2) var(--s3);
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
    max-width:82rem; margin:0 auto; padding:var(--s4) var(--s3) var(--s5); align-items:start; }
  .side { position:sticky; top:6.4rem; display:flex; flex-direction:column; gap:var(--s2);
    max-height:calc(100vh - 8rem); overflow-y:auto; }
  .filterhead { display:flex; align-items:baseline; justify-content:space-between; gap:var(--s2); }
  .filters { display:flex; flex-direction:column; gap:.2rem; }
  .main { min-width:0; display:flex; flex-direction:column; gap:var(--s4); }
  .entry, .empty { max-width:var(--measure); }

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

  .entry { display:flex; flex-direction:column; gap:var(--s2); scroll-margin-top:7rem;
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
  details.prov { margin-top:var(--s2); font-size:.86rem; color:var(--muted); }
  details.prov summary { cursor:pointer; font-family:var(--mono); font-size:.76rem; }
  details.prov table { margin-top:var(--s1); } details.prov td, details.prov th { text-align:left;
    white-space:normal; vertical-align:top; }
  details.prov .hash { color:var(--muted); } details.prov li.bad { color:var(--oxide); }
  details.prov ul { padding-left:1.2em; }
  details.prov .made { padding-left:1.2em; font-size:.76rem; } details.prov .made .ref { margin-left:.4em; }
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
  @media (max-width:70rem) {
    .shell, .topbar .inner { grid-template-columns:minmax(0,1fr); gap:var(--s3); }
    .topbar .inner { gap:0; }
    .topbar .head { grid-column:1; }
    .shell { padding:var(--s3); }
    .side { position:static; border-bottom:1px solid var(--rule); padding-bottom:var(--s2); }
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
"""
