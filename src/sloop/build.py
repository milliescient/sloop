#!/usr/bin/env python3
"""Build the notebook, a page's assets, or a page's artifacts.

Run from a notebook directory, which is any directory holding a notebook.yml. See cli.py
for the commands and paths.py for what the config holds.

Building the notebook reads assets only, so a clone builds with no checkout at all.

Each entry is a file whose header is `key: value` lines terminated by `---`, then a body.
A .md body is rendered as Markdown, a .html body is passed through. Markdown keeps raw
HTML, so a table that needs per-cell classes stays HTML inside a .md entry.
The header is YAML, terminated by `---`. Required: title, date, tags.

    refs:                          # checkouts the page rests on, verified before anything runs
      - url: https://github.com/you/sim
        commit: 2523662d3
        checkout: sim         # path under $CHECKOUTS; defaults to the url's last part
    artifacts:                     # analysis output, living where it was made
      make: bash make-artifacts.sh # hours; refuses if the paths are already there
      paths:                       # named <ref>/<path>, so nothing here is absolute
        - sim/sims/rho0n10/specimens
    assets:                        # tracked, and all the notebook build reads
      make: bash make-assets.sh    # minutes; hashes the artifacts it read
      paths:
        - data.out

Artifacts belong to whoever produced them and are far too large to track; assets belong to
the page and are committed with it, which is why a clone renders without any checkout.

The url is tracked rather than derived from a remote: a checkout can have several, and the
one that matters may not be `origin`. paleobuddy here is checked out from upstream while the
commit lives on a fork.

Entry numbers come from the filename prefix and are stable, because entries cite each
other by number. Display order is by date, newest first.

A page is a directory holding its `page.md`, the assets the render reads, and whatever its
`assets.make` and `artifacts.make` need beside them. Everything an `assets.make` prints after a line reading
`## values` is `key<TAB>value` pairs, and `{{key}}` anywhere in the page is replaced by the
value, so a number cannot drift from the computation behind it: rerun the build and the prose
follows. A `{{key}}` with no value stops the render, as does a declared asset that is missing.

Routes: #/ everything, #/tag/<tag> filtered, #/entry/<slug>.

Writes one self-contained page to site/index.html, which works at any mount path.
"""
import hashlib
import html
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from string import Template

import yaml

# init() fills these in from the notebook's config: the builder is installed once, and
# every path it touches comes from the directory it was pointed at.
ROOT = PAGES = SOURCES = SITE = CHECKOUTS = None
CONF, TITLE, REPO, BRANCH = {}, "notebook", "", "main"


def init(root, conf):
    global ROOT, PAGES, SOURCES, SITE, CHECKOUTS, CONF, TITLE, REPO, BRANCH
    ROOT, CONF = root, conf
    PAGES, SOURCES, SITE = conf["pages"], conf["sources"], conf["out"]
    CHECKOUTS = conf["checkouts"]
    TITLE, REPO, BRANCH = conf["title"], conf["repo"], conf["branch"]

META_KEYS = ("title", "date", "tags")
MONTHS = [None, "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def to_html(body, suffix):
    if suffix != ".md":
        return body
    try:
        import markdown
    except ModuleNotFoundError:
        raise SystemExit("a .md entry needs the markdown package; this interpreter "
                         f"({sys.executable}) does not have it")
    for line in body.split("\n"):
        if line.startswith("<math") and 'display="block"' not in line:
            raise SystemExit("inline <math> may not start a line: markdown treats it as "
                             f"block level and splits the paragraph.\n  {line[:70]}")
    return markdown.markdown(body, extensions=["tables", "attr_list", "fenced_code",
                                               "md_in_html"])


def stamp_text(iso):
    """2026-08-02T14:29:03-05:00 -> 2026-08-02 14:29. Seconds and offset are noise here."""
    return iso[:10] + " " + iso[11:16] if len(iso) >= 16 else iso[:10]


def last_activity():
    """When each page directory was last committed to.

    A page is a directory, so git already tracks one history per page. One walk of the log
    is cheaper than a call per page, and a page with no commit yet simply has none.
    """
    try:
        out = subprocess.run(["git", "log", "--format=C%cI", "--name-only", "--", PAGES.name],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    seen, when = {}, None
    for line in out.stdout.splitlines():
        if line.startswith("C"):
            when = line[1:]
        elif line.startswith("pages/") and when:
            seen.setdefault(line.split("/")[1], when)     # log is newest first
    return seen


def commit_link(url, rev):
    """A github URL plus a hex revision is a commit page; anything else is the repo."""
    if "github.com" in url and re.fullmatch(r"[0-9a-f]{7,40}", rev):
        return f"{url.rstrip('/')}/commit/{rev}"
    return url


def computation(page_dir):
    """The companion script, its captured output, and the values the entry interpolates.

    Nothing is executed here. Analyses read hundreds of MCMC logs and take minutes, while a
    a notebook build takes a tenth of a second, so `notebook assets` runs them and this reads it.
    """
    out = page_dir / "data.out"
    if not out.exists():
        return None, None, {}
    src = out
    shown, vals = "", {}
    if out.exists():
        text = out.read_text()
        head, sep, tail = text.partition("\n## values\n")
        shown = head.rstrip()
        for line in tail.splitlines():
            if "\t" in line:
                k, v = line.split("\t", 1)
                vals[k.strip()] = v.strip()
    return src.read_text(), shown, vals


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
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{path.parent.name}: header is not valid YAML\n  {e}")
    missing = [k for k in META_KEYS if k not in meta]
    if missing:
        raise SystemExit(f"{path.parent.name}: missing {', '.join(missing)}")
    meta["tags"] = [str(x).strip() for x in meta["tags"]] if isinstance(meta["tags"], list) \
        else [t.strip() for t in str(meta["tags"]).split(",") if t.strip()]
    meta["date"] = str(meta["date"])
    for r in meta.get("refs") or []:
        r["name"] = r["url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        r.setdefault("checkout", r["name"])       # where it sits under $CHECKOUTS
    meta["refs"] = meta.get("refs") or []
    for key in ("artifacts", "assets"):
        meta[key] = meta.get(key) or {}
        meta[key]["paths"] = meta[key].get("paths") or []
    for a in meta["assets"]["paths"]:
        if not (path.parent / a).exists():
            raise SystemExit(f"{path.parent.name}: declares asset {a}, which is not there")
    meta["body"] = to_html(body.strip(), path.suffix)
    meta["draft"] = bool(meta.get("draft", False))
    meta["slug"] = path.parent.name
    meta["num"] = path.parent.name.split("-")[0]
    meta["path"] = str(path)
    if not meta["tags"]:
        raise SystemExit(f"{path.parent.name}: needs at least one tag")
    return meta


def read_sources():
    # optional: a notebook that cites nothing simply has no sources file
    if not SOURCES.exists():
        return []
    rows = [l.split("\t") for l in SOURCES.read_text().strip().splitlines()[1:]]
    return [r + [""] * (4 - len(r)) for r in rows]


def sidebar_sources(rows):
    """What the sidebar lists: anything with a URL, minus the ref-only rows that exist just
    to resolve a citation. The namesake sits last however the file is ordered."""
    listed = [r for r in rows if r[2] and r[1] != "ref-only"]
    return sorted(listed, key=lambda r: r[1] == "namesake")


CHECKABLE = re.compile(r"^#\|\s*check\b", re.M)


def header(page_dir):
    page = next((p for p in page_dir.glob("page.*")), None)
    if page is None:
        return {}
    meta = yaml.safe_load(page.read_text().partition("\n---\n")[0]) or {}
    for r in meta.get("refs") or []:
        r["name"] = r["url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        r.setdefault("checkout", r["name"])       # where it sits under $CHECKOUTS
        if "commit" in r:
            r["commit"] = str(r["commit"])       # an all-digit short hash parses as an int
    return meta


def git(path, *args):
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def digest(path):
    """A stable hash of a file, or of every file under a directory."""
    h = hashlib.sha1()
    base = path if path.is_dir() else path.parent
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    for f in files:
        h.update(str(f.relative_to(base)).encode())
        with f.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()[:12]


def verify(page_dir, checkouts=True):
    """Check every ref, and hash what it declares.

    Returns (problems, notes, digests, paths). The paths reach the make script as SIMFBD and
    the like, resolved, so it never joins $CHECKOUTS to a checkout name itself.

    `checkouts` is what separates the two steps. Generating artifacts runs the checkout's own
    code, so it needs that checkout at the commit the page names. Building assets only reads
    artifacts already on disk, so what has to be verified there is the artifacts, not the
    working tree that happens to be beside them. A commit mismatch is a note in that case.
    """
    meta = header(page_dir)
    bad, notes, digs, paths = [], [], {}, {}
    where = {}
    for r in meta.get("refs") or []:
        co = CHECKOUTS / r["checkout"]
        where[r["name"]] = co
        paths[re.sub(r"[^A-Z0-9]", "_", r["name"].upper())] = str(co)
        if not co.is_dir():
            notes.append(f'{r["name"]}@{r["commit"]} not checked, no {co}')
            continue
        head = git(co, "rev-parse", "HEAD")
        if head is None:
            notes.append(f'{r["checkout"]} is not a git checkout')
            continue
        if not head.startswith(r["commit"]) and git(co, "rev-parse", r["commit"]) != head:
            (bad if checkouts else notes).append(f'{r["name"]} is at {head[:len(r["commit"])]}, page wants {r["commit"]}')
            continue
        remotes = (git(co, "remote", "-v") or "").replace(":", "/")
        want = r["url"].rstrip("/").removesuffix(".git").split("github.com")[-1].lstrip("/")
        if want and want not in remotes:
            (bad if checkouts else notes).append(f'{r["name"]} has no remote matching {r["url"]}')
            continue
        if git(co, "status", "--porcelain"):
            notes.append(f'{r["name"]} matches but its tree is dirty')

    # artifacts name a ref and a path inside it, so nothing here holds an absolute path
    for a in (meta.get("artifacts") or {}).get("paths") or []:
        name, _, rest = a.partition("/")
        if name not in where:
            bad.append(f"artifact {a} names {name}, which is not a ref")
            continue
        p = where[name] / rest
        if not p.exists():
            bad.append(f"artifact {a} is missing")
        else:
            digs[a] = digest(p)
    return bad, notes, digs, paths


def run(page_dir, check=False, generate=False, force=False, accept=False):
    meta = header(page_dir)
    cmd = (meta.get("artifacts" if generate else "assets") or {}).get("make")
    # the `#| check` marker lives in whatever script the make command names
    src = next((page_dir / w for w in (cmd or "").split() if (page_dir / w).is_file()), None)
    out = page_dir / "data.out"
    print(f"{page_dir.name} ... ", end="", flush=True)

    if generate:
        # artifacts that already exist are not remade by accident: another page may rest on
        # them, and a job may be writing them right now
        have = [a for a in (header(page_dir).get("artifacts") or {}).get("paths") or []
                if (CHECKOUTS / next((r["checkout"] for r in header(page_dir).get("refs") or []
                                      if r["name"] == a.split("/")[0]), "")
                    / a.partition("/")[2]).exists()]
        if have and not force:
            print("refused", flush=True)
            for a in have:
                sys.stderr.write(f"  {a} already exists\n")
            sys.stderr.write("  another page may rest on these, or a job may be writing them.\n"
                             "  pass --force to remake them anyway\n")
            return False
        bad, _, _, paths = verify(page_dir)
        if bad:
            print("refused")
            for line in bad:
                sys.stderr.write(f"  {line}\n")
            return False
        print(f"running `{cmd}`, the long step")
        env = dict(os.environ, CHECKOUTS=str(CHECKOUTS), PAGE=str(page_dir), **paths)
        r = subprocess.run(cmd, shell=True, env=env, cwd=page_dir)
        print("generated" if r.returncode == 0 else "failed")
        return r.returncode == 0

    if check and not (src and CHECKABLE.search(src.read_text())):
        print("skipped, no `#| check`")
        return True
    if check and not out.exists():
        print(f"no {out.name} to check against")
        return False

    # only the artifacts have to be the right ones here: this step reads them, it does not
    # run the checkout's code, so the working tree beside them is not what is at stake
    bad, notes, digs, paths = verify(page_dir, checkouts=False)
    # what IS at stake is that they are the artifacts the last run described. data.out
    # carries their digests, so a batch regenerated underneath the page is caught here.
    if out.exists():
        was = dict(l.split("\t", 1) for l in out.read_text().splitlines()
                   if l.startswith("artifact.") and "\t" in l)
        for k, v in sorted(digs.items()):
            old_v = (was.get("artifact." + k) or "").strip()
            if old_v and old_v != v:
                # deliberate is not the same as unnoticed: the change is always named, and
                # --accept-artifacts is how someone says they meant it
                line = f"{k} has changed since these numbers were written ({old_v} -> {v})"
                if accept:
                    notes.append(line + ", accepted")
                else:
                    bad.append(line)
    if bad:
        print("refused")
        for line in bad:
            sys.stderr.write(f"  {line}\n")
        sys.stderr.write("  the numbers would not be the ones this page rests on\n")
        return False

    env = dict(os.environ, CHECKOUTS=str(CHECKOUTS), PAGE=str(page_dir), **paths)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       env=env, cwd=page_dir)
    if r.returncode != 0:
        print("failed")
        sys.stderr.write(r.stderr[-2000:])
        return False

    # the artifacts a page read are recorded with its numbers, not asserted beside them
    text = r.stdout
    if digs:
        head, sep, tail = text.partition("\n## values\n")
        if not sep:
            head, sep, tail = text.rstrip("\n"), "\n## values\n", ""
        text = head + sep + tail + "".join(f"artifact.{k}\t{v}\n" for k, v in sorted(digs.items()))

    def trailing():
        for n in notes:
            print(f"    note: {n}")

    if check:
        if out.read_text() != text:
            print("DIFFERS")
            trailing()
            return False
        print("matches")
        trailing()
        return True

    out.write_text(text)
    n = sum(1 for l in text.partition("\n## values\n")[2].splitlines() if "\t" in l)
    print(f"{len(text.splitlines())} lines, {n} values -> {out.name}")
    trailing()
    return True



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
    for e in entries:
        # the timestamp orders, the date displays: two pages committed the same day
        # still sort in the order they were touched
        e["stamp"] = activity.get(e["slug"], e["date"])
        e["updated"] = stamp_text(e["stamp"])
    for e in entries:
        src, shown, vals = computation(Path(e["path"]).parent)
        e["script"], e["shown"] = src, shown
        for field in ("body",):
            for key in set(re.findall(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}", e[field])):
                if key not in vals:
                    raise SystemExit(f"{Path(e['path']).parent.name}: no value for {{{{{key}}}}}. "
                                     f"Run notebook assets, or check the page emits it.")
                e[field] = re.sub(r"\{\{\s*" + key + r"\s*\}\}", vals[key], e[field])
    recent = sorted(entries, key=lambda e: (e["stamp"], e["num"]), reverse=True)
    counts = Counter(t for e in entries for t in e["tags"])
    rows = read_sources()
    urls = {r[0]: r[2] for r in rows}

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
        pins_html = ""
        for r in e["refs"]:
            label = html.escape(f'{r["name"]}@{r["commit"]}')
            u = commit_link(r["url"], r["commit"])
            pins_html += (f'<a class="ref" href="{u}" rel="noreferrer">{label}</a>'
                          if u else f'<span class="ref">{label}</span>')
        pins_ = pins_html
        body += f"""
      <section class="entry" id="{e['slug']}" data-tags="{html.escape('|'.join(e['tags']))}"
               data-month="{e['date'][:7]}">
        <p class="label">{e['updated']} {tags}</p>
        <h2 class="etitle"><a class="anchor" href="#/entry/{e['slug']}">{html.escape(e['title'])}</a></h2>
        {e['body']}
        <p class="tags">{hist}{pins_}</p>
      </section>"""

    # a ref-only row is cited for its pin, not as a source: it belongs in the page footer
    ref_only = {r[2].rstrip("/") for r in rows if r[1] == "ref-only"}
    named = {r[2].rstrip("/"): r[0] for r in rows if r[2]}
    cited = {}
    for e in entries:
        for r in e["refs"]:
            u = r["url"].rstrip("/")
            if u in ref_only:
                continue
            cited.setdefault(u, named.get(u, r["name"]))
    listed = [(name, url) for url, name in cited.items()]
    for r in sidebar_sources(rows):                       # extras no page cites
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
    for p in held:
        print(f"  draft, not published: {p.parent.name}")
    print(f"{len(entries)} pages, {len(counts)} tags, "
          f"{len(sidebar_sources(rows))} sources listed of {len(rows)}")
    print(f"  newest: {recent[0]['num']} {recent[0]['title'][:52]} ({recent[0]['date']})")


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
:root { --ground:#17120E; --panel:#201913; --ink:#E9E0D2; --muted:#A08D77; --rule:#3A2E24;
      --accent:#E08A4A; --visited:#B9A0E0; --blue:#8C99FF;
      --ok:#5FB48C; --amber:#D2A05A; --oxide:#D77C6E; --chip-bg:#241C15; --sel:#3A2C1C; --fig1:#4F93E6; --fig2:#E06A36; }
  }
  :root[data-theme="dark"] { --ground:#17120E; --panel:#201913; --ink:#E9E0D2; --muted:#A08D77; --rule:#3A2E24;
      --accent:#E08A4A; --visited:#B9A0E0; --blue:#8C99FF;
      --ok:#5FB48C; --amber:#D2A05A; --oxide:#D77C6E; --chip-bg:#241C15; --sel:#3A2C1C; --fig1:#4F93E6; --fig2:#E06A36; }
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
  math.eq { display:block; overflow-x:auto; margin:var(--s2) 0; font-size:1.05rem; }
  .scroll { overflow-x:auto; background:var(--panel); border:1px solid var(--rule); border-radius:2px; }
  /* a figure is inline SVG from make-assets, drawn in currentColor and the --fig tokens */
  figure.fig { margin:0; background:var(--panel); border:1px solid var(--rule); border-radius:2px;
    padding:var(--s2); overflow-x:auto; }
  figure.fig svg { display:block; width:100%; height:auto; min-width:34rem; }
  figure.fig figcaption { font-size:.86rem; color:var(--muted); margin-top:var(--s1); line-height:1.45; }
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


def run_pages(args, generate=False):
    check = "--check" in args
    force = "--force" in args
    accept = "--accept-artifacts" in args
    args = [a for a in args if a not in ("--check", "--force", "--accept-artifacts")]
    want = args[0] if args else None

    key = "artifacts" if generate else "assets"
    dirs = sorted(d for d in PAGES.iterdir()
                  if d.is_dir() and (header(d).get(key) or {}).get("make")
                  and (want is None or d.name.startswith(want)))
    if not dirs:
        raise SystemExit(f"no page matches {want!r}" if want else f"no page makes {key}")

    if not all([run(d, check, generate, force, accept) for d in dirs]):
        raise SystemExit("output moved under --check; rerun without it to accept the new numbers"
                         if check else "a page failed; its cached output is unchanged")


