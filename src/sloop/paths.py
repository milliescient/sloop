"""Where a notebook lives, and what it is called.

The builder used to sit inside the notebook it built, so it could take its own location as
the root. As a package it cannot, so the root is discovered the way other site generators
do it: an explicit --dir, else $NOTEBOOK_DIR, else the nearest ancestor of the working
directory holding a notebook.yml.
"""
import os
import re
from pathlib import Path

CONFIG_NAME = "notebook.yml"
ENV_NAME = "notebook.env"
ENV_DEFAULT = ENV_NAME + ".default"

DEFAULTS = {
    "title": "notebook",
    "repo": "",
    "branch": "",       # the branch repo links point at; read off the checkout when empty
    "pages": "pages",
    "out": "site",
    # the sidebar's reading list: rows of name, kind, url, note
    "sources": [],
    # what a page can depend on; deps.py has the fields
    "dependencies": {},
    # where a repository or a data directory is looked for when it gives no path of its own
    "checkouts": "..",
    "bibliography": "references.bib",
    # literal colours in a figure and the stylesheet variable each stands for
    "figure_colors": {},
    "serve": {"port": 8099, "edit": True, "panes": []},
    # a page shared at a revocable link: the share server's port. The public url is read
    # off the funnel that proxies that port; stating one here overrides that.
    "share": {"port": 8106, "url": ""},
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


def load_env(root):
    """Per-installation values: notebook.env, which is not tracked and is where a path that
    differs between machines belongs, then notebook.env.default, which is tracked and holds
    the layout a fresh clone has. The environment wins over both, and the first file to give
    a name wins over the second, so a clone builds and a machine can still differ.
    """
    for name_of in (ENV_NAME, ENV_DEFAULT):
        f = root / name_of
        for line in f.read_text().splitlines() if f.is_file() else []:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def expand(value, where):
    """${NAME} in the config, read from the environment. ${NAME:-default} is left alone: the
    shell expands that one, in the commands it runs."""
    if isinstance(value, str):
        def one(m):
            if m.group(1) not in os.environ:
                raise SystemExit(f"{where}: ${{{m.group(1)}}} is not set. "
                                 f"Put it in {ENV_NAME}, or in {ENV_DEFAULT} if every "
                                 f"clone wants it")
            return os.environ[m.group(1)]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", one, value)
    if isinstance(value, dict):
        return {k: expand(v, where) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, where) for v in value]
    return value


def load(explicit=None):
    """Return (root, conf) with every default filled in and paths resolved."""
    import yaml
    root = find_root(explicit)
    load_env(root)
    raw = expand(yaml.safe_load((root / CONFIG_NAME).read_text()) or {}, CONFIG_NAME)
    conf = {**DEFAULTS, **raw}
    conf["serve"] = {**DEFAULTS["serve"], **(raw.get("serve") or {})}
    conf["share"] = {**DEFAULTS["share"], **(raw.get("share") or {})}
    for key in ("pages", "out", "checkouts", "bibliography"):
        conf[key] = (root / conf[key]).resolve() if not Path(conf[key]).is_absolute() \
                    else Path(conf[key])
    # CHECKOUTS in the environment moves the default location for one machine
    if os.environ.get("CHECKOUTS"):
        conf["checkouts"] = Path(os.environ["CHECKOUTS"]).expanduser().resolve()
    return root, conf
