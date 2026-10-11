"""The `sloop` command, aliased to `notebook`.

Every path comes from the notebook being built, not from where the builder is installed:

    sloop build                         # render the notebook into its out directory
    sloop serve                         # build, serve, and rebuild as pages change
    sloop status [PAGE]                 # each step's state. Runs checks, and no steps.
    sloop make PAGE [STEP]              # run what is stale, in order. A frozen step only by name.
    sloop freeze PAGE STEP              # fix a step as it stands, without running it
    sloop unfreeze PAGE STEP            # make it an ordinary step again
    sloop accept PAGE WHAT -m "why"     # say a changed input or version was meant
    sloop cite DOI                      # add a reference to the notebook's .bib file
    sloop new NAME ["A title"]          # scaffold a page
    sloop init [DIR]                    # start a notebook here
    sloop pack OUT.pyz                  # pack the builder into one file for deployment

The notebook is the directory holding notebook.yml: --dir, else $NOTEBOOK_DIR, else the
nearest one at or above the working directory, so the commands work from inside a page.
`notebook` is an alias for this same command.
"""
import argparse
import sys
from pathlib import Path

from . import build as builder
from . import lock as locker
from . import paths, scaffold, serve as server

SKELETON = """title: {title}
repo:            # https://github.com/you/{title}, for the per-page source links
branch: main

# pages: pages    # where the page directories live
# out: site       # where the built notebook is written
# checkouts: ..   # where a repository or a data directory is looked for by default
# bibliography: references.bib

# What a page can depend on. An entry with a version command is a tool, one with only a url
# is a repository checked out on disk, and one with neither is a data directory.
# dependencies:
#   sim:
#     url: https://github.com/you/sim
#     version: sim --version
#   R:
#     version: Rscript -e 'cat(as.character(getRversion()))'
#   data:
#     path: /data/study

# The sidebar's reading list.
# sources:
#   - name: a paper or a repository
#     url: https://example.org
serve:
  port: 8099
  edit: true      # serve the in-browser page editor on localhost
"""


def start(where):
    root = Path(where or ".").expanduser().resolve()
    conf = root / paths.CONFIG_NAME
    if conf.exists():
        raise SystemExit(f"{conf} already exists")
    root.mkdir(parents=True, exist_ok=True)
    (root / "pages").mkdir(exist_ok=True)
    conf.write_text(SKELETON.format(title=root.name))
    print(conf)
    print("  set title and repo, then: notebook new \"A title\" && notebook build")
    print("  the out directory is generated, so keep it out of version control")


COMMANDS = ("build", "serve", "status", "make", "freeze", "unfreeze", "accept", "cite",
            "new", "init", "pack")


def cite(conf, doi):
    """Ask doi.org for a paper's BibTeX entry and append it to the notebook's .bib file, as
    supplied, key included. The lookup happens here, so a build never needs the network."""
    import re
    import urllib.error
    import urllib.request
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip())
    req = urllib.request.Request("https://doi.org/" + doi, headers={"Accept": "application/x-bibtex"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            entry = r.read().decode("utf-8").strip()
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"could not look up {doi}: {e}")
    m = re.match(r"\s*@\w+\s*\{\s*([^,\s]+)\s*,", entry)
    if not m:
        raise SystemExit(f"doi.org did not return a BibTeX entry for {doi}")
    key, bib = m.group(1), conf["bibliography"]
    have = bib.read_text() if bib.exists() else ""
    if re.search(r"@\w+\s*\{\s*" + re.escape(key) + r"\s*,", have):
        raise SystemExit(f"{bib.name} already has an entry with the key {key}; it was left as it is")
    bib.write_text(have + ("" if not have or have.endswith("\n\n") else "\n" if have.endswith("\n") else "\n\n")
                   + entry + "\n")
    print(f"{key}    added to {bib.name}; cite it as [@{key}]")


def pack(out):
    """One executable file, so a deployment environment needs nothing but python3.

    The compiled YAML accelerator is dropped on purpose. The pure-Python fallback is what
    lets one file run on any python3, on any platform, with no wheel to match.
    """
    import shutil
    import subprocess
    import tempfile
    import zipapp

    pkg = Path(__file__).resolve().parent
    out = Path(out).expanduser().resolve()
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp) / "app"
        app.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--no-warn-conflicts", "--target", str(app),
                        "markdown", "PyYAML", "latex2mathml", "bibtexparser>=2"], check=True)
        # The math converter reads its symbol table from a file beside its code when it is
        # imported, which an archive cannot serve. So the table is read once here and written
        # into the module, and the packed copy imports with no file to find.
        import runpy
        symbols = app / "latex2mathml" / "symbols_parser.py"
        table = runpy.run_path(str(symbols))["SYMBOLS"]
        symbols.write_text("SYMBOLS = " + repr(table) + "\n\n\n"
                           "def convert_symbol(symbol):\n    return SYMBOLS.get(symbol, None)\n")
        (app / "latex2mathml" / "unimathsymbols.txt").unlink()
        shutil.copytree(pkg, app / pkg.name, ignore=shutil.ignore_patterns("__pycache__"))
        for so in app.rglob("*.so"):
            so.unlink()
        for cache in sorted(app.rglob("__pycache__"), reverse=True):
            shutil.rmtree(cache)
        (app / "__main__.py").write_text(f"from {pkg.name}.cli import main\n\nmain()\n")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(app, out, interpreter="/usr/bin/env python3",
                              compressed=True)
    out.chmod(0o755)
    print(f"  {out}  {out.stat().st_size // 1024} KB")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # SUPPRESS so a subcommand's unset --dir does not overwrite one given before it
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS,
                        help="the notebook directory (default: found from the cwd)")
    ap.add_argument("--dir", help="the notebook directory (default: found from the cwd)")
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("build", parents=[common], help="render the notebook")
    b.add_argument("--out", help="write the site here instead of the configured out")
    b.add_argument("--no-drafts", action="store_true", help="hold back draft pages")

    s = sub.add_parser("serve", parents=[common], help="build, then serve on localhost")
    s.add_argument("--out", help="serve from here instead of the configured out")
    s.add_argument("--port", type=int)
    s.add_argument("--edit", action="store_true", default=None)
    s.add_argument("--no-edit", dest="edit", action="store_false")
    s.add_argument("--no-drafts", action="store_true")
    s.add_argument("--no-build", action="store_true", help="serve what is already built")
    s.add_argument("--no-watch", action="store_true", help="do not rebuild on change")

    g = sub.add_parser("status", parents=[common], help="each step's state; runs no steps")
    g.add_argument("page", nargs="?", help="page name or prefix; omit for every page")
    g.add_argument("--rehash", action="store_true",
                   help="read every file again instead of trusting sizes and modification times")

    g = sub.add_parser("make", parents=[common], help="run a page's stale steps, in order")
    g.add_argument("page", nargs="?", help="page name or prefix; omit for every page")
    g.add_argument("step", nargs="?", help="run this step alone, frozen or not")
    g.add_argument("--check", action="store_true",
                   help="rerun and compare with the record, leaving the outputs as they were")
    g.add_argument("--force", action="store_true",
                   help="let a frozen step overwrite outputs that already exist")

    g = sub.add_parser("freeze", parents=[common], help="fix a step as it stands, without running it")
    g.add_argument("page")
    g.add_argument("step")
    g.add_argument("--unverified", metavar="WHY",
                   help="record the versions the page states without checking them, and why")

    g = sub.add_parser("unfreeze", parents=[common], help="make a frozen step an ordinary one again")
    g.add_argument("page")
    g.add_argument("step")

    g = sub.add_parser("accept", parents=[common], help="say a changed input or version was meant")
    g.add_argument("page")
    g.add_argument("what", help="a path as the page writes it, or a dependency's name")
    g.add_argument("-m", "--why", required=True, help="the reason, which is recorded")

    g = sub.add_parser("cite", parents=[common], help="add a reference by DOI")
    g.add_argument("doi")

    n = sub.add_parser("new", parents=[common], help="scaffold a page")
    n.add_argument("name", help="the page directory, e.g. starving-column")
    n.add_argument("title", nargs="*", help="the title; defaults to the name")

    i = sub.add_parser("init", help="start a notebook in a directory")
    i.add_argument("where", nargs="?")

    k = sub.add_parser("pack", help="pack the builder into one executable file")
    k.add_argument("out", nargs="?", default="sloop.pyz")

    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "build")                    # the bare command builds
    a = ap.parse_args(argv)
    if a.cmd == "init":
        start(a.where)
        return
    if a.cmd == "pack":
        pack(a.out)
        return

    root, conf = paths.load(getattr(a, "dir", None))
    builder.init(root, conf)

    if a.cmd == "build":
        out = Path(a.out).expanduser().resolve() if a.out else None
        builder.build(out, drafts=not a.no_drafts)
        return
    if a.cmd == "status":
        results = [locker.status(d, fresh=a.rehash) for d in locker.pages(a.page)]
        if not all(results):
            raise SystemExit(1)
        return
    if a.cmd == "make":
        if a.step:
            ok = locker.make(locker.one_page(a.page), a.step, force=a.force, check=a.check)
        else:
            ok = all([locker.make(d, force=a.force, check=a.check) for d in locker.pages(a.page)])
        if not ok:
            raise SystemExit("the record differs under --check" if a.check
                             else "not everything ran; what did not is unchanged")
        return
    if a.cmd == "freeze":
        locker.freeze(locker.one_page(a.page), a.step, unverified=a.unverified)
        return
    if a.cmd == "unfreeze":
        locker.unfreeze(locker.one_page(a.page), a.step)
        return
    if a.cmd == "accept":
        locker.accept(locker.one_page(a.page), a.what, a.why)
        return
    if a.cmd == "cite":
        cite(conf, a.doi)
        return
    if a.cmd == "new":
        scaffold.new(conf["pages"], a.name, a.title, conf.get("dependencies") or {})
        return

    site = Path(a.out).expanduser().resolve() if a.out else conf["out"]
    flags = ["--no-drafts"] if a.no_drafts else []
    if not a.no_build:
        builder.build(site, drafts=not a.no_drafts)
    server.serve(root, conf, port=a.port, site=site, edit=a.edit, flags=flags,
                 watching=not a.no_watch)
