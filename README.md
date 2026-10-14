# sloop

A lab notebook as one self-contained HTML page: dated entries of prose whose numbers come
from the scripts that produced them, each entry recording what it ran, at which version, and
the hash of everything it read and wrote.

A `{{key}}` anywhere in a page is replaced by a value one of the page's steps wrote, so a
number in the prose cannot drift from the computation behind it. Rerun the step and the prose
follows. A page declares its steps, and `page.lock` beside it records each one. An input that
has changed underneath a page, a tool at the wrong version and a check that fails are all
refused until someone says the change was meant, and a page that reads unfinished work says
so on its face and cannot be published.

> *slop* is an anagram of *PLoS*, the Public Library of Slop. We generate slop data, run
> slop analyses and present slop results, and then draw real conclusions, which is only
> honest if the slop can be reproduced exactly. Hence the rig: a sloop is the smallest one
> that will still beat upwind, and one person can sail it. A ship carrying a sloop carries
> a slop chest too.

## Install

    pip install -e /path/to/sloop

That puts `sloop` on the path, with `notebook` as an alias.

## Use

    sloop init my-notebook              # notebook.yml and pages/
    sloop new starving-column "A title" # a page, dated
    sloop build                         # render it into site/index.html
    sloop serve                         # build, serve on localhost, rebuild as pages change
    sloop status [PAGE]                 # where each step stands; runs checks and no steps
    sloop make PAGE [STEP]              # run what is stale, in order; a frozen step only by name
    sloop freeze PAGE STEP              # fix a step as it stands, without running it
    sloop unfreeze PAGE STEP            # make it an ordinary step again
    sloop accept PAGE WHAT -m "why"     # say a changed input or version was meant
    sloop cite DOI                      # add a reference to the notebook's .bib file
    sloop pack sloop.pyz                # pack the builder into one file for deployment

The notebook is the directory holding `notebook.yml`: pass `--dir`, set `$NOTEBOOK_DIR`, or
run from anywhere at or below it, so the commands work from inside a page.

## notebook.yml

    title: my-notebook
    repo: https://github.com/you/the-notebook   # per-page source links; omit for none
    branch: main
    pages: pages            # where the page directories live
    out: site               # where the built page is written
    checkouts: ..           # where a dependency is looked for when it gives no path
    bibliography: references.bib
    serve:
      port: 8099
      edit: true            # the in-browser page editor, on localhost only

    dependencies:           # what a page can depend on, declared once
      sim:
        url: https://github.com/you/sim
        version: sim --version
      R:
        version: Rscript -e 'cat(as.character(getRversion()))'
      families:
        url: https://github.com/you/families
      batches:
        path: /data/batches

    sources:                # the sidebar's reading list
      - name: a paper
        url: https://example.org
        note: why it is here

    figure_colors:          # literal colours in a figure, and the stylesheet variable each stands for
      "#000000": ink
      "#1b9e77": fig1

Only `title` is needed.

### Dependencies

The kind of a dependency is read off its fields, because the kinds are not exclusive:

| An entry with | is | and its version is |
|---|---|---|
| `version` | a tool: an installed program or package | the one line that command prints |
| `url` and no `version` | a repository checked out on disk | the checkout's commit |
| neither | a data directory | none |

`url` is where a thing comes from, the same on every machine, and it is the link behind a
version shown on a page: a commit on a git host links to that commit, a url containing
`{version}` is a pattern, and anything else links as it stands.

`path` is where a repository or a data directory sits on this machine. It is looked for in
three places, first match winning: the environment variable `SLOOP_<NAME>`, the entry's own
`path`, and `<checkouts>/<name>`. `$CHECKOUTS` moves the default for one machine. A step's
command receives each location as `$<NAME>`, without the prefix. A tool has no path: it is
whatever its version command finds, and installing the right one is up to you.

A tool whose version is the commit of its source can be provisioned, so that a step runs
against the commit it states and not against whatever is installed:

```yaml
dependencies:
  simfbd:
    url: https://github.com/you/simfbd
    version: simfbd --version
    provision: worktree
    provides: {PATH: bin, PYTHONPATH: src}
```

A step that states `simfbd@1616e09` is then run against a git worktree of that commit, which
`sloop make` creates from the tool's own checkout the first time it is needed and keeps under
`<checkouts>/.sloop/simfbd/`. That checkout is found as a repository's is. `provides` names
the directories of the worktree to put first on each variable, and one a commit does not have
is left out. The version command is run the same way and still has to print the stated commit,
so a `provides` that does not reach the worktree is refused. Two steps can state
two commits of one tool, and the checkout you work in is never moved. The command also
receives the worktree as `$<NAME>`. `sloop prune` removes the worktrees no step states any
more. A commit the checkout does not have is refused: fetch it. A version that is not a
commit, or one built with uncommitted changes, cannot be provisioned.

A dependency that is not on this machine is reported as not checkable here, not as missing.
A build never needs any of them.

## Deploying

`sloop pack` writes a zipapp carrying everything it imports, under 1 MB, which runs
on any python3 with nothing installed and nothing to resolve:

    sloop pack sloop.pyz
    python3 sloop.pyz build --no-drafts --out _site

Commit that file beside the pages and a GitHub Actions job is one line with no setup step,
since every runner image already ships python3. The same file works on Netlify, Cloudflare
Pages, a container or a cluster node. The compiled YAML accelerator is dropped when packing,
so the archive is platform independent and the pure-Python parser is used instead.

## A page

A page is a directory under `pages/` holding a `page.md` whose header is YAML terminated by
`---`:

    title: The col=3 move was starving the column that mixes worst
    date: 2026-10-10
    draft: true                    # keeps it local: `sloop build --no-drafts` holds it back
    tags: mcmc, moves
    steps:
      simulate:
        cmd: ./simulate.sh
        uses: sim@2523662            # runs this, which must be at this version
        frozen: true                 # runs only when named, never overwrites without --force
        outputs: [batches/rho0/specimens]
      summarize:
        cmd: ./summarize.R
        uses: R@4.5.3
        inputs:  [batches/rho0/specimens]
        outputs: [values.tsv, ranks.csv]
    checks:
      three intervals:
        cmd: head -1 $BATCHES/rho0/true_vals.tsv | tr '\t' '\n' | grep -c '^lambda'
        expect: 3
    ---
    Body, in Markdown. Raw HTML passes through.

Steps are named as the page likes, and there can be any number.

| Field | Meaning |
|---|---|
| `cmd` | The command, run from the page directory. |
| `uses` | `name@version`, or a list: the step runs that dependency, which must be at that version when the step runs. The version has to be stated. |
| `frozen` | The step runs only when named, and never overwrites its outputs without `--force`. |
| `inputs` | Paths the step reads. |
| `outputs` | Paths the step writes. |

A path whose first part names a dependency is inside that checkout or data directory, and
anything else is relative to the page. Edges come from paths: a step that reads what another
writes runs after it. A step's own script, every word of `cmd` that names a file in the page
directory, is an input without being listed. A cycle, two steps writing the same path and a
`uses` that names nothing are refused before anything runs.

A page with no script behind it can still say what it rests on, with `uses:` at the top of
its header. That is shown and never enforced.

### The record

`sloop make` writes `page.lock` beside the page: for each step, the versions it ran and the
hash of every path it read and wrote. It is committed.

A tool that makes data may pick its own tools, as simfbd picks the build of rb that analyzes
a run. Where a path a step reads or writes has a `manifest.yml` at or above it with a `uses`
list of `name@version`, the lock copies the list under `made_with`, and the provenance block
shows it under the step's `uses`, each name linked through its entry in `dependencies`. It says what
ran there and is never compared with what is installed here.

On the page the block opens as a footer under the entry, one step after another, each with
what it `uses`, its `input` and its `output`. A version links to its commit and a file in the
notebook links to itself. A step that states no `uses` shows the versions it rests on through
its inputs: those of the step that wrote them, and those a manifest names.

A step is then one of

| State | Meaning |
|---|---|
| missing | an output is not there |
| moved | an output differs from what the step recorded writing |
| stale | an input, a script or a stated version differs from what it recorded |
| unrecorded | its outputs are there, and nothing says how they got there |
| current | none of those |

A stale step that is not frozen is rerun. What is not supposed to change underneath a page is
refused: an input that lives in a dependency and that no step here writes,
the output of a frozen step, a version. `sloop accept PAGE WHAT -m "why"` writes down that
the change was meant, in the lock, with the reason.

`sloop freeze PAGE STEP` records a step as it stands without running it, for work done
outside sloop, such as an analysis started on a cluster. Nothing checks what a frozen step
ran, so it is only for what cannot be cheaply rerun.

A check is a command of a line or two with the answer the page expects. Its definition is
hashed into the lock, so an edited check shows in the diff.

Hashes are remembered by file size and modification time in `.sloop/`, which should be
ignored by git, so a directory of ten thousand logs is not read again on every status.
`sloop status --rehash` reads everything, and freezing and building always do.

### Unfinished work

A page that reads the output of a step with no record, or whose quick step ran on
uncommitted code, is provisional: the lock says why, the page carries a banner, and
`sloop build --no-drafts` fails on it unless it is marked `draft: true`. Nothing clears that
by hand. A frozen step refuses to run on uncommitted code at all.

Results can be watched as they come in. With an analysis running, `sloop make PAGE` skips it
and reruns the summary against the logs that exist.

### Writing

| In a page | Becomes |
|---|---|
| `{{key}}` | a value from `values.tsv`, or any `*.values.tsv` a step wrote: one `key<TAB>value` a line |
| `$x^2$`, `$$ ... $$` | math, converted to MathML at build time |
| `![A caption](ranks.csv)` on a line by itself | a table: the first row is the header, a cell is inline Markdown, and the caption may be empty |
| `![A caption](plot.svg)` | a figure, inlined; `.png` and `.jpg` are embedded, `.md` is included, `.txt` is shown as it is |
| `{#fig:name}`, `{#tbl:name}`, `{#eq:name}` after one of those | a label, which numbers it |
| `@fig:name`, `[@fig:a; @tbl:b]` | "Figure 2", linked |
| `[@Key]`, `@Key` | a citation from the `.bib` file, and a reference list at the foot of the page |
| `==text==` | a highlight |
| `[text]{.name}` | a span with that class, for a table cell that wants its own style; `hi` and `lo` are defined |

A `{{key}}` with no value, an embedded file that is missing, a reference to a label that is
not there, a citation that is not in the `.bib` file and math the converter does not know
all stop the build. A table's emphasis belongs in the data: the script that knows which
number is notable writes `**0.555**` or `==+2.69==` in the cell, and it follows the numbers
when they are regenerated.

An SVG's colours that are listed under `figure_colors` are swapped for the stylesheet
variables they stand for, so the figure follows the page between light and dark. One with
none of them, and any bitmap, is inverted in the dark instead.

The spelling is Pandoc's wherever there was a choice, so a page can be handed to it for a
manuscript. `sloop cite 10.1371/journal.pcbi.1007999` asks doi.org for the entry and appends
it to the `.bib` file as supplied.

A build reads only committed files: the page, its lock, and the outputs beside it. It checks
that those outputs are the ones the lock records, and needs no checkout, no tool and no data.
