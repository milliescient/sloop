"""A page's steps, the record of what each one ran, read and wrote, and the gate between them.

A page declares named steps in its header:

    steps:
      simulate:
        cmd: ./simulate.sh
        uses: simfbdr@2ac91ba          # runs this, which must be at this version
        frozen: true                  # runs only when named, never overwrites without --force
        outputs: [sbc/thin/specimens_thinned]
      summarize:
        cmd: ./summarize.R
        uses: R@4.5.3
        inputs:  [sbc/thin/specimens_thinned]
        outputs: [values.tsv, ranks.csv]
    checks:
      truth order:
        cmd: awk '$1=="true_vals_order:"{print $2}' $SBC/thin/manifest.yml
        expect: youngest_first

A path whose first part names a dependency is inside that checkout or data directory, and
anything else is relative to the page. Edges come from paths: a step that reads what another
writes depends on it, so there is no list of edges to disagree with the paths. A step's own
script, every word of `cmd` naming a file in the page directory, is an input without being
listed.

`page.lock` beside the page records, per step, the versions it ran, and the hash of every
path it read and wrote. Where such a path has a manifest.yml at or above it that states its
own `uses`, the lock copies that under `made_with`, so the page can show what a tool ran on
the step's behalf. It is written by sloop and read by a person in a diff. A step is

    missing      an output is not there
    moved        an output differs from what the step recorded writing
    stale        an input, a script or a stated version differs from what it recorded
    unrecorded   its outputs are there, and nothing says how they got there
    current      none of those

A stale step that is not frozen is simply rerun, whatever changed, and says which of its
inputs had changed. Only a frozen step marks a result as decided, so only what a frozen step
rests on is refused when it moves, until someone says they meant it: an input a frozen step
read, or the output of a frozen step that another step reads. `sloop accept` writes that down
with its reason, in the lock, where the next reader finds it. A version a step states is a
requirement, not a record, so a step whose stated version is not what is installed does not
run at all.

Work that is unfinished is not refused and not waved through. A page that reads the output of
a step with no record, or whose quick step ran on uncommitted code, is provisional: the lock
says why, and the page carries a banner. A page reaches anyone else only when it is shared,
which exports it as it stands, banner and all.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

from . import deps as D

LOCK = "page.lock"
STEP_FIELDS = {"cmd", "uses", "frozen", "inputs", "outputs"}
CHECK_FIELDS = {"cmd", "expect"}

ROOT = PAGES = None


def init(root, conf):
    global ROOT, PAGES, HASHER
    ROOT, PAGES = root, conf["pages"]
    HASHER = Hasher(root / ".sloop" / "hashes.json")


# ---- hashing ----

class Hasher:
    """digest(), with each file's own hash remembered by its size and modification time.

    A fit's output is tens of thousands of files, and watching results come in hashes it
    again on every refresh, so only files that are new or changed are read. The cache is
    local and can be deleted at any time. It trusts size and mtime, which is what makes it
    fast, so anything that writes a record on trust or publishes passes fresh=True.
    """

    def __init__(self, path):
        self.path, self.changed = path, False
        try:
            self.seen = json.loads(path.read_text())
        except (OSError, ValueError):
            self.seen = {}

    def of_file(self, f, fresh=False):
        st = f.stat()
        key = str(f)
        was = self.seen.get(key)
        if was and not fresh and was[0] == st.st_size and was[1] == st.st_mtime_ns:
            return was[2]
        h = hashlib.sha1()
        with f.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        self.seen[key] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
        self.changed = True
        return h.hexdigest()

    def digest(self, path, fresh=False):
        """A stable hash of a file, or of every file under a directory: SHA-1 over one line
        per file, its relative name and the SHA-1 of its bytes, files in sorted order, first
        12 hex digits. simfbdr's bin/manifest.py computes the same thing, on purpose, so a
        path hashes alike in a page's lock and a batch's manifest."""
        path = Path(path)
        base = path if path.is_dir() else path.parent
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        h = hashlib.sha1()
        for f in files:
            h.update(f"{f.relative_to(base)}\0{self.of_file(f, fresh)}\n".encode())
        return h.hexdigest()[:12]

    def save(self):
        if not self.changed:
            return
        live = {k: v for k, v in self.seen.items() if os.path.exists(k)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live))
        os.replace(tmp, self.path)
        self.changed = False


HASHER = None


def digest(path, fresh=False):
    return HASHER.digest(path, fresh)


# ---- what a page declares ----

def page_file(page_dir):
    for name in ("page.md", "page.html"):
        if (page_dir / name).exists():
            return page_dir / name
    return None


def header(page_dir):
    page = page_file(page_dir)
    if page is None:
        return {}
    try:
        return yaml.safe_load(page.read_text().partition("\n---\n")[0]) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{page_dir.name}: header is not valid YAML\n  {e}")


class Plan:
    """A page's steps and checks, validated, with their order."""

    def __init__(self, page_dir, strict=True):
        self.dir, self.name = page_dir, page_dir.name
        meta = header(page_dir)
        for old in ("refs", "artifacts", "assets"):
            if old in meta:
                raise SystemExit(f"{self.name}: `{old}:` is the old page format. A page now "
                                 f"declares `steps:`; see the README.")
        self.deps = D.merged(meta.get("dependencies"), self.name)
        self.steps, self.checks = {}, {}
        # A page with numbers typed by hand has no step to check anything, and can still say
        # what it rests on: `uses:` at the top of a header is shown and never enforced.
        stated_here = meta.get("uses") or []
        self.rests_on = [stated_here] if isinstance(stated_here, str) else [str(u) for u in stated_here]
        for u in self.rests_on:
            if u.partition("@")[0] not in self.deps or "@" not in u:
                raise SystemExit(f"{self.name}: uses {u}; write a dependency from notebook.yml "
                                 f"as name@version")
        for name, raw in (meta.get("steps") or {}).items():
            self.steps[str(name)] = self._step(str(name), raw or {}, strict)
        for name, raw in (meta.get("checks") or {}).items():
            raw = raw or {}
            if set(raw) - CHECK_FIELDS or "cmd" not in raw or "expect" not in raw:
                raise SystemExit(f"{self.name}: check {name!r} takes cmd and expect")
            self.checks[str(name)] = {"cmd": str(raw["cmd"]), "expect": str(raw["expect"]).strip()}
        self._structure()

    def _step(self, name, raw, strict):
        unknown = set(raw) - STEP_FIELDS
        if unknown:
            raise SystemExit(f"{self.name}: step {name} has no field {sorted(unknown)[0]!r}; "
                             f"the fields are {', '.join(sorted(STEP_FIELDS))}")
        if not raw.get("cmd"):
            raise SystemExit(f"{self.name}: step {name} has no cmd")
        uses = raw.get("uses") or []
        uses = [uses] if isinstance(uses, str) else list(uses)
        pins = []
        for u in uses:
            dep_name, at, ver = str(u).partition("@")
            dep = self.deps.get(dep_name)
            if dep is None or dep["kind"] == "directory":
                raise SystemExit(f"{self.name}: step {name} uses {dep_name}, which "
                                 + ("is a data directory and has no version" if dep
                                    else "is not a dependency in notebook.yml"))
            if not at or not ver:
                if not strict:
                    ver = ""
                else:
                    found, problem = D.version(dep_name, dep)
                    raise SystemExit(
                        f"{self.name}: step {name} uses {dep_name} without saying which version. "
                        + (f"Here it is at: {dep_name}@{D.shown(dep, found, found[:7])}" if found
                           else f"It could not be found here ({problem})."))
            pins.append((dep_name, ver))
        cmd = str(raw["cmd"])
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        scripts = []
        for w in words:
            rel = os.path.normpath(w)
            if not rel.startswith("..") and not os.path.isabs(w) and (self.dir / rel).is_file():
                scripts.append(rel)
        inputs = list(dict.fromkeys(scripts + [os.path.normpath(str(p)) for p in raw.get("inputs") or []]))
        return {"name": name, "cmd": cmd, "uses": pins, "frozen": bool(raw.get("frozen", False)),
                "inputs": inputs, "scripts": scripts,
                "outputs": [os.path.normpath(str(p)) for p in raw.get("outputs") or []]}

    def where(self, path):
        """(location on disk, the dependency it sits in or None for the page itself)."""
        first, _, rest = path.partition("/")
        dep = self.deps.get(first)
        if dep is None:
            return self.dir / path, None
        if dep["kind"] == "tool":
            raise SystemExit(f"{self.name}: {path} starts with {first}, which is a tool and "
                             f"has no path. Name a repository or a data directory.")
        root, _ = D.locate(first, dep)
        return (root / rest if rest else root), first

    def here(self, path):
        """Whether the dependency a path sits in is on this machine at all."""
        first = path.partition("/")[0]
        dep = self.deps.get(first)
        return dep is None or dep["kind"] == "tool" or D.locate(first, dep)[0].is_dir()

    def producer(self, path):
        """The step that writes a path, or writes a directory holding it, or part of it."""
        for s in self.steps.values():
            for o in s["outputs"]:
                if overlap(o, path):
                    return s["name"], o
        return None, None

    def _structure(self):
        outs = [(o, s["name"]) for s in self.steps.values() for o in s["outputs"]]
        for i, (a, sa) in enumerate(outs):
            self.where(a)
            for b, sb in outs[i + 1:]:
                if overlap(a, b):
                    raise SystemExit(f"{self.name}: steps {sa} and {sb} both write {a if a == b else a + ' and ' + b}")
        needs = {n: [] for n in self.steps}
        for s in self.steps.values():
            for p in s["inputs"]:
                self.where(p)
                by, _ = self.producer(p)
                if by and by != s["name"] and by not in needs[s["name"]]:
                    needs[s["name"]].append(by)
        self.needs = needs
        # in dependency order, the order written breaking ties, so a rerun runs alike
        done, order = set(), []
        while len(order) < len(needs):
            ready = [n for n in needs if n not in done and all(d in done for d in needs[n])]
            if not ready:
                left = [n for n in needs if n not in done]
                cycle = self._cycle(left)
                raise SystemExit(f"{self.name}: steps depend on each other in a cycle: "
                                 + " -> ".join(cycle))
            done.add(ready[0])
            order.append(ready[0])
        self.order = order

    def _cycle(self, left):
        path, at = [], left[0]
        while at not in path:
            path.append(at)
            at = next(d for d in self.needs[at] if d in left)
        return path[path.index(at):] + [at]


def overlap(a, b):
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


# ---- the record ----

def read_lock(page_dir):
    try:
        doc = yaml.safe_load((page_dir / LOCK).read_text()) or {}
    except OSError:
        doc = {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{page_dir.name}: {LOCK} is not valid YAML\n  {e}")
    steps, accepted = doc.get("steps") or {}, doc.get("accepted") or []
    return {"steps": steps, "checks": doc.get("checks") or {},
            "accepted": accepted, "provisional": doc.get("provisional") or []}


def write_lock(plan, lock):
    """Steps in the page's order and nothing dated, so the file changes only when a fact does."""
    doc = {"steps": {}}
    for name in plan.order:
        rec = lock["steps"].get(name)
        if rec:
            doc["steps"][name] = {k: rec[k] for k in ("frozen", "uses", "inputs", "outputs", "made_with") if rec.get(k)}
    if lock["checks"]:
        doc["checks"] = {n: lock["checks"][n] for n in plan.checks if n in lock["checks"]}
    if lock["provisional"]:
        doc["provisional"] = lock["provisional"]
    if lock["accepted"]:
        doc["accepted"] = lock["accepted"]
    body = ("# Written by sloop: what each step ran, read and wrote. Read it in a diff; do not edit it.\n"
            + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True,
                             width=1000))
    tmp = plan.dir / (LOCK + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, plan.dir / LOCK)


def stated(step):
    return [f"{n}@{v}" for n, v in step["uses"]]


def clean(use):
    return re.sub(r"(\+uncommitted|-dirty)$", "", use)


def renamed(ran, now):
    """Recorded uses under the names their dependencies have now. A version identifies a
    checkout on its own, so a dependency that was renamed needs no declaration to be read."""
    at = {}
    for u in now:
        at.setdefault(u.partition("@")[2], []).append(u)
    out = []
    for u in ran:
        one = at.get(u.partition("@")[2]) or []
        out.append(one[0] if len(one) == 1 else u)
    return out


def observe(plan, paths, fresh=False):
    """{path: digest} for the paths that are on disk."""
    out = {}
    for p in paths:
        at, _ = plan.where(p)
        if at.exists():
            out[p] = digest(at, fresh)
    return out


def inspect(plan, lock, fresh=False):
    """{step: (state, [why])}, from what is on disk against what the lock records."""
    out = {}
    for name in plan.order:
        s, rec = plan.steps[name], lock["steps"].get(name)
        away = sorted({p.partition("/")[0] for p in s["inputs"] + s["outputs"] if not plan.here(p)})
        if away:
            out[name] = ("not here", [f"{d} is not on this machine" for d in away])
            continue
        have_out = observe(plan, s["outputs"], fresh)
        gone = [p for p in s["outputs"] if p not in have_out]
        if gone:
            out[name] = ("missing", [f"{p} is not there" for p in gone])
            continue
        if not rec:
            out[name] = ("unrecorded", ["nothing records how its outputs were made"])
            continue
        was_out, was_in = rec.get("outputs") or {}, rec.get("inputs") or {}
        wrote_h = Counter(was_out.values())
        moved = [f"{p} is not what this step wrote ({was_out.get(p, 'unrecorded')} -> {h})"
                 for p, h in have_out.items() if was_out.get(p) != h and not wrote_h[h]]
        if moved:
            out[name] = ("moved", moved)
            continue
        have_in = observe(plan, s["inputs"], fresh)
        why = [f"{p} is not there" for p in s["inputs"] if p not in have_in]
        both = [p for p in s["inputs"] if p in have_in and p in was_in]
        why += [f"{p} has changed ({was_in[p]} -> {have_in[p]})" for p in both
                if was_in[p] != have_in[p]]
        # What a step reads is its inputs' content, so a path it reads under a new name is
        # unchanged. Only the paths that pair off by name are reported by name.
        now_p = [p for p in s["inputs"] if p in have_in and p not in both]
        was_p = [p for p in was_in if p not in both]
        surplus = Counter(have_in[p] for p in now_p) - Counter(was_in[p] for p in was_p)
        deficit = Counter(was_in[p] for p in was_p) - Counter(have_in[p] for p in now_p)
        for p in now_p:
            if surplus[have_in[p]]:
                surplus[have_in[p]] -= 1
                why.append(f"{p} is a new input")
        for p in was_p:
            if deficit[was_in[p]]:
                deficit[was_in[p]] -= 1
                why.append(f"{p} is no longer an input")
        if sorted(renamed([clean(u) for u in rec.get("uses") or []], stated(s))) != sorted(stated(s)):
            why.append(f"the page states {', '.join(stated(s)) or 'no versions'}, and it ran "
                       f"{', '.join(rec.get('uses') or []) or 'none'}")
        for p in s["inputs"]:
            by, o = plan.producer(p)
            wrote = ((lock["steps"].get(by) or {}).get("outputs") or {}).get(p) if by else None
            if by and by != name and o == p and wrote and was_in.get(p) and wrote != was_in[p]:
                why.append(f"{by} has written {p} again since this read it")
        out[name] = ("stale", why) if why else ("current", [])
    return out


def unsettled(plan, lock):
    """Why a page's numbers are not settled, recomputed from the lock alone."""
    why = []
    for name in plan.order:
        s, rec = plan.steps[name], lock["steps"].get(name)
        if not rec:
            continue
        for p in s["inputs"]:
            by, _ = plan.producer(p)
            if by and by != name and by not in lock["steps"]:
                why.append(f"{name} reads the output of {by}, which has not finished or has not been recorded")
        if not s["frozen"]:
            why += [f"{name} ran on uncommitted code, {u}" for u in rec.get("uses") or []
                    if D.uncommitted(u)]
    return list(dict.fromkeys(why))


def gate(page_dir):
    """What stands between a page and being published, from committed files alone.

    A build has no checkouts, so this reads the lock and the page's own directory and nothing
    else: the outputs that live with the page must be the ones the lock records, every step
    must have a record, the edges in the lock must agree, and every check must have passed.
    """
    plan = Plan(page_dir, strict=False)
    if not plan.steps and not plan.checks:
        return []
    if not (page_dir / LOCK).exists():
        return [f"there is no {LOCK}: run sloop make {plan.name}"]
    lock = read_lock(page_dir)
    why = list(lock["provisional"])
    for name in plan.order:
        s, rec = plan.steps[name], lock["steps"].get(name)
        if not rec:
            why.append(f"{name} has no record")
            continue
        for p in s["outputs"]:
            at, dep = plan.where(p)
            if dep is not None:
                continue
            if not at.exists():
                why.append(f"{p} is declared by {name} and is not there")
            elif digest(at, fresh=True) != (rec.get("outputs") or {}).get(p):
                why.append(f"{p} is not what {name} recorded writing: run sloop make {plan.name}")
        for p in s["inputs"]:
            by, o = plan.producer(p)
            wrote = ((lock["steps"].get(by) or {}).get("outputs") or {}).get(p) if by else None
            if by and by != name and o == p and wrote and (rec.get("inputs") or {}).get(p) not in (None, wrote):
                why.append(f"{name} read {p} before {by} last wrote it")
    for name, c in plan.checks.items():
        got = lock["checks"].get(name)
        if not got or got.get("cmd") != cmd_hash(c["cmd"]):
            why.append(f"check {name!r} has not been run as written")
        elif str(got.get("result")) != c["expect"]:
            why.append(f"check {name!r} failed: expected {c['expect']}, got {got.get('result')}")
    return list(dict.fromkeys(why))


def cmd_hash(cmd):
    return hashlib.sha1(cmd.encode()).hexdigest()[:12]


# ---- running ----

def environment(plan, step=None):
    """What a command receives. A step also gets each provisioned tool it states, first."""
    env = dict(os.environ, PAGE=str(plan.dir), **D.environment(plan.deps))
    for dep_name, pinned in (step["uses"] if step else []):
        dep = plan.deps[dep_name]
        at = D.provisioned(dep_name, dep, pinned)[0] if dep["provision"] and pinned else None
        if at:
            env = D.provided(dep_name, dep, at, env)
    return env


def check_uses(plan, step):
    """(refusals, the versions as observed). Called at the moment a step is about to run."""
    bad, ran = [], []
    for dep_name, pinned in step["uses"]:
        dep = plan.deps[dep_name]
        at = None
        if dep["provision"]:
            at, problem = D.provisioned(dep_name, dep, pinned, create=True)
            if at is None:
                bad.append(problem)
                continue
        found, problem = D.version(dep_name, dep, at)
        if found is None:
            bad.append(f"{dep_name}: {problem}")
            continue
        if at and not D.matches(dep, pinned, found):
            bad.append(f"{dep_name}@{pinned} is checked out at {at}, and its version command "
                       f"still prints {found}: what {dep_name} provides in notebook.yml does "
                       f"not put that checkout first")
            continue
        if not D.matches(dep, pinned, found):
            fix = ""
            if dep["kind"] == "repository":
                fix = f"\n      git -C {D.locate(dep_name, dep)[0]} checkout {pinned}"
            bad.append(f"{dep_name} is at {D.shown(dep, found, pinned)}, and the page states "
                       f"{dep_name}@{pinned}{fix}")
            continue
        if D.uncommitted(found) and step["frozen"]:
            bad.append(f"{dep_name} has uncommitted changes. A frozen step's result could "
                       f"never be traced to its code: commit first.")
            continue
        ran.append(f"{dep_name}@{D.shown(dep, found, pinned)}")
    return bad, ran


def accepted(lock, key, target, now):
    return any(a.get(key) == target and a.get("now") == now for a in lock["accepted"])


def check_inputs(plan, lock, step):
    """(refusals, notes). Only a frozen step is refused for an input that moved under it."""
    bad, notes = [], []
    rec = lock["steps"].get(step["name"]) or {}
    for p in step["inputs"]:
        at, dep = plan.where(p)
        if not at.exists():
            bad.append(f"{p} is not there")
            continue
        by, o = plan.producer(p)
        now = digest(at)
        if by and by != step["name"]:
            prec = lock["steps"].get(by)
            wrote = (prec or {}).get("outputs", {}).get(o)
            if (plan.steps[by]["frozen"] and prec and o == p and wrote != now
                    and now not in ((prec or {}).get("outputs") or {}).values()):
                bad.append(f"{p} is not what {by} recorded writing ({wrote} -> {now}). Rerun "
                           f"it by name, or: sloop accept {plan.name} {p} -m \"why\"")
        elif dep is not None:
            was = (rec.get("inputs") or {}).get(p)
            if was and was != now and not accepted(lock, "path", p, now):
                if step["frozen"]:
                    bad.append(f"{p} has changed since this step last read it ({was} -> {now}). "
                               f"If that was meant: sloop accept {plan.name} {p} -m \"why\"")
                else:
                    notes.append(f"{p} has changed since this step last read it ({was} -> {now})")
    return bad, notes


def run_step(plan, lock, name, force=False, check=False):
    """Run one step and record it. Returns True when it ran and, under check, matched."""
    step = plan.steps[name]
    print(f"{plan.name} {name} ... ", end="", flush=True)
    bad, ran = check_uses(plan, step)
    have = [p for p in step["outputs"] if plan.where(p)[0].exists()]
    if step["frozen"] and have and force and not check:
        print("", flush=True)
        confirm(f"remake the frozen step {plan.name} {name}, overwriting {len(have)} recorded output"
                + ("" if len(have) == 1 else "s"))
        print(f"{plan.name} {name} ... ", end="", flush=True)
    if step["frozen"] and have and not force and not check:
        bad += [f"{p} already exists" for p in have]
        bad.append("another page may rest on these, or a job may be writing them: pass "
                   "--force to remake them")
    refused, notes = check_inputs(plan, lock, step)
    bad += refused
    if bad:
        print("refused", flush=True)
        for line in bad:
            sys.stderr.write(f"  {line}\n")
        return False

    backup = None
    if check:
        local = [p for p in step["outputs"] if plan.where(p)[1] is None]
        if step["frozen"] or len(local) != len(step["outputs"]) or name not in lock["steps"]:
            print("not checkable: --check reruns a recorded step that is not frozen and "
                  "writes only into its page")
            return False
        backup = Path(tempfile.mkdtemp(prefix="sloop-check-"))
        for p in local:
            at = plan.dir / p
            if at.is_dir():
                shutil.copytree(at, backup / p)
            elif at.exists():
                (backup / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(at, backup / p)

    print(f"running `{step['cmd']}`", flush=True)
    for line in notes:
        print(f"  {line}")
    r = subprocess.run(step["cmd"], shell=True, env=environment(plan, step), cwd=plan.dir)
    ok = r.returncode == 0
    gone = [p for p in step["outputs"] if not plan.where(p)[0].exists()]
    if ok and gone:
        ok = False
        for p in gone:
            sys.stderr.write(f"  {name} did not write {p}\n")
    if ok and check:
        was = lock["steps"][name].get("outputs") or {}
        now = observe(plan, step["outputs"], fresh=True)
        same = now == was
        print(f"  {name}: " + ("matches" if same else "DIFFERS: "
              + ", ".join(p for p in now if now[p] != was.get(p))))
        ok = same
    if backup is not None:
        for p in step["outputs"]:
            at = plan.dir / p
            if at.is_dir():
                shutil.rmtree(at)
            elif at.exists():
                at.unlink()
            if (backup / p).is_dir():
                shutil.copytree(backup / p, at)
            elif (backup / p).exists():
                shutil.copy2(backup / p, at)
        shutil.rmtree(backup)
        return ok
    if not ok:
        print(f"  {name}: failed, and its record is unchanged")
        return False
    lock["steps"][name] = record(plan, step, ran, fresh=True)
    print(f"  {name}: done, {len(step['outputs'])} output" + ("" if len(step["outputs"]) == 1 else "s"))
    return True


def record(plan, step, uses, fresh=True):
    rec = {"uses": list(uses), "inputs": observe(plan, step["inputs"], fresh),
           "outputs": observe(plan, step["outputs"], fresh)}
    if step["frozen"]:
        rec["frozen"] = True
    with_ = made_with(plan, step)
    if with_:
        rec["made_with"] = with_
    return rec


MANIFEST = "manifest.yml"


def manifest_uses(at, root):
    """What the nearest manifest.yml at or above a path says made it, as name@version, or [].

    A tool that makes data can pick its own tools: simfbdr fits a run with the build of rb the
    run's config states, which need not be the one a page's own scripts would find. Its
    manifest says which it was, in the form a step's `uses` has, so a page can show it. A
    manifest with entries, each with its own `uses`, gives all of them.
    """
    d = at if at.is_dir() else at.parent
    while True:
        f = d / MANIFEST
        if f.is_file():
            try:
                doc = yaml.safe_load(f.read_text())
            except (OSError, yaml.YAMLError):
                return []
            doc = doc if isinstance(doc, dict) else {}
            found = doc.get("uses") or [u for e in doc.get("simulated") or [] if isinstance(e, dict)
                                        for u in e.get("uses") or []]
            found = [found] if isinstance(found, str) else found
            return list(dict.fromkeys(str(u) for u in found if re.fullmatch(r"[^@\s]+@\S+", str(u))))
        if d == root or d == d.parent:
            return []
        d = d.parent


def made_with(plan, step):
    """{path: [name@version]} for the paths a step reads and writes that a manifest covers.
    It is shown on the page and never checked: it is what ran there, not what is installed."""
    out = {}
    for p in dict.fromkeys(step["inputs"] + step["outputs"]):
        at, dep = plan.where(p)
        if at.exists():
            found = manifest_uses(at, D.locate(dep, plan.deps[dep])[0] if dep else plan.dir)
            if found:
                out[p] = found
    return out


def run_checks(plan, lock):
    """Each check is a line or two with a stated answer, so a person can read it in the page."""
    ok = True
    for name, c in plan.checks.items():
        r = subprocess.run(c["cmd"], shell=True, capture_output=True, text=True,
                           env=environment(plan), cwd=plan.dir)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 and not lines:
            got = f"the command failed: {r.stderr.strip()[-120:]}"
        elif len(lines) > 2:
            got = f"{len(lines)} lines of output; a check prints one or two"
        else:
            got = " ".join(l.strip() for l in lines)
        lock["checks"][name] = {"cmd": cmd_hash(c["cmd"]), "result": got}
        passed = got == c["expect"]
        ok = ok and passed
        print(f"  check {name}: " + ("pass" if passed else f"FAIL, expected {c['expect']!r}, got {got!r}"))
    for gone in [n for n in lock["checks"] if n not in plan.checks]:
        del lock["checks"][gone]
    return ok


def finish(plan, lock):
    for gone in [n for n in lock["steps"] if n not in plan.steps]:
        del lock["steps"][gone]
    lock["provisional"] = unsettled(plan, lock)
    write_lock(plan, lock)
    HASHER.save()


def thawed(plan, lock):
    """Steps the lock records as frozen and the page no longer marks frozen."""
    return [n for n in plan.order
            if (lock["steps"].get(n) or {}).get("frozen") and not plan.steps[n]["frozen"]]


def confirm(what):
    """Removing a protection is a person's decision, made at a terminal."""
    if not sys.stdin.isatty():
        raise SystemExit(f"{what}: this takes away a protection, so it asks a person at a "
                         f"terminal, and there is none here")
    if input(f"{what}? type yes: ").strip().lower() != "yes":
        raise SystemExit("not done")


def make(page_dir, only=None, force=False, check=False):
    plan = Plan(page_dir)
    if not plan.steps and not plan.checks:
        return True
    lock = read_lock(page_dir)
    gone = thawed(plan, lock)
    if gone:
        for n in gone:
            print(f"{plan.name} {n}: the lock records it as frozen, and the page no longer says "
                  f"so. Put frozen: true back, or: sloop unfreeze {plan.name} {n}")
        return False
    # a step frozen before the lock kept the mark is marked now
    for n in plan.order:
        if plan.steps[n]["frozen"] and n in lock["steps"]:
            lock["steps"][n]["frozen"] = True
    if only:
        if only not in plan.steps:
            raise SystemExit(f"{plan.name}: no step {only!r}; it has {', '.join(plan.order)}")
        ok = run_step(plan, lock, only, force=force, check=check)
    else:
        ok = True
        for name in plan.order:
            state, why = inspect(plan, lock)[name]
            step = plan.steps[name]
            if check:
                if not step["frozen"] and name in lock["steps"]:
                    ok = run_step(plan, lock, name, check=True) and ok
                continue
            if state == "current":
                # nothing to run, and the record still describes what is on disk, so what a
                # manifest there says made it can be read now
                with_ = made_with(plan, step)
                if with_:
                    lock["steps"][name]["made_with"] = with_
                else:
                    lock["steps"][name].pop("made_with", None)
                continue
            if state == "not here":
                print(f"{plan.name} {name}: {why[0]}, so it is not checkable here")
                continue
            if step["frozen"]:
                if state == "unrecorded":
                    print(f"{plan.name} {name}: frozen with no record yet. When it is done: "
                          f"sloop freeze {plan.name} {name}")
                elif state == "missing":
                    print(f"{plan.name} {name}: frozen and not yet run. It runs only when "
                          f"named: sloop make {plan.name} {name}")
                else:
                    ok = False
                    print(f"{plan.name} {name}: frozen and {state}")
                    for w in why:
                        print(f"    {w}")
                    print(f"    rerun it by name, or say the change was meant with sloop accept")
                continue
            if not run_step(plan, lock, name, force=force):
                ok = False
                break
    if check:
        HASHER.save()
        return ok
    ok = run_checks(plan, lock) and ok
    finish(plan, lock)
    for w in lock["provisional"]:
        print(f"  provisional: {w}")
    return ok


# ---- freezing, and saying a change was meant ----

def set_frozen(page_dir, name, on):
    """Add or remove `frozen: true` on one step, changing that line of the page and no other."""
    page = page_file(page_dir)
    head, sep, body = page.read_text().partition("\n---\n")
    lines = head.split("\n")
    try:
        top = next(i for i, l in enumerate(lines) if re.match(r"steps:\s*$", l))
        at = next(i for i in range(top + 1, len(lines))
                  if re.match(rf"\s+{re.escape(name)}:\s*$", lines[i]))
    except StopIteration:
        raise SystemExit(f"{page_dir.name}: could not find step {name} written as a block "
                         f"under steps:, so set `frozen` in the page by hand")
    indent = len(lines[at]) - len(lines[at].lstrip())
    end = at + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    block = range(at + 1, end)
    has = [i for i in block if re.match(r"\s+frozen:\s*(true|yes|on)\s*(#.*)?$", lines[i], re.I)]
    if on and not has:
        child = next((len(lines[i]) - len(lines[i].lstrip()) for i in block if lines[i].strip()),
                     indent + 2)
        cmd = next((i for i in block if re.match(r"\s+cmd:", lines[i])), at)
        lines.insert(cmd + 1, " " * child + "frozen: true")
    elif not on and has:
        del lines[has[0]]
    else:
        return False
    page.write_text("\n".join(lines) + sep + body)
    return True


def freeze(page_dir, name, unverified=None):
    """Fix a step as it stands, without running it.

    This is the one place sloop takes the author's word: it sees the outputs and the versions
    as they are now, not as they were when the work ran. So it is only for what cannot be
    cheaply rerun, and what sloop can rerun it reruns instead.
    """
    plan = Plan(page_dir)
    if name not in plan.steps:
        raise SystemExit(f"{plan.name}: no step {name!r}; it has {', '.join(plan.order)}")
    step, lock = plan.steps[name], read_lock(page_dir)
    gone = [p for p in step["outputs"] if not plan.where(p)[0].exists()]
    if gone:
        raise SystemExit(f"{plan.name} {name}: nothing to freeze, {gone[0]} is not there")
    if unverified:
        ran = stated(step)
    else:
        bad, ran = check_uses(plan, dict(step, frozen=True))
        if bad:
            for line in bad:
                sys.stderr.write(f"  {line}\n")
            raise SystemExit(f"{plan.name} {name}: not frozen")
    lock["steps"][name] = dict(record(plan, step, ran, fresh=True), frozen=True)
    if unverified:
        lock["accepted"].append({"step": name, "uses": list(ran), "why": unverified})
    if set_frozen(page_dir, name, True):
        print(f"  {page_file(page_dir).name}: frozen: true added to {name}")
        plan = Plan(page_dir)
    finish(plan, lock)
    print(f"{plan.name} {name}: frozen as it stands, {len(step['outputs'])} outputs recorded")


def freeze_page(page_dir):
    """Freeze every step, but only a page whose every step is current, so a page is never
    frozen around numbers that no longer follow from what it reads."""
    plan = Plan(page_dir)
    if not plan.steps:
        raise SystemExit(f"{plan.name}: no steps to freeze")
    lock = read_lock(page_dir)
    states = inspect(plan, lock, fresh=True)
    bad = [(n, s, why) for n, (s, why) in states.items() if s != "current"]
    if bad:
        for n, s, why in bad:
            sys.stderr.write(f"  {n}: {s}" + (f", {why[0]}" if why else "") + "\n")
        raise SystemExit(f"{plan.name}: not frozen. Every step has to be current: sloop make {plan.name}")
    todo = [n for n in plan.order if not plan.steps[n]["frozen"]]
    for n in todo:
        freeze(page_dir, n)
    print(f"{plan.name}: every step frozen" + ("" if todo else ", as it already was"))


def unfreeze(page_dir, name):
    plan = Plan(page_dir)
    if name not in plan.steps:
        raise SystemExit(f"{plan.name}: no step {name!r}; it has {', '.join(plan.order)}")
    lock = read_lock(page_dir)
    marked = (lock["steps"].get(name) or {}).get("frozen")
    if not plan.steps[name]["frozen"] and not marked:
        print(f"{plan.name} {name}: was not frozen")
        return
    confirm(f"unfreeze {plan.name} {name}")
    set_frozen(page_dir, name, False)
    if marked:
        del lock["steps"][name]["frozen"]
        finish(Plan(page_dir), lock)
    print(f"{plan.name} {name}: no longer frozen. Its record is kept, and it runs when stale.")


def accept(page_dir, target, why):
    """Write down that a change was meant.

    A path: every frozen step that recorded it takes its hash as it is now, and a step that
    is not frozen is allowed to read it again. A dependency's name: every frozen step that
    used it takes the version the page now states.
    """
    plan = Plan(page_dir)
    lock = read_lock(page_dir)
    if not why or not why.strip():
        raise SystemExit("sloop accept needs a reason: -m \"why the change was meant\"")
    confirm(f"accept the change to {target} on {plan.name}")
    if target in plan.deps and plan.deps[target]["kind"] != "directory":
        changed = None
        for name in plan.order:
            s, rec = plan.steps[name], lock["steps"].get(name)
            now = next((f"{n}@{v}" for n, v in s["uses"] if n == target), None)
            was = next((u for u in (rec or {}).get("uses") or [] if u.split("@")[0] == target), None)
            if rec and s["frozen"] and now and was and clean(was) != now:
                rec["uses"] = [now if u.split("@")[0] == target else u for u in rec["uses"]]
                changed = (was.split("@", 1)[1], now.split("@", 1)[1])
        if not changed:
            raise SystemExit(f"{plan.name}: no frozen step ran a {target} other than the one the page states")
        lock["accepted"].append({"uses": target, "was": changed[0], "now": changed[1], "why": why.strip()})
    else:
        path = os.path.normpath(target)
        at, _ = plan.where(path)
        if not at.exists():
            raise SystemExit(f"{plan.name}: {path} is not there")
        now, was = digest(at, fresh=True), None
        for name in plan.order:
            s, rec = plan.steps[name], lock["steps"].get(name)
            if not rec:
                continue
            for side in ("inputs", "outputs"):
                old = (rec.get(side) or {}).get(path)
                if old and old != now:
                    was = was or old
                    if s["frozen"]:
                        rec[side][path] = now
        if was is None:
            raise SystemExit(f"{plan.name}: {path} is what the lock records ({now}); nothing to accept")
        lock["accepted"].append({"path": path, "was": was, "now": now, "why": why.strip()})
    finish(plan, lock)
    print(f"{plan.name}: accepted, and written in {LOCK}")


# ---- status ----

GENERIC = {"strings", "grep", "sed", "awk", "head", "tail", "cat", "bash", "sh", "command",
           "python", "python3", "echo", "env"}


def unlisted_tools(plan, step):
    """Tools a step's script mentions and the step does not list. A text search, so it can
    miss one and it can raise a false alarm: a reminder, not a guarantee."""
    text = step["cmd"] + "\n" + "\n".join(
        (plan.dir / s).read_text(errors="replace") for s in step["scripts"])
    # a tool named in a comment is not a tool the step runs
    text = re.sub(r"#[^\n]*", "", text)
    listed = {n for n, _ in step["uses"]}
    out = []
    for name, dep in plan.deps.items():
        if dep["kind"] != "tool" or name in listed:
            continue
        word = name if len(name) > 1 else name + "script"
        if re.search(rf"(?<![\w.-]){re.escape(word)}(?![\w-])", text):
            out.append(name)
    return out


def status(page_dir, fresh=False):
    """Print one page. Runs its checks and version commands, and no steps."""
    plan = Plan(page_dir, strict=False)
    if not plan.steps and not plan.checks:
        return True
    lock = read_lock(page_dir)
    states = inspect(plan, lock, fresh)
    used = sorted({p.partition("/")[0] for s in plan.steps.values() for p in s["inputs"] + s["outputs"]
                   if p.partition("/")[0] in plan.deps}
                  | {n for s in plan.steps.values() for n, _ in s["uses"]})
    print(plan.name)
    for name in used:
        dep = plan.deps[name]
        if dep["kind"] == "tool":
            found, problem = D.version(name, dep)
            print(f"  {name:<16} tool        {found or problem}")
        else:
            where, rule = D.locate(name, dep)
            print(f"  {name:<16} {dep['kind']:<11} {where}  ({rule})"
                  + ("" if where.is_dir() else "  NOT ON THIS MACHINE"))
    ok = True
    width = max((len(n) for n in plan.order), default=0)
    for name in plan.order:
        state, why = states[name]
        step = plan.steps[name]
        mark = " (frozen)" if step["frozen"] else ""
        print(f"  {state:<10}  {name:<{width}}{mark}")
        for w in why:
            print(f"      {w}")
        for dep_name, pinned in step["uses"]:
            if pinned and plan.deps[dep_name]["provision"]:
                at, problem = D.provisioned(dep_name, plan.deps[dep_name], pinned)
                if problem:
                    print(f"      {problem}: this step would refuse to run")
                elif not (step["frozen"] and state == "current"):
                    print(f"      {dep_name}@{pinned} " + (f"is checked out at {at}" if at else
                          "is checked out when this step runs"))
                continue
            found, problem = D.version(dep_name, plan.deps[dep_name])
            if not pinned:
                print(f"      uses {dep_name} without a version; here it is "
                      f"{dep_name}@{D.shown(plan.deps[dep_name], found, found[:7]) if found else '?'}")
                ok = False
            elif found and not D.matches(plan.deps[dep_name], pinned, found) \
                    and not (step["frozen"] and state == "current"):
                print(f"      {dep_name} is at {D.shown(plan.deps[dep_name], found, pinned)} here, "
                      f"and the page states {pinned}: this step would refuse to run")
        for t in unlisted_tools(plan, step):
            print(f"      its script mentions {t}, which it does not list under uses")
        ok = ok and state in ("current", "not here")
    scratch = {"checks": {}}
    if plan.checks:
        ok = run_checks(plan, scratch) and ok
    for n in thawed(plan, lock):
        print(f"  {n}: the lock records it as frozen, and the page no longer says so")
        ok = False
    for w in unsettled(plan, lock):
        print(f"  provisional: {w}")
    for a in lock["accepted"]:
        what = a.get("path") or a.get("step") or a.get("uses")
        print(f"  accepted: {what}: {a.get('why')}")
    HASHER.save()
    return ok


def prune():
    """Remove the provisioned checkouts no step of any page states any more."""
    kept, root = set(), D.CHECKOUTS / ".sloop"
    for d in pages():
        plan = Plan(d, strict=False)
        for step in plan.steps.values():
            for dep_name, pinned in step["uses"]:
                dep = plan.deps[dep_name]
                at = D.provisioned(dep_name, dep, pinned)[0] if dep["provision"] and pinned else None
                if at:
                    kept.add(at)
    for at in sorted(root.glob("*/*")) if root.is_dir() else []:
        if at in kept or not at.is_dir():
            continue
        base = D.git(at, "rev-parse", "--path-format=absolute", "--git-common-dir")
        r = subprocess.run(["git", "-C", str(Path(base).parent if base else at), "worktree",
                            "remove", str(at)], capture_output=True, text=True)
        print(f"{at}: " + ("removed" if r.returncode == 0 else f"kept: {r.stderr.strip()[-200:]}"))
    print(f"{len(kept)} provisioned checkout(s) are stated by a step")


def pages(want=None):
    dirs = sorted(d for d in PAGES.iterdir() if d.is_dir() and page_file(d)
                  and (want is None or d.name.startswith(want)))
    if want is not None:
        exact = [d for d in dirs if d.name == want]
        dirs = exact or dirs
    if not dirs:
        raise SystemExit(f"no page matches {want!r}" if want else "no pages")
    return dirs


def one_page(want):
    found = pages(want)
    if len(found) > 1:
        raise SystemExit(f"{want!r} matches {', '.join(d.name for d in found)}; name one page")
    return found[0]
