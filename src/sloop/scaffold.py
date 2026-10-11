#!/usr/bin/env python3
"""Scaffold a notebook page, so writing one is filling in a file rather than remembering a format.

    sloop new starving-column
    sloop new starving-column "The col=3 move was starving the column that mixes worst"

The name is the directory, and the directory name is the page's identity: the slug in a URL
and what `sloop make` matches on. It is given rather than derived, because a name derived
from a title goes stale the moment the title changes, and a number implies an order that the
table of contents takes from dates anyway. Prints the path. Then write the body and run
`sloop build`.
"""
import datetime
import re
from pathlib import Path


TEMPLATE = """title: {title}
date: {date}
draft: true
tags:
# Numbers from a script? Declare the step that writes them, then: sloop make {name}
# steps:
#   summarize:
#     cmd: ./summarize.R
#     uses: [{uses}]
#     inputs:  []
#     outputs: [values.tsv]
---
Body, in Markdown. `{{{{key}}}}` is a value a step wrote to values.tsv, `$x^2$` is math,
`![A caption](table.csv)` on a line by itself shows a table or a figure a step wrote, and
`[@Key]` cites the notebook's .bib file.
"""

# a directory name and a URL slug, so keep it to what both carry without escaping
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def new(pages, name, words=(), dependencies=None):
    if not name:
        raise SystemExit('usage: sloop new <name> ["Title of the finding"]')
    if not NAME.fullmatch(name):
        raise SystemExit(f"{name!r} is not a usable name: lowercase letters, digits and "
                         f"single hyphens, e.g. starving-column")
    PAGES = pages
    title = " ".join(words).strip() or name.replace("-", " ")

    d = PAGES / name
    if d.exists():
        raise SystemExit(f"{d.name} already exists")
    d.mkdir()
    path = d / "page.md"
    # every tool the notebook knows, so the one a script runs is a deletion away from listed
    tools = [f"{n}@VERSION" for n, d in (dependencies or {}).items() if (d or {}).get("version")
             and not isinstance(d, str)]
    path.write_text(TEMPLATE.format(title=title, date=datetime.date.today().isoformat(),
                                    name=name, uses=", ".join(tools) or "tool@VERSION"))
    # a page carries its own environment so it can be read anywhere it is cloned
    (d / ".env.default").write_text(
        "# Copy to .env and edit. The builder reads .env, which is not tracked, so the page\n"
        "# moves between environments without carrying anyone's paths.\n"
        "# the interpreter for analysis.*: RSCRIPT, PYTHON, BASH or JULIA\n"
        "RSCRIPT=Rscript\n")
    print(path)
    print("  fill in the title, tags and the body, then: sloop build")
    print("  it stays out of the site until you drop the draft: line")
    print(f"  numbers from a script? uncomment steps:, then: sloop make {d.name}")
    return path
