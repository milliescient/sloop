"""What a page can depend on, where it is on this machine, and what version it is at.

Everything is declared once, in notebook.yml:

    dependencies:
      simfbdr:                       # has a version command: a tool
        url: https://github.com/you/simfbdr
        version: simfbdr --version
      pbdb-families:                # a url and no version command: a repository checked out
        url: https://github.com/you/pbdb-families
      sbc:                          # neither: a data directory
        path: /data/sbc

The kind is read off the fields rather than declared, because the kinds are not exclusive: a
tool usually has a source url too. `sloop status` prints the kind it found, and an unknown
field is refused, so a misspelt `version` cannot quietly turn a tool into a repository.

`url` is where a thing comes from and is the same on every machine. `path` is where it sits on
this one, and is looked for in three places, first match winning: the environment variable
SLOOP_<NAME>, the entry's own `path`, and <checkouts>/<name>. A tool has no path: it is
whatever its version command finds.

A dependency can be renamed freely. A record made under its old name is read under the one it
has now, because a version identifies a checkout without the help of a name.

A version is compared as a string. For a repository it is the checkout's commit, and for a
tool it is the one line its version command prints, which can be a number, a commit, anything.

A tool whose version is the commit of its source can be provisioned:

      simfbdr:
        url: https://github.com/you/simfbdr
        version: simfbdr --version
        provision: worktree                       # check out the commit a step states
        provides: {PATH: bin, PYTHONPATH: src}    # and put these of its directories first

A step that states simfbdr@1616e09 is then run against a git worktree of that commit, made
from the tool's own checkout (found as a repository's is) and kept under <checkouts>/.sloop.
So two steps can state two commits of one tool, and neither moves the checkout you work in.
The version command is run with the same directories first, and still has to print the
commit the step states.
"""
import os
import re
import subprocess
from pathlib import Path

FIELDS = {"url", "version", "path", "provision", "provides"}

ROOT = CHECKOUTS = None
DEPS = {}


def init(root, conf):
    global ROOT, CHECKOUTS, DEPS
    ROOT, CHECKOUTS = root, conf["checkouts"]
    DEPS = read(conf.get("dependencies") or {}, "notebook.yml")
    _versions.clear()


def read(raw, where):
    """Entries as {name: {url, version, path, kind}}, refusing what cannot be meant."""
    if not isinstance(raw, dict):
        raise SystemExit(f"{where}: dependencies must be a mapping of name to fields")
    out = {}
    for name, d in raw.items():
        d = d or {}
        if isinstance(d, str):
            d = {"url": d}
        unknown = set(d) - FIELDS
        if unknown:
            raise SystemExit(f"{where}: dependency {name} has no field {sorted(unknown)[0]!r}; "
                             f"the fields are {', '.join(sorted(FIELDS))}")
        kind = "tool" if d.get("version") else "repository" if d.get("url") else "directory"
        provision, provides = d.get("provision"), d.get("provides") or {}
        if provision not in (None, "worktree"):
            raise SystemExit(f"{where}: {name} has provision: {provision}; it can be worktree")
        if provision and kind != "tool":
            raise SystemExit(f"{where}: {name} is provisioned, so it needs a version command "
                             f"that prints the commit of its source")
        if provides and not provision:
            raise SystemExit(f"{where}: {name} has provides, which says what a provisioned "
                             f"checkout puts first: add provision: worktree")
        if not isinstance(provides, dict):
            raise SystemExit(f"{where}: {name}: provides is a mapping of an environment "
                             f"variable to a directory of the checkout")
        if kind == "tool" and d.get("path") and not provision:
            raise SystemExit(f"{where}: {name} is a tool, which has no path: it is whatever "
                             f"its version command finds")
        out[str(name)] = {"url": (d.get("url") or "").strip(), "version": d.get("version"),
                          "path": d.get("path"), "kind": kind, "provision": provision,
                          "provides": {str(k): str(v) for k, v in provides.items()}}
    return out


def merged(page_deps, page):
    """The notebook's dependencies plus a page's own. A name never means two things."""
    extra = read(page_deps or {}, page)
    for name in extra:
        if name in DEPS:
            raise SystemExit(f"{page}: redefines the dependency {name}, which notebook.yml "
                             f"already declares")
    return {**DEPS, **extra}


def env_name(name):
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def locate(name, dep):
    """(path, which rule supplied it) for a repository or a data directory."""
    if dep["kind"] == "tool":
        return None, "a tool has no path"
    return checkout(name, dep)


def checkout(name, dep):
    """Where a dependency's checkout is, which for a provisioned tool is its source."""
    var = "SLOOP_" + env_name(name)
    if os.environ.get(var):
        return Path(os.environ[var]).expanduser().resolve(), f"from ${var}"
    if dep["path"]:
        p = Path(str(dep["path"])).expanduser()
        return (p if p.is_absolute() else ROOT / p).resolve(), "from path in notebook.yml"
    return (CHECKOUTS / name).resolve(), "the default location"


def git(path, *args):
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


_versions = {}


def provisioned(name, dep, pinned, create=False):
    """(the worktree of a provisioned tool at the commit a step states, problem). Without
    create, a worktree that is not there yet is (None, None)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", pinned):
        return None, f"{name}@{pinned} is not a commit, so there is nothing to check out"
    base, _ = checkout(name, dep)
    if git(base, "rev-parse", "--git-dir") is None:
        return None, f"{name}'s source is not on this machine: {base} is not a git checkout"
    full = git(base, "rev-parse", "--verify", "--quiet", pinned + "^{commit}")
    if full is None:
        return None, f"{base} has no commit {pinned}: fetch it"
    at = CHECKOUTS / ".sloop" / name / full[:12]
    if at.is_dir():
        if git(at, "rev-parse", "HEAD") != full:
            return None, f"{at} is not at {pinned}: remove it and sloop makes it again"
        if git(at, "status", "--porcelain", "--untracked-files=no"):
            return None, f"{at} has been edited. It is {name}@{pinned} only as sloop made it"
        return at, None
    if not create:
        return None, None
    at.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "-C", str(base), "worktree", "add", "--detach", str(at), full],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"could not check out {name}@{pinned}: {r.stderr.strip()[-200:]}"
    return at, None


def provided(name, dep, at, env=None):
    """What a provisioned checkout adds to an environment: its directories first on the
    variables it provides, and itself under the tool's name."""
    env = dict(os.environ if env is None else env)
    for var, rel in dep["provides"].items():
        if (at / rel).is_dir():
            env[var] = os.pathsep.join(filter(None, [str(at / rel), env.get(var)]))
    env[env_name(name)] = str(at)
    return env


def version(name, dep, at=None):
    """(version, problem). The version is None when it cannot be found here. With a
    provisioned checkout, it is what the version command prints with that checkout first."""
    key = (name, str(at)) if at else name
    if key in _versions:
        return _versions[key]
    if dep["kind"] == "tool":
        r = subprocess.run(dep["version"], shell=True, capture_output=True, text=True, cwd=ROOT,
                           env=provided(name, dep, at) if at else None)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            out = (None, f"its version command failed: {(r.stderr.strip() or 'no output')[-200:]}")
        elif len(lines) > 1:
            out = (None, f"its version command printed {len(lines)} lines; it must print one")
        else:
            out = (lines[0].strip(), None)
    elif dep["kind"] == "repository":
        where, _ = locate(name, dep)
        if not where.is_dir():
            out = (None, f"not on this machine: no {where}")
        else:
            head = git(where, "rev-parse", "HEAD")
            if head is None:
                out = (None, f"{where} is not a git checkout")
            else:
                dirty = bool(git(where, "status", "--porcelain", "--untracked-files=no"))
                out = (head + ("+uncommitted" if dirty else ""), None)
    else:
        out = (None, "a data directory has no version")
    _versions[key] = out
    return out


def uncommitted(observed):
    """A version that says its code had uncommitted changes, by either convention."""
    return observed.endswith("+uncommitted") or observed.endswith("-dirty")


def matches(dep, pinned, observed):
    """Whether what is installed is what the page states. A repository's pin may be a short
    hash; anything else is compared whole."""
    clean = re.sub(r"(\+uncommitted|-dirty)$", "", observed)
    if dep["kind"] == "repository":
        return clean.startswith(pinned) and len(pinned) >= 7
    # a tool that reports the commit it was built from may abbreviate it differently from the
    # page, so two hex strings match when one begins the other
    if all(re.fullmatch(r"[0-9a-f]{6,40}", v) for v in (clean, pinned)):
        return clean.startswith(pinned) or pinned.startswith(clean)
    return clean == pinned


def shown(dep, observed, pinned):
    """A repository's version at the length the page wrote it, so the two read alike."""
    if dep["kind"] != "repository":
        return observed
    clean = re.sub(r"\+uncommitted$", "", observed)
    return clean[:max(len(pinned), 7)] + observed[len(clean):]


def link(dep, ver):
    """The url behind a version shown on a page, or '' for plain text."""
    url = dep["url"]
    if not url:
        return ""
    if "{version}" in url:
        return url.replace("{version}", ver)
    clean = re.sub(r"(\+uncommitted|-dirty)$", "", ver)
    if "github.com" in url and re.fullmatch(r"[0-9a-f]{6,40}", clean):
        return f"{url.rstrip('/')}/commit/{clean}"
    return url


def environment(deps):
    """What a step's command receives: each located dependency under its own name."""
    env = {"CHECKOUTS": str(CHECKOUTS)}
    for name, dep in deps.items():
        where, _ = locate(name, dep)
        if where is not None:
            env[env_name(name)] = str(where)
    return env
