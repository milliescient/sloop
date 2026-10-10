"""The `notebook` command.

Every path comes from the notebook being built, not from where the builder is installed:

    notebook build                 # render the notebook into its out directory
    notebook serve                 # build, serve, and rebuild as pages change
    notebook assets [NN]           # build a page's assets from its artifacts
    notebook artifacts NN          # build its artifacts. Hours. Refuses if they exist.
    notebook new "A title"         # scaffold the next page
    notebook init [DIR]            # start a notebook here

The notebook is the directory holding notebook.yml: --dir, else $NOTEBOOK_DIR, else the
nearest one at or above the working directory, so the commands work from inside a page.
"""
import argparse
import sys
from pathlib import Path

from . import build as builder
from . import paths, scaffold, serve as server

SKELETON = """title: {title}
repo:            # https://github.com/you/{title}, for the per-page source links
branch: main

# pages: pages    # where the page directories live
# out: site       # where the built notebook is written
# sources: sources.tsv
# checkouts: ..   # where the repositories a page pins by commit are checked out
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


COMMANDS = ("build", "serve", "assets", "artifacts", "new", "init")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="notebook", description=__doc__,
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

    g = sub.add_parser("assets", parents=[common], help="build a page's assets")
    g.add_argument("page", nargs="?", help="page number, or all pages that make assets")
    g.add_argument("--check", action="store_true", help="rebuild and compare, do not overwrite")
    g.add_argument("--force", action="store_true")

    g = sub.add_parser("artifacts", parents=[common], help="build a page's artifacts")
    g.add_argument("page", nargs="?")
    g.add_argument("--force", action="store_true", help="remake artifacts that already exist")
    g.add_argument("--accept-artifacts", action="store_true")

    n = sub.add_parser("new", parents=[common], help="scaffold the next page")
    n.add_argument("title", nargs="+")

    i = sub.add_parser("init", help="start a notebook in a directory")
    i.add_argument("where", nargs="?")

    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "build")                    # the bare command builds
    a = ap.parse_args(argv)
    if a.cmd == "init":
        return start(a.where)

    root, conf = paths.load(getattr(a, "dir", None))
    builder.init(root, conf)

    if a.cmd == "build":
        out = Path(a.out).expanduser().resolve() if a.out else None
        return builder.build(out, drafts=not a.no_drafts)
    if a.cmd in ("assets", "artifacts"):
        rest = ([a.page] if a.page else []) + [
            f"--{f}" for f in ("check", "force", "accept-artifacts")
            if getattr(a, f.replace("-", "_"), False)]
        return builder.run_pages(rest, generate=a.cmd == "artifacts")
    if a.cmd == "new":
        return scaffold.new(conf["pages"], a.title)

    site = Path(a.out).expanduser().resolve() if a.out else conf["out"]
    flags = ["--no-drafts"] if a.no_drafts else []
    if not a.no_build:
        builder.build(site, drafts=not a.no_drafts)
    server.serve(root, conf, port=a.port, site=site, edit=a.edit, flags=flags,
                 watching=not a.no_watch)
