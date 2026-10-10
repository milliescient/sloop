#!/usr/bin/env python3
"""Scaffold a notebook, so writing one is filling in a file rather than remembering a format.

    notebook new "The col=3 move was starving the column that mixes worst"

Picks the next number, slugs the title, stamps today's date, and leaves the header keys
waiting. Prints the path. Then write the body and run `notebook build`.
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


def new(pages, words):
    if not words:
        raise SystemExit('usage: notebook new "Title of the finding"')
    title = " ".join(words).strip()
    PAGES = pages

    used = [int(d.name.split("-")[0]) for d in PAGES.iterdir()
            if d.is_dir() and d.name.split("-")[0].isdigit()]
    num = max(used, default=0) + 1
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].rstrip("-")

    d = PAGES / f"{num:02d}-{slug}"
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
    print("  fill in tags, refs and the body, then: notebook build")
    print("  it stays out of the site until you drop the draft: line")
    print(f"  numbers from a script? add {d.name}/make-assets, then: "
          f"notebook assets {num:02d}")
    return path
