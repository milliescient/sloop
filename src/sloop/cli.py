"""The `sloop` command, aliased to `notebook`.

Every path comes from the notebook being built, not from where the builder is installed:

    sloop build                 # render the notebook into its out directory
    sloop serve                 # build, serve, and rebuild as pages change
    sloop assets [NAME]         # build a page's assets from its artifacts
    sloop artifacts NN          # build its artifacts. Hours. Refuses if they exist.
    sloop new NAME ["A title"]  # scaffold a page
    sloop init [DIR]            # start a notebook here
    sloop pack OUT.pyz          # pack the builder into one file for deployment

The notebook is the directory holding notebook.yml: --dir, else $NOTEBOOK_DIR, else the
nearest one at or above the working directory, so the commands work from inside a page.
`notebook` is an alias for this same command.
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


COMMANDS = ("build", "serve", "assets", "artifacts", "new", "init", "pack")


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
                        "markdown", "PyYAML"], check=True)
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

    g = sub.add_parser("assets", parents=[common], help="build a page's assets")
    g.add_argument("page", nargs="?", help="page name or prefix; omit for every page that makes assets")
    g.add_argument("--check", action="store_true", help="rebuild and compare, do not overwrite")
    g.add_argument("--force", action="store_true")

    g = sub.add_parser("artifacts", parents=[common], help="build a page's artifacts")
    g.add_argument("page", nargs="?")
    g.add_argument("--force", action="store_true", help="remake artifacts that already exist")
    g.add_argument("--accept-artifacts", action="store_true")

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
    if a.cmd in ("assets", "artifacts"):
        rest = ([a.page] if a.page else []) + [
            f"--{f}" for f in ("check", "force", "accept-artifacts")
            if getattr(a, f.replace("-", "_"), False)]
        builder.run_pages(rest, generate=a.cmd == "artifacts")
        return
    if a.cmd == "new":
        scaffold.new(conf["pages"], a.name, a.title)
        return

    site = Path(a.out).expanduser().resolve() if a.out else conf["out"]
    flags = ["--no-drafts"] if a.no_drafts else []
    if not a.no_build:
        builder.build(site, drafts=not a.no_drafts)
    server.serve(root, conf, port=a.port, site=site, edit=a.edit, flags=flags,
                 watching=not a.no_watch)
