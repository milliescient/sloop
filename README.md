# sloop

A lab notebook as one self-contained HTML page: dated entries of prose whose numbers come
from the scripts that produced them, each entry pinned to the commit of the repository it
rests on.

A `{{key}}` anywhere in a page is replaced by a value the page's own script printed, so a
number in the prose cannot drift from the computation behind it. Rerun the build and the
prose follows. Declared assets that are missing, refs at the wrong commit and artifacts
whose digests have changed all stop the build, because a page that cannot be reproduced
should not render.

> *slop* is an anagram of *PLoS*, the Public Library of Slop. We generate slop data, run
> slop analyses and present slop results, and then draw real conclusions, which is only
> honest if the slop can be reproduced exactly. Hence the rig: a sloop is the smallest one
> that will still beat upwind, and one person can sail it. A ship carrying a sloop carries
> a slop chest too.

## Install

    pip install -e /path/to/sloop

That puts `sloop` on the path, with `notebook` as an alias.

## Use

    sloop init my-notebook         # notebook.yml and pages/
    sloop new "A title"            # the next page, numbered and dated
    sloop build                    # render it into site/index.html
    sloop serve                    # build, serve on localhost, rebuild as pages change
    sloop assets [NN]              # run a page's script and cache what it printed
    sloop artifacts NN             # run the long analysis behind those assets
    sloop pack sloop.pyz           # pack the builder into one file for deployment

The notebook is the directory holding `notebook.yml`: pass `--dir`, set `$NOTEBOOK_DIR`, or
run from anywhere at or below it, so the commands work from inside a page.

## notebook.yml

    title: my-notebook
    repo: https://github.com/you/the-notebook   # per-page source links; omit for none
    branch: main
    pages: pages            # where the page directories live
    out: site               # where the built page is written
    sources: sources.tsv    # optional, the sidebar's reading list
    checkouts: ..           # where the repositories a page pins by commit are checked out
    serve:
      port: 8099
      edit: true            # the in-browser page editor, on localhost only

Only `title` is needed. Everything above is the default.

## Deploying

`sloop pack` writes a zipapp carrying its own markdown and yaml, about 180 KB, which runs
on any python3 with nothing installed and nothing to resolve:

    sloop pack sloop.pyz
    python3 sloop.pyz build --no-drafts --out _site

Commit that file beside the pages and a GitHub Actions job is one line with no setup step,
since every runner image already ships python3. The same file works on Netlify, Cloudflare
Pages, a container or a cluster node. The compiled YAML accelerator is dropped when packing,
so the archive is platform independent and the pure-Python parser is used instead.

## A page

A page is a directory under `pages/`, numbered so entries can cite each other, holding a
`page.md` whose header is YAML terminated by `---`:

    title: The col=3 move was starving the column that mixes worst
    date: 2026-10-10
    draft: true                    # keeps it local: `sloop build --no-drafts` holds it back
    tags: mcmc, moves
    refs:                          # checkouts the page rests on, verified before anything runs
      - url: https://github.com/you/sim
        commit: 2523662d3
    assets:                        # tracked, and all a build reads
      make: bash make-assets
      paths: [data.out]
    artifacts:                     # the long output, left where it was made
      make: bash make-artifacts
      paths: [sim/sims/rho0n10/specimens]
    ---
    Body, in Markdown. Raw HTML passes through.

`sloop artifacts NN` runs the long step, which may take hours and refuses to overwrite
output that already exists. `sloop assets NN` reduces it to a few numbers and caches them
in `data.out`, which is committed with the page. A build reads the assets only, so a clone
renders the whole notebook with no checkout and no analysis at all.

A make script prints its summary to stdout. Everything after a line reading `## values` is
`key<TAB>value` pairs, and those are the `{{key}}` substitutions. A `{{key}}` with no value
stops the build, as does a declared asset that is missing.

Each ref reaches the make script as an environment variable named after it, uppercased:
`$SIM` above, resolved against `checkouts`.
