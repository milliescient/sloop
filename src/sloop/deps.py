"""What a page can depend on, where it is on this machine, and what version it is at.

A dependency is one checkout, and it may carry tools. Both are declared in notebook.yml:

    dependencies:
      revbayes:                     # a checkout under <checkouts>, found by this name
        tools:
          rb: strings "$(command -v rb)" | sed 's/.*-g//'
      palaeoverse:                  # no checkout here, so the url is written down
        url: palaeoverse/palaeoverse
        tools: {palaeoverse: Rscript -e 'cat(...)'}
      divDyn:
        checkout: r-package         # the directory is not named after the dependency
      sbc:
        path: /data/sbc             # a plain directory, not a repository

A page states `dependency@commit` or `tool@version`, so one checkout can carry several tools
and a tool need not be named after the thing it was built from. A dependency that is only a
checkout, and that no path names, needs no entry at all: a used name with no entry is taken
to be a checkout of that name, and a name that resolves to no checkout is refused.

`url` is a whole url, and is rarely needed: a checkout already records where it came from on
its own `origin`, which is read and rewritten as a web address. A dependency with no checkout
falls back to the first remote that is a web address, which resolves it by name, so only one
whose owner no remote names has to write a url down. It may hold {version}, and then it is the
link itself.

Where a checkout sits is looked for in three places, first match winning: the environment
variable SLOOP_<NAME>, the entry's own `path`, and <checkouts>/<checkout>, which defaults to
the dependency's name.

A dependency can be renamed freely. A record made under its old name is read under the one it
has now, because a version identifies a checkout without the help of a name.

A version is compared as a string. For a dependency it is its checkout's commit, and for a
tool it is the one line its version command prints, which can be a number, a commit, anything.
A version that names a commit or a tag of the checkout links to that commit; one that names
neither links to the checkout itself.

A tool whose version is the commit of its source can be provisioned:

      simfbdr:
        tools: {simfbdr: simfbdr --version}
        provision: worktree                       # check out the commit a step states
        provides: {PATH: bin, PYTHONPATH: src}    # and put these of its directories first

A step that states simfbdr@1616e09 is then run against a git worktree of that commit, made
from the dependency's own checkout and kept under <checkouts>/.sloop. So two steps can state
two commits of one tool, and neither moves the checkout you work in. The version command is
run with the same directories first, and still has to print the commit the step states.
"""
import os
import re
import subprocess
from pathlib import Path

FIELDS = {"url", "path", "checkout", "tools", "provision", "provides"}

ROOT = CHECKOUTS = None
DEPS = {}
REMOTES = {}


def init(root, conf):
    global ROOT, CHECKOUTS, DEPS, REMOTES
    ROOT, CHECKOUTS = root, conf["checkouts"]
    DEPS = read(conf.get("dependencies") or {}, "notebook.yml")
    REMOTES = remotes(conf.get("remotes") or {}, "notebook.yml")
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
        tools = d.get("tools") or {}
        if not isinstance(tools, dict):
            raise SystemExit(f"{where}: {name}: tools is a mapping of a tool's name to the "
                             f"command that prints its version")
        # what the dependency's checkout is here. A plain directory is one that exists and is
        # not a repository; anything absent is taken to be a checkout, so a page still reads
        # on a machine that has not got it.
        at, _ = checkout(str(name), {"path": d.get("path"),
                                     "checkout": str(d.get("checkout") or name)})
        kind = ("tool" if tools else
                "directory" if at.is_dir() and git(at, "rev-parse", "--git-dir") is None
                else "repository")
        provision, provides = d.get("provision"), d.get("provides") or {}
        if provision not in (None, "worktree"):
            raise SystemExit(f"{where}: {name} has provision: {provision}; it can be worktree")
        if provision and not tools:
            raise SystemExit(f"{where}: {name} is provisioned, so it needs a tool whose version "
                             f"command prints the commit of its source")
        if provides and not provision:
            raise SystemExit(f"{where}: {name} has provides, which says what a provisioned "
                             f"checkout puts first: add provision: worktree")
        if not isinstance(provides, dict):
            raise SystemExit(f"{where}: {name}: provides is a mapping of an environment "
                             f"variable to a directory of the checkout")
        out[str(name)] = {"url": (d.get("url") or "").strip(),
                          "path": d.get("path"), "kind": kind, "provision": provision,
                          "checkout": str(d.get("checkout") or name),
                          "tools": {str(k): v for k, v in tools.items()},
                          "provides": {str(k): str(v) for k, v in provides.items()}}
    for name, d in out.items():
        for tool in d["tools"]:
            other = [n for n, e in out.items() if n != name and tool in e["tools"]]
            if other:
                raise SystemExit(f"{where}: {name} and {other[0]} both have a tool called "
                                 f"{tool}; a tool is named once")
    return out


def tool_of(name, deps=None):
    """(the dependency it belongs to, the command that prints its version), or None."""
    for dn, d in (DEPS if deps is None else deps).items():
        if name in d["tools"]:
            return dn, d["tools"][name]
    return None


def merged(page_deps, page):
    """The notebook's dependencies plus a page's own. A name never means two things."""
    extra = read(page_deps or {}, page)
    for name in extra:
        if name in DEPS:
            raise SystemExit(f"{page}: redefines the dependency {name}, which notebook.yml "
                             f"already declares")
    return {**DEPS, **extra}


def remotes(raw, where):
    """Where a stated commit is looked for when the checkout here does not have it, in the
    order given. A location holds {name} for the dependency's name and {url} for its own."""
    if isinstance(raw, list):
        raw = {str(e): str(e) for e in raw}
    if not isinstance(raw, dict):
        raise SystemExit(f"{where}: remotes must be a mapping of a name to a location")
    return {str(k): str(v) for k, v in raw.items()}


def remotes_for(name, dep):
    """(remote, location) for each remote that could supply a dependency, in order."""
    out = []
    raw = dep.get("url") or ""
    for at, template in REMOTES.items():
        # a url written in full needs no host supplied for it
        if "{url}" in template and "://" in raw and template.strip() != "{url}":
            continue
        where = template.replace("{name}", name).replace("{url}", raw)
        if where and "{" not in where:
            out.append((at, where))
    return out


def resolved(where):
    """A location as git itself will use it, so an abstract url that a machine maps with
    url.<base>.insteadOf is understood here too, and nothing names a machine in a page."""
    return git(ROOT, "ls-remote", "--get-url", str(where)) or str(where)


def supplies(where, sha):
    """Whether a remote already has a commit. A repository on this machine answers for
    certain; a place on this machine that is not one answers no; a url answers None, which
    only fetching settles."""
    at = resolved(where)
    p = Path(at).expanduser()
    if p.is_dir() and git(p, "rev-parse", "--git-dir") is not None:
        return git(p, "cat-file", "-e", sha + "^{commit}") is not None
    return None if ("://" in at or "@" in at) else False


def fetch_commit(base, name, dep, sha):
    """Bring a stated commit into a checkout from the first remote that has it. A remote
    serves a bare commit only where it is configured to, so each is also asked for its
    branches, which any remote answers."""
    spec = f"+refs/heads/*:refs/remotes/sloop/{name}/*"
    for at, where in remotes_for(name, dep):
        if supplies(where, sha) is False:
            continue
        # asking for one commit needs its whole object name, which a repository here can
        # expand from the abbreviation a page states and a url cannot
        full = git(Path(resolved(where)), "rev-parse", "--verify", "--quiet", sha + "^{commit}")
        for ref in filter(None, (full, spec)):
            tags = "--tags" if ref is spec else "--no-tags"
            r = subprocess.run(["git", "-C", str(base), "fetch", tags, "--quiet",
                                str(where), ref], capture_output=True, text=True)
            if r.returncode == 0 and git(base, "rev-parse", "--verify", "--quiet", sha + "^{commit}"):
                return at
    return None


def fetch_hint(name, dep, sha):
    """What sloop would do about a commit the checkout here does not have, said without
    fetching, because status never reaches the network."""
    tried = remotes_for(name, dep)
    for at, where in tried:
        if supplies(where, sha):
            return f"{at} has it, and sloop make fetches it from there"
    unknown = [at for at, where in tried if supplies(where, sha) is None]
    if unknown:
        return f"sloop make looks for it in {', '.join(unknown)}"
    return "fetch it"


def env_name(name):
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def locate(name, dep):
    """(path, which rule supplied it) for a repository or a data directory."""
    return checkout(name, dep)


def checkout(name, dep):
    """Where a dependency's checkout is, which for a provisioned tool is its source."""
    var = "SLOOP_" + env_name(name)
    if os.environ.get(var):
        return Path(os.environ[var]).expanduser().resolve(), f"from ${var}"
    if dep["path"]:
        p = Path(str(dep["path"])).expanduser()
        return (p if p.is_absolute() else ROOT / p).resolve(), "from path in notebook.yml"
    return (CHECKOUTS / dep["checkout"]).resolve(), "the default location"


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
    if full is None and create and fetch_commit(base, name, dep, pinned):
        full = git(base, "rev-parse", "--verify", "--quiet", pinned + "^{commit}")
    if full is None:
        return None, f"{base} has no commit {pinned}: {fetch_hint(name, dep, pinned)}"
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
    cmd = dep["tools"].get(name)
    if cmd:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=ROOT,
                           env=provided(name, dep, at) if at else None)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            out = (None, f"its version command failed: {(r.stderr.strip() or 'no output')[-200:]}")
        elif len(lines) > 1:
            out = (None, f"its version command printed {len(lines)} lines; it must print one")
        else:
            out = (lines[0].strip(), None)
    else:
        where, _ = locate(name, dep)
        if not where.is_dir():
            out = (None, f"not on this machine: no {where}")
        else:
            head = git(where, "rev-parse", "HEAD")
            if head is None:
                out = (None, f"{where} is not a git checkout, so it has no version")
            else:
                dirty = bool(git(where, "status", "--porcelain", "--untracked-files=no"))
                out = (head + ("+uncommitted" if dirty else ""), None)
    _versions[key] = out
    return out


def uncommitted(observed):
    """A version that says its code had uncommitted changes, by either convention."""
    return observed.endswith("+uncommitted") or observed.endswith("-dirty")


def matches(name, dep, pinned, observed):
    """Whether what is installed is what the page states. A commit pin may be a short hash;
    anything else is compared whole."""
    clean = re.sub(r"(\+uncommitted|-dirty)$", "", observed)
    if name not in dep["tools"]:
        return clean.startswith(pinned) and len(pinned) >= 7
    # a tool that reports the commit it was built from may abbreviate it differently from the
    # page, so two hex strings match when one begins the other
    if all(re.fullmatch(r"[0-9a-f]{6,40}", v) for v in (clean, pinned)):
        return clean.startswith(pinned) or pinned.startswith(clean)
    return clean == pinned


def implicit(name):
    """A dependency nothing declares: a checkout of that name and no tools."""
    return {"url": "", "path": None, "kind": "repository", "provision": None,
            "checkout": name, "tools": {}, "provides": {}}


def shown(name, dep, observed, pinned):
    """A commit at the length the page wrote it, so the two read alike."""
    if name in dep["tools"]:
        return observed
    clean = re.sub(r"\+uncommitted$", "", observed)
    return clean[:max(len(pinned), 7)] + observed[len(clean):]


_urls = {}


def web_url(raw):
    """A git remote's address as one a reader can open, so either form of it links."""
    if not raw:
        return ""
    ssh = re.fullmatch(r"(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?", raw)
    return f"https://{ssh.group(1)}/{ssh.group(2)}" if ssh else re.sub(r"\.git/?$", "", raw)


def origin_of(at):
    """Where a checkout came from, read off its own remote, so nothing has to be declared
    for anything that is checked out here."""
    return web_url(git(at, "remote", "get-url", "origin") if at and Path(at).is_dir() else None)


def origin_url(name, dep):
    """Where a dependency's checkout came from."""
    if name not in _urls:
        _urls[name] = origin_of(locate(name, dep)[0])
    return _urls[name]


def url_of(name, dep):
    """A dependency's url: the one it declares, else the one its checkout came from, else the
    first remote that is a web address, which names an owner and so can resolve it by name."""
    raw = (dep.get("url") or "").strip()
    if raw:
        return raw
    got = origin_url(name, dep)
    if got:
        return got
    for _, where in remotes_for(name, dep):
        got = web_url(where)
        if got.startswith(("http://", "https://")):
            return got
    return ""


def link(name, dep, ver):
    """The url behind a version shown on a page, or '' for plain text.

    A version that names a commit or a tag of the dependency's checkout links to that commit.
    One that names neither links to the checkout itself, and a dependency with no url at all
    is plain text.
    """
    raw = (dep.get("url") or "").strip()
    if "{version}" in raw:
        return raw.replace("{version}", ver)
    url = url_of(name, dep)
    if not url or "github.com" not in url:
        return url
    clean = re.sub(r"(\+uncommitted|-dirty)$", "", ver)
    at, _ = locate(name, dep)
    full = (git(at, "rev-parse", "--verify", "--quiet", clean + "^{commit}")
            if at and at.is_dir() else None)
    if full:
        return f"{url.rstrip('/')}/commit/{full}"
    if re.fullmatch(r"[0-9a-f]{6,40}", clean):
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
