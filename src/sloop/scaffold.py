#!/usr/bin/env python3
"""Scaffold a notebook page, so writing one is filling in a file rather than remembering a format.

    sloop new starving-column
    sloop new starving-column "The col=3 move was starving the column that mixes worst"

The name is the directory, and the directory name is the page's identity: the slug in a URL
and what `sloop assets` matches on. It is given rather than derived, because a name derived
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
refs:
---
Body, in Markdown. Raw HTML passes through, so a table that needs per-cell classes
stays HTML: wrap it in `<div class="scroll">`, and mark a good number with
`<td class="hi">` and a bad one with `<td class="lo">`.
"""

# a directory name and a URL slug, so keep it to what both carry without escaping
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def new(pages, name, words=()):
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
    path.write_text(TEMPLATE.format(title=title, date=datetime.date.today().isoformat()))
    # a page carries its own environment so it can be read anywhere it is cloned
    (d / ".env.default").write_text(
        "# Copy to .env and edit. The builder reads .env, which is not tracked, so the page\n"
        "# moves between environments without carrying anyone's paths.\n"
        "# the interpreter for analysis.*: RSCRIPT, PYTHON, BASH or JULIA\n"
        "RSCRIPT=Rscript\n")
    print(path)
    print("  fill in the title, tags, refs and the body, then: sloop build")
    print("  it stays out of the site until you drop the draft: line")
    print(f"  numbers from a script? add {d.name}/make-assets, then: sloop assets {d.name}")
    return path
