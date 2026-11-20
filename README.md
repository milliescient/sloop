# sloop

sloop makes a lab notebook as a static site, with dated entries whose numbers come from the scripts that produced them. Each entry records what it ran, at which version, and the hash of everything it read and wrote.

A `{{key}}` anywhere in a page is replaced by a value written by one of the page's steps. Rerun the step and the prose follows the computation.

A page declares its steps, and `page.lock` beside it records each one. An ordinary step reruns when anything it reads changes and reports what changed. A frozen step marks a result as decided. Changes to what it rests on are refused until you record that the change was meant.

A page that reads unfinished work carries a banner. A page reaches anyone else only when you share it.

> *slop* is an anagram of *PLoS*, the Public Library of Slop. We generate slop data, run
> slop analyses and present slop results, and then draw real conclusions, which is only
> honest if the slop can be reproduced exactly. Hence the rig: a sloop is the smallest one
> that will still beat upwind, and one person can sail it. A ship carrying a sloop carries
> a slop chest too.

## Install

Install from the public repository:

    pip install git+https://github.com/milliescient/sloop

If you are working on a checkout, install from that checkout:

    pip install -e /path/to/sloop

Both put `sloop` on the path, with `notebook` as an alias.

## Use

    sloop init my-notebook              # notebook.yml and pages/
    sloop new starving-column "A title" # a page, dated
    sloop build                         # render it into site/, a page per entry
    sloop build --static                # for a static host: no terminal, no library import
    sloop export PAGE [-o FILE]         # one page as one file, to send to someone
    sloop share PAGE                    # the page at a link that can be taken back
    sloop share --serve                 # serve shared pages at /share/<token>/
    sloop unshare TOKEN|PAGE            # take it back
    sloop serve                         # build, serve on localhost, rebuild as pages change
    sloop status [PAGE]                 # where each step stands; runs checks and no steps
    sloop make PAGE [STEP]              # run what is stale, in order; a frozen step only by name
    sloop freeze PAGE STEP              # fix a step as it stands, without running it
    sloop unfreeze PAGE STEP            # make it an ordinary step again
    sloop accept PAGE WHAT -m "why"     # say a change under a frozen step was meant
    sloop cite DOI                      # add a reference to the notebook's .bib file
    sloop pack sloop.pyz                # pack the builder into one file for deployment
    sloop prune                         # remove provisioned worktrees no step states any more

The notebook is the directory holding `notebook.yml`. Pass `--dir`, set `$NOTEBOOK_DIR`, or run from anywhere at or below that directory. The commands also work from inside a page.

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
    palette:                # stylesheet variables this notebook sets over the default palette
      light: {ground: "#E8EBF1", ink: "#121A30", accent: "#2F4A9C"}
      dark:  {ground: "#14213F", ink: "#F0F2F7", accent: "#9FB2EE"}

Only `title` is needed.

### Dependencies

A dependency's fields determine its kind. The kinds are not exclusive.

| An entry with | is | and its version is |
|---|---|---|
| `version` | a tool: an installed program or package | the one line that command prints |
| `url` and no `version` | a repository checked out on disk | the checkout's commit |
| neither | a data directory | none |

`url` says where a dependency comes from and stays the same on every machine. It also supplies the link behind a version shown on a page. A commit on a git host links to that commit. A URL containing `{version}` is a pattern. Any other URL links as it stands.

`path` says where a repository or data directory sits on this machine. sloop uses the first match among the environment variable `SLOOP_<NAME>`, the entry's own `path`, and `<checkouts>/<name>`. `$CHECKOUTS` changes the default for one machine. Each step's command receives the location as `$<NAME>`, without the prefix.

A tool has no path. It is whatever its version command finds. You are responsible for installing the right version.

A tool whose version is its source commit can be provisioned. This runs a step against the commit it states rather than whatever is installed:

```yaml
dependencies:
  simfbdr:
    url: https://github.com/you/simfbdr
    version: simfbdr --version
    provision: worktree
    provides: {PATH: bin, PYTHONPATH: src}
```

A step that states `simfbdr@1616e09` runs against a git worktree of that commit. `sloop make` creates the worktree from the tool's own checkout the first time it is needed and keeps it under `<checkouts>/.sloop/simfbdr/`. The tool's checkout is found in the same way as a repository dependency.

`provides` names the worktree directories to put first on each variable. A directory that the commit does not have is left out. The version command runs in the same environment and must still print the stated commit. A `provides` setting that does not reach the worktree is refused.

Two steps can state two commits of one tool. The checkout you work in is never moved. The command also receives the worktree as `$<NAME>`. `sloop prune` removes worktrees that no step states any more.

A commit absent from the checkout is refused. Fetch it before running the step. A version that is not a commit, or one built with uncommitted changes, cannot be provisioned.

For a renamed dependency, set `formerly: old-name` or give a list of former names. A lock or manifest written under an old name is then read under the new one. The rename makes nothing stale.

A dependency absent from this machine is reported as not checkable here, not as missing. A build needs none of the dependencies.

## A page

A page is a directory under `pages/` containing a `page.md`. Its YAML header ends with `---`:

    title: The col=3 move was starving the column that mixes worst
    date: 2026-10-10
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

You can name steps as you like and include any number.

| Field | Meaning |
|---|---|
| `cmd` | The command, run from the page directory. |
| `uses` | `name@version`, or a list: the step runs that dependency, which must be at that version when the step runs. The version has to be stated. |
| `frozen` | The step runs only when named, and never overwrites its outputs without `--force`. |
| `inputs` | Paths the step reads. |
| `outputs` | Paths the step writes. |

A path whose first part names a dependency is inside that checkout or data directory. Other paths are relative to the page. Paths determine the order of steps. A step that reads what another writes runs after it.

A step's own script is an input without being listed. This includes every word of `cmd` that names a file in the page directory. A cycle, two steps writing the same path, or a `uses` entry naming an undeclared dependency is refused before anything runs.

A page with no script can still state what it rests on with `uses:` at the top of its header. This is shown but never enforced.

### The record

`sloop make` writes `page.lock` beside the page. For each step, it records the versions that ran and the hash of every path read and written. Commit this file.

A tool that makes data may choose its own tools, as simfbdr chooses the build of rb that analyzes a run. A path that a step reads or writes can have a `manifest.yml` at or above it with a `uses` list of `name@version`. The lock copies that list under `made_with`. The provenance block shows it under the step's `uses`, with each name linked through its entry in `dependencies`. This records what ran there. It is never compared with what is installed here.

The provenance block opens as a footer under the entry. It shows each step with its `uses`, `input` and `output`. A version links to its commit, and a file in the notebook links to itself. A step with no `uses` shows the versions it rests on through its inputs. These come from the step that wrote the inputs and from any manifest.

A step has one of these states:

| State | Meaning |
|---|---|
| missing | an output is not there |
| moved | an output differs from what the step recorded writing |
| stale | an input, a script or a stated version differs from what it recorded |
| unrecorded | its outputs are there, and nothing says how they got there |
| current | none of those |

A stale step that is not frozen reruns, whatever changed, and prints which inputs changed. A version stated by a step is a requirement. The step does not run if that version is not installed.

Only a frozen step marks a result as decided. Changes are refused when they affect an input a frozen step read, or an output of a frozen step that another step reads. Use `sloop accept PAGE WHAT -m "why"` to record in the lock that a change was meant, with your reason.

Freezing adds protection and unfreezing removes it. `sloop freeze PAGE` freezes every step of a wholly current page and refuses a page that is not wholly current. `sloop unfreeze`, `sloop accept` and `sloop make --force` on a frozen step each require you to type `yes` at a terminal. They refuse without it, so an agent can freeze a page but cannot undo the protection.

The lock records which steps are frozen. If you delete `frozen: true` from the page by hand, `sloop make` refuses the change and `sloop status` reports it.

`sloop freeze PAGE STEP` records a step as it stands without running it. Use this only for work that cannot be cheaply rerun, such as an analysis started on a cluster. Nothing checks what a frozen step ran.

A check is a command of a line or two with the answer the page expects. Its definition is hashed into the lock, so an edited check appears in the diff.

Hashes are remembered by file size and modification time in `.sloop/`. Add this directory to your git ignore rules. This avoids rereading a directory of ten thousand logs on every status check. `sloop status --rehash` reads everything. Freezing and building always read everything.

### Unfinished work

A page is provisional if it reads the output of a step with no record, or if its quick step ran on uncommitted code. The lock records why, and the page carries a banner, including in a shared copy. You cannot clear this by hand. A frozen step refuses to run on uncommitted code.

You can watch results as they come in. While an analysis is running, `sloop make PAGE` skips it and reruns the summary against the logs that exist.

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

The build stops for a `{{key}}` with no value, a missing embedded file, a reference to a missing label, a citation absent from the `.bib` file, or math the converter does not know.

Put a table's emphasis in the data. The script that knows which number is notable should write `**0.555**` or `==+2.69==` in the cell. The emphasis then follows the numbers when they are regenerated.

SVG colours listed under `figure_colors` are replaced with the stylesheet variables they stand for. This makes the figure follow the page between light and dark. An SVG with none of those colours is inverted in the dark instead. Bitmaps are also inverted.

The spelling follows Pandoc wherever there was a choice, so you can hand a page to Pandoc for a manuscript. `sloop cite 10.1371/journal.pcbi.1007999` asks doi.org for the entry and appends it to the `.bib` file as supplied.

A build reads only committed files: the page, its lock and the outputs beside it. It checks that the outputs match the lock. It needs no checkout, tool or data.

### Dates

The index lists when each page was created and when it last changed. The created time is the earliest recorded time, either the page's first commit or the oldest of its files. If the header's `date:` is earlier still, that date is used without a time. A notebook put under git late therefore keeps its pages' own dates.

The updated time is the page's last commit, or its newest file before it is committed.

### Scratch

Every notebook has a scratch page at `pages/scratch/page.md`, with `scratch: true` in its header. The first command you run creates it if it is absent. Deleting it brings a fresh one back on the next command.

Use the scratch page for notes, arithmetic and dead ends until they are worth a page. You can reach it from the square in the rail. It has no index row, tags or date counts.

## Serving

`sloop serve` builds the site, serves it on localhost at `serve.port`, and rebuilds when a page changes. The default port is 8099. The browser reloads when the page changes. A failed build never replaces the served copy.

With `serve.edit: true`, each page has an editor. A save is built in a scratch directory first and rolled back if the build fails. The editor answers only on localhost.

Every response has a noindex header, and every page has a noindex meta tag. Neither is access control.

You can serve a notebook under a path prefix behind a proxy. For example, use `tailscale serve --set-path /notebook http://127.0.0.1:8099`. sloop emits relative links, so the site works under any prefix. A page reached without its trailing slash adds the slash before anything loads.

### The sidebar

Every page has a rail on the left that opens a sidebar. Press ctrl+` to toggle it. The sidebar has notebook and library views.

The notebook view is a terminal. Set `serve.panes` to a list of `{url, label}` entries. The entry labelled `terminal` is loaded in a frame in the sidebar. For example, `panes: [{url: /term/, label: terminal}]` uses a ttyd served beside the notebook at `/term/`. Without a terminal, the library is the only view.

### The library

`library/` holds one PDF per paper at `library/pdf/<key>.pdf`. The key is the citation key in the `.bib` file. Each paper's notes are stored in `library/<key>.json`.

The library view lists the papers. A paper's page shows its PDF with its notes beside it. A second PDF whose name starts with the paper's key and an underscore is treated as another file of that paper, such as a supplement.

With `sloop serve` and `edit: true`, you can enter a DOI in the library view or drop a PDF onto it. A DOI is cited into the `.bib` file in the same way as `sloop cite`. A dropped PDF is read by Claude through the `claude` command line tool, which must be installed. Its notes are written to `library/<key>.json`.

A notes file contains `one_line`, `summary`, `points`, `connection` and `quotes`, along with the paper's details. Use `library/PROMPT.md` to state the project and what the notebook cares about. The notes can then say what a paper means for this notebook rather than just that it is relevant.

You can name fields of your own in `PROMPT.md`. The notes then carry those fields, and the paper's page shows them under their names.

If a paper cannot be fetched automatically, add it by hand. Put the PDF in `library/pdf/` under its citation key.

## Sharing

`sloop share PAGE` exports a page as one file into `share/`, under a token no one can guess. Add `share/` to your git ignore rules. Anyone holding the link can read the page.

`sloop share --serve` serves only shared pages at `/share/<token>/`, with no listing and no index. It uses `share.port`, which defaults to 8106. The public address is read from the Tailscale Funnel that proxies that port, or you can set it with `share.url`.

Sharing an already shared page refreshes it at the same link. `sloop unshare` removes its directory to revoke the link. Run `sloop share` without a page to list what is shared.

## Static builds

Use `sloop build --static` for any deployment that is not `sloop serve`. It makes a site for a host with no sloop behind it, such as App Engine, S3 or GitHub Pages.

A static build leaves out the terminal, the library's DOI and PDF import, and requests to `sloop serve` for the editor and live reload.

`sloop build` does not copy the library's PDFs into the site. If a deployment should include them, copy `library/pdf/` into `<out>/library/pdf/` yourself.

## Deploying

`sloop pack` writes a zipapp carrying everything it imports. The archive is under 1 MB and runs on any python3 with nothing installed and nothing to resolve:

    sloop pack sloop.pyz
    python3 sloop.pyz build --out _site

Commit the archive beside the pages. A GitHub Actions job is then one line with no setup step, since every runner image already ships python3. Use `--static` when building for that deployment.

The same archive works on Netlify, Cloudflare Pages, a container or a cluster node. Packing drops the compiled YAML accelerator and uses the pure-Python parser instead, so the archive is platform independent.
