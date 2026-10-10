# labnotebook

A lab notebook as one self-contained HTML page: dated entries of prose whose numbers come
from the scripts that produced them, each entry pinned to the commit of the repository it
rests on.

A `{{key}}` anywhere in a page is replaced by a value the page's own script printed, so a
number in the prose cannot drift from the computation behind it. Rerun the build and the
prose follows.

## Install

    pip install -e /path/to/labnotebook

That puts `labnotebook` on the path, with `notebook` as a short alias.

## Use

    notebook init my-notebook      # notebook.yml and pages/
    notebook new "A title"         # the next page, numbered and dated
    notebook build                 # render it into site/index.html
    notebook serve                 # build, serve on localhost, rebuild as pages change
    notebook assets [NN]           # run a page's script and cache what it printed
    notebook artifacts NN          # run the long analysis behind those assets

The notebook is the directory holding `notebook.yml`: pass `--dir`, set `$NOTEBOOK_DIR`, or
run from anywhere at or below it, so the commands work from inside a page.

## notebook.yml

    title: fossilizedbirthdeathology
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

## A page

A page is a directory under `pages/`, numbered so entries can cite each other, holding a
`page.md` whose header is YAML terminated by `---`:

    title: The col=3 move was starving the column that mixes worst
    date: 2026-10-10
    draft: true                    # keeps it local: `notebook build --no-drafts` holds it back
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

`notebook artifacts NN` runs the long step, which may take hours and refuses to overwrite
output that already exists. `notebook assets NN` reduces it to a few numbers and caches them
in `data.out`, which is committed with the page. A build reads the assets only, so a clone
renders the whole notebook with no checkout and no analysis at all.

A make script prints its summary to stdout. Everything after a line reading `## values` is
`key<TAB>value` pairs, and those are the `{{key}}` substitutions. A `{{key}}` with no value
stops the build, as does a declared asset that is missing.

Each ref reaches the make script as an environment variable named after it, uppercased:
`$SIM` above, resolved against `checkouts`.
