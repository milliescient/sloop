#!/usr/bin/env python3
"""Turn a PDF into a library entry, out of process.

    python3 -m sloop.librarian <notebook-root> <key>

Reads library/pdf/<key>.pdf, asks Claude for the fields the library renders, and writes
library/<key>.json. The notebook's own library/PROMPT.md supplies the project context, so what
counts as a useful entry is the notebook's business rather than sloop's.

It runs detached from the request that started it: generation takes a minute or so, and the
page picks the result up over live reload when the file lands.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODEL = "claude-opus-5"


def claude_bin():
    """systemd gives a service a short PATH, and claude is usually installed under ~/.local."""
    found = shutil.which("claude")
    if found:
        return found
    for d in (Path.home() / ".local/bin", Path("/usr/local/bin")):
        if (d / "claude").is_file():
            return str(d / "claude")
    return "claude"
CHARS = 60000           # the front of a paper carries almost everything an entry needs

SCHEMA = """Return ONLY a JSON object, no prose around it, with exactly these keys:
  key          the citation key you were given
  authors      "Surname, A., Surname, B." or "Surname, A. et al." past six
  year         four digits as a string
  title        the paper's title
  venue        journal, volume and pages, or the preprint server
  doi          bare doi, or "" if none is printed
  version      "published article", "preprint", "chapter", with anything odd noted
  one_line     one sentence: what this paper is, for someone deciding whether to read it
  summary      one paragraph of what it actually does. No throat-clearing.
  points       3 to 8 strings, the specific claims, methods or numbers worth having
  age_uncertainty  how this paper treats the age of a fossil occurrence, or "" if it does not
  connection   2 to 5 strings: how it bears on THIS project, each starting with a verb
  quotes       0 to 3 objects {"text": "...", "where": "section or page"}, verbatim only
Write plainly. No em dashes, no semicolons. Do not invent a number, a quote or a DOI: leave a
field empty rather than guess at it."""


def run(root: Path, key: str) -> int:
    lib = root / "library"
    pdf = lib / "pdf" / f"{key}.pdf"
    out = lib / f"{key}.json"
    out.write_text(json.dumps({"key": key, "status": "reading"}, indent=2))

    if not pdf.is_file():
        out.write_text(json.dumps({"key": key, "status": "error",
                                   "error": "no pdf to read"}, indent=2))
        return 1
    try:
        text = subprocess.run(["pdftotext", "-q", str(pdf), "-"], capture_output=True,
                              text=True, timeout=120).stdout[:CHARS]
    except (OSError, subprocess.SubprocessError) as e:
        out.write_text(json.dumps({"key": key, "status": "error",
                                   "error": f"pdftotext: {e}"}, indent=2))
        return 1
    if len(text.strip()) < 400:
        out.write_text(json.dumps({"key": key, "status": "error",
                                   "error": "the pdf has no extractable text, so it needs OCR"},
                                  indent=2))
        return 1

    ctx = (lib / "PROMPT.md").read_text() if (lib / "PROMPT.md").is_file() else ""
    prompt = (f"{ctx}\n\n{SCHEMA}\n\nThe citation key is {key}.\n\n"
              f"Here is the paper:\n\n{text}")
    try:
        r = subprocess.run([claude_bin(), "-p", "--output-format", "json", "--model", MODEL,
                            "--allowed-tools", ""],
                           input=prompt, capture_output=True, text=True, timeout=900)
        payload = json.loads(r.stdout)
        if isinstance(payload, list):
            payload = payload[-1]
        body = payload.get("result", "")
    except Exception as e:                                   # noqa: BLE001
        out.write_text(json.dumps({"key": key, "status": "error",
                                   "error": f"generation failed: {e}"}, indent=2))
        return 1

    body = body.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        entry = json.loads(body)
    except ValueError:
        out.write_text(json.dumps({"key": key, "status": "error",
                                   "error": "the model did not return JSON",
                                   "raw": body[:2000]}, indent=2))
        return 1
    entry["key"] = key
    entry["status"] = "generated"
    out.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 -m sloop.librarian <notebook-root> <key>")
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2]))
