"""Where a notebook lives, and what it is called.

The builder used to sit inside the notebook it built, so it could take its own location as
the root. As a package it cannot, so the root is discovered the way other site generators
do it: an explicit --dir, else $NOTEBOOK_DIR, else the nearest ancestor of the working
directory holding a notebook.yml.
"""
import os
from pathlib import Path

CONFIG_NAME = "notebook.yml"

DEFAULTS = {
    "title": "notebook",
    "repo": "",
    "branch": "main",   # the branch the repo links point at
    "pages": "pages",
    "out": "site",
    "sources": "sources.tsv",
    # checkouts a page pins by commit; alongside the notebook by default, as before
    "checkouts": "..",
    "serve": {"port": 8099, "edit": True},
}


def find_root(explicit=None):
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / CONFIG_NAME).exists():
            raise SystemExit(f"no {CONFIG_NAME} in {root}")
        return root
    env = os.environ.get("NOTEBOOK_DIR")
    if env:
        return find_root(env)
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).exists():
            return d
    raise SystemExit(
        f"no {CONFIG_NAME} here or above {here}.\n"
        f"run from a notebook directory, pass --dir, or set NOTEBOOK_DIR"
    )


def load(explicit=None):
    """Return (root, conf) with every default filled in and paths resolved."""
    import yaml
    root = find_root(explicit)
    raw = yaml.safe_load((root / CONFIG_NAME).read_text()) or {}
    conf = {**DEFAULTS, **raw}
    conf["serve"] = {**DEFAULTS["serve"], **(raw.get("serve") or {})}
    for key in ("pages", "out", "sources", "checkouts"):
        conf[key] = (root / conf[key]).resolve() if not Path(conf[key]).is_absolute() \
                    else Path(conf[key])
    # CHECKOUTS in the environment still wins, so existing scripts keep working
    if os.environ.get("CHECKOUTS"):
        conf["checkouts"] = Path(os.environ["CHECKOUTS"]).expanduser().resolve()
    return root, conf
