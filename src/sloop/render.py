"""What a page can write beyond plain Markdown, in Pandoc's spelling wherever there is a choice.

    $\\tau_K - T$                     math, converted to MathML at build time
    $$ ... $$ {#eq:proposal}          a displayed equation, numbered because it is labelled
    ![A caption](ranks.csv){#tbl:r}   a file a step wrote, rendered by its type
    @fig:ranks, [@fig:a; @tbl:b]      a numbered reference to a labelled figure, table, equation
    [@Magee_2020], @Magee_2020        a citation from the notebook's .bib file
    ==+2.69==                         a highlight
    {{key}}                           a value from a values file

Nothing here runs at view time: the page stays one file with no script behind its content.
Pandoc itself is not used. The spelling is its, so a page can be handed to it for a manuscript.

Everything is done on the Markdown source with code set aside first, and what is produced is
swapped back in after Markdown has run, so Markdown never sees the HTML it would mangle.
"""
import base64
import csv
import html
import io
import re
from pathlib import Path

KINDS = {"fig": "Figure", "tbl": "Table", "eq": "Equation"}
LABEL = r"(fig|tbl|eq):[A-Za-z0-9_.-]*[A-Za-z0-9_]"
EMBED = re.compile(r"^!\[(?P<cap>.*)\]\((?P<path>[^)\s]+)\)(?:\{(?P<attrs>[^}]*)\})?\s*$")
VALUE = re.compile(r"\{\{\s*([A-Za-z0-9_.+-]+)\s*\}\}")
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
COLOUR = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgb\([^)]*\)")


class Failure(Exception):
    """Something in a page that cannot be rendered. The caller names the page."""


def markdown_lib():
    try:
        import markdown
    except ModuleNotFoundError:
        raise SystemExit("rendering a .md page needs the markdown package")
    return markdown


def inline(text):
    """A caption or a table cell: Markdown without the paragraph around it."""
    out = markdown_lib().markdown(text, extensions=["attr_list"]).strip()
    if out.startswith("<p>") and out.endswith("</p>") and out.count("<p>") == 1:
        out = out[3:-4]
    return out


# ---- math ----

def to_mathml(tex, block):
    try:
        from latex2mathml.converter import convert
    except ModuleNotFoundError:
        raise SystemExit("math written as $...$ needs the latex2mathml package")
    # Two commands the converter gets wrong or does not have, spelt the way it does know:
    # it writes \\sim as a tilde, and has no \\arg.
    spelt = re.sub(r"\\sim(?![A-Za-z])", r"\\thicksim", tex.strip())
    spelt = re.sub(r"\\arg(?![A-Za-z])", r"\\operatorname{arg}", spelt)
    try:
        out = convert(spelt, display="block" if block else "inline")
    except Exception as e:
        raise Failure(f"could not convert the math `{tex.strip()[:60]}`: {e}")
    # The converter does not refuse what it does not know. An unknown command comes back as
    # literal text, and some environments as markup that is not well formed, so both are
    # caught here rather than shipped.
    if "\\" in re.sub(r"<mtext[^>]*>.*?</mtext>", "", out):
        left = re.search(r"\\[A-Za-z]+", out)
        raise Failure(f"the math `{tex.strip()[:60]}` uses {left.group(0) if left else 'a command'}, "
                      f"which the converter does not know")
    try:
        import xml.dom.minidom
        xml.dom.minidom.parseString(out)
    except Exception:
        raise Failure(f"the math `{tex.strip()[:60]}` did not convert to well-formed MathML; "
                      f"`aligned` is one environment that does not, and `align` does")
    if block:
        out = out.replace("<math ", '<math class="eq" ', 1)
    return out


# ---- colours ----

def colour(text):
    """One spelling for a colour: matplotlib writes #1b9e77, R's SVG device writes
    rgb(10.588235%, 61.960784%, 46.666667%), and a palette can only be matched against one."""
    if text.startswith("#"):
        h = text[1:].lower()
        return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h)
    try:
        parts = [p.strip() for p in text[text.index("(") + 1:-1].split(",")][:3]
        vals = [round(float(p[:-1]) * 2.55) if p.endswith("%") else int(float(p)) for p in parts]
        return "#%02x%02x%02x" % tuple(max(0, min(255, v)) for v in vals)
    except (ValueError, IndexError):
        return text


def themed(svg, palette):
    """(svg, how many colours were swapped). Each palette colour becomes the stylesheet
    variable it stands for, so the figure follows the page between light and dark."""
    hits = 0

    def swap(m):
        nonlocal hits
        var = palette.get(colour(m.group(0)))
        if var is None:
            return m.group(0)
        hits += 1
        return f"var(--{var})"
    return COLOUR.sub(swap, svg), hits


# ---- citations ----

class Bibliography:
    def __init__(self, path):
        self.path, self.entries = path, None

    def load(self):
        if self.entries is not None:
            return self.entries
        self.entries = {}
        if not self.path or not self.path.exists():
            return self.entries
        try:
            import bibtexparser
            from bibtexparser.middlewares import (LatexDecodingMiddleware, SeparateCoAuthors,
                                                  SplitNameParts)
        except ModuleNotFoundError:
            raise SystemExit(f"reading {self.path.name} needs the bibtexparser package")
        lib = bibtexparser.parse_file(str(self.path), append_middleware=[
            LatexDecodingMiddleware(), SeparateCoAuthors(), SplitNameParts()])
        # a broken entry is not an error to the library, only an item on this list
        if lib.failed_blocks:
            b = lib.failed_blocks[0]
            raise SystemExit(f"{self.path.name}: an entry near line {getattr(b, 'start_line', '?')} "
                             f"could not be read: {str(getattr(b, 'error', '')).strip()[:160]}")
        for e in lib.entries:
            f = {k.lower(): v.value for k, v in e.fields_dict.items()}
            people = f.get("author") or f.get("editor") or []
            f["_family"] = [" ".join(n.von + n.last) for n in people] if isinstance(people, list) else []
            f["_given"] = [" ".join(n.first) for n in people] if isinstance(people, list) else []
            self.entries[e.key] = f
        return self.entries

    def names(self, key):
        fam = self.load()[key]["_family"] or [key]
        return fam[0] if len(fam) == 1 else f"{fam[0]} and {fam[1]}" if len(fam) == 2 else f"{fam[0]} et al."

    def year(self, key):
        return str(self.load()[key].get("year") or "n.d.")

    def reference(self, key):
        """One style, author and year. Journal styles are what Pandoc is for, and it reads
        the same .bib and the same [@key] marks."""
        f = self.load()[key]
        people = []
        for fam, giv in zip(f["_family"], f["_given"]):
            initials = "".join(w[0] for w in re.split(r"[\s.-]+", giv) if w)
            people.append(f"{fam} {initials}".strip())
        if len(people) > 8:
            people = people[:8] + ["et al."]
        bits = [html.escape(", ".join(people) or key) + f" ({html.escape(self.year(key))})."]
        if f.get("title"):
            bits.append(html.escape(str(f["title"]).rstrip(".")) + ".")
        where = f.get("journal") or f.get("booktitle") or f.get("publisher")
        if where:
            place = f"<em>{html.escape(str(where))}</em>"
            if f.get("volume"):
                place += " " + html.escape(str(f["volume"]))
                if f.get("number"):
                    place += f"({html.escape(str(f['number']))})"
            if f.get("pages"):
                place += ": " + html.escape(str(f["pages"]))
            bits.append(place + ".")
        doi = str(f.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            bits.append(f'<a href="https://doi.org/{html.escape(doi)}" rel="noreferrer">doi:{html.escape(doi)}</a>')
        elif f.get("url"):
            bits.append(f'<a href="{html.escape(str(f["url"]))}" rel="noreferrer">link</a>')
        return " ".join(bits)


# ---- a page ----

class Page:
    """One page's Markdown, taken to HTML."""

    def __init__(self, slug, page_dir, values, bib, palette):
        self.slug, self.dir, self.values, self.bib, self.palette = slug, page_dir, values, bib, palette
        self.held = []                      # what is swapped back in after Markdown has run
        self.labels = {}                    # label -> (kind, number)
        self.counts = {k: 0 for k in KINDS}
        self.cited = []

    def hold(self, html_text, block=False):
        self.held.append(html_text)
        token = f"SLOOPHELD{len(self.held) - 1:05d}X"
        return f"\n\n{token}\n\n" if block else token

    def anchor(self, label):
        return f"{self.slug}-{label.replace(':', '-')}"

    def number(self, label, kind):
        if not label:
            return None
        if label.split(":")[0] != kind:
            raise Failure(f"the label {label} is on a {KINDS[kind].lower()}; write {kind}:{label.split(':', 1)[1]}")
        if label in self.labels:
            raise Failure(f"the label {label} is used twice")
        self.counts[kind] += 1
        self.labels[label] = (kind, self.counts[kind])
        return self.counts[kind]

    def caption(self, kind, label, text, n):
        lead = f'<span class="capnum">{KINDS[kind]} {n}.</span> ' if n else ""
        return lead + self.inline(text)

    def inline(self, text):
        """A caption or a cell: math, highlights and Markdown, on one line."""
        text = self.math(text, blocks=False)
        text = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", text)
        return self.restore(inline(text))

    # math: $$...$$ {#eq:x} first, then $...$ under Pandoc's rule, which leaves $5 and $10 alone
    def math(self, text, blocks=True):
        if blocks:
            def block(m):
                label = m.group("label")
                n = self.number(label, "eq") if label else None
                mm = to_mathml(m.group("tex"), True)
                if not label:
                    return self.hold(mm, block=True)
                return self.hold(f'<div class="eqn" id="{self.anchor(label)}">{mm}'
                                 f'<span class="eqnum">({n})</span></div>', block=True)
            text = re.sub(r"\$\$(?P<tex>.+?)\$\$[ \t]*(?:\{#(?P<label>" + LABEL + r")\})?",
                          block, text, flags=re.S)
        text = text.replace("\\$", "SLOOPDOLLARX")
        text = re.sub(r"(?<![\w$])\$(?=[^\s$])((?:[^$\n]|\n(?!\n))+?)(?<=[^\s$])\$(?![\d$])",
                      lambda m: self.hold(to_mathml(m.group(1), False)), text)
        return text.replace("SLOOPDOLLARX", "$")

    # one file a step wrote, on a line by itself
    def embed(self, m):
        attrs = m.group("attrs") or ""
        lm = re.search(r"#((?:fig|tbl|eq):[^\s}]+)", attrs)
        label = lm.group(1) if lm else None
        classes = " ".join(re.findall(r"(?<!\S)\.([A-Za-z0-9_-]+)", attrs))
        rel = m.group("path")
        path = (self.dir / rel).resolve()
        if self.dir.resolve() not in path.parents:
            raise Failure(f"{rel} is outside the page directory; a page embeds its own outputs")
        if not path.is_file():
            raise Failure(f"{rel} is embedded and is not there. If a step writes it: sloop make {self.slug}")
        ext = path.suffix.lower()
        ident = f' id="{self.anchor(label)}"' if label else ""
        cap = m.group("cap").strip()
        if ext in (".csv", ".tsv"):
            n = self.number(label, "tbl")
            head = f"<figcaption>{self.caption('tbl', label, cap, n)}</figcaption>" if cap or n else ""
            return self.hold(f'<figure class="tbl {classes}"{ident}>{head}'
                             f'<div class="scroll">{self.table(path)}</div></figure>', block=True)
        if ext == ".md":
            return "\n\n" + path.read_text().strip() + "\n\n"
        if ext == ".txt":
            return self.hold(f'<pre class="out {classes}"{ident}>{html.escape(path.read_text().rstrip())}</pre>',
                             block=True)
        if ext == ".html":
            return self.hold(path.read_text(), block=True)
        n = self.number(label, "fig")
        foot = f"<figcaption>{self.caption('fig', label, cap, n)}</figcaption>" if cap or n else ""
        if ext == ".svg":
            svg = re.sub(r"^\s*<\?xml[^>]*\?>\s*|<!DOCTYPE[^>]*>\s*", "", path.read_text())
            svg, hits = themed(svg, self.palette)
            # a figure with none of the palette's colours cannot follow the theme, so the
            # stylesheet inverts it in the dark instead, as it does a bitmap
            cls = "fig" + ("" if hits or "class=" in svg else " plain")
            return self.hold(f'<figure class="{cls} {classes}"{ident}>{svg}{foot}</figure>', block=True)
        if ext in IMAGES:
            data = base64.b64encode(path.read_bytes()).decode()
            return self.hold(f'<figure class="fig plain {classes}"{ident}>'
                             f'<img alt="{html.escape(cap)}" src="data:{IMAGES[ext]};base64,{data}"/>'
                             f'{foot}</figure>', block=True)
        raise Failure(f"{rel} is embedded, and sloop does not know how to show a {ext or 'file with no extension'}")

    def table(self, path):
        """The first row is the header, a column of numbers is right-aligned, and a cell is
        inline Markdown, so the script that knows which number is notable can say so."""
        rows = list(csv.reader(io.StringIO(path.read_text()),
                               delimiter="\t" if path.suffix.lower() == ".tsv" else ","))
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            raise Failure(f"{path.name} is embedded as a table and is empty")
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        bare = lambda c: re.sub(r"[*=_`$\s]|\\[a-z]+|[{}]", "", c)
        num = [all(re.fullmatch(r"[-+−]?[\d.,]+(e[-+]?\d+)?%?|", bare(r[i]), re.I) for r in rows[1:])
               and any(bare(r[i]) for r in rows[1:]) for i in range(width)]
        cls = lambda i: ' class="num"' if num[i] else ""
        out = "<table><thead><tr>" + "".join(
            f"<th{cls(i)}>{self.inline(c)}</th>" for i, c in enumerate(rows[0])) + "</tr></thead><tbody>"
        for r in rows[1:]:
            out += "<tr>" + "".join(f"<td{cls(i)}>{self.inline(c)}</td>" for i, c in enumerate(r)) + "</tr>"
        return out + "</tbody></table>"

    # @fig:x and [@fig:x; @tbl:y], then citations, which share the mark
    def refer(self, text):
        def one(key):
            if re.fullmatch(LABEL, key):
                if key not in self.labels:
                    raise Failure(f"@{key} refers to a label this page does not have")
                kind, n = self.labels[key]
                return f'<a class="xref" href="#{self.anchor(key)}" data-xref="{self.anchor(key)}">{KINDS[kind]} {n}</a>'
            return None

        def cite(key, how):
            if key not in self.cited:
                self.cited.append(key)
            href = f'href="#{self.slug}-ref-{html.escape(key)}" data-xref="{self.slug}-ref-{html.escape(key)}"'
            if how == "text":
                return f'<a class="cite" {href}>{html.escape(self.bib.names(key))} ({self.bib.year(key)})</a>'
            return f'<a class="cite" {href}>{html.escape(self.bib.names(key))} {self.bib.year(key)}</a>'

        def bracket(m):
            keys = [k.strip().lstrip("@") for k in m.group(1).split(";")]
            if all(re.fullmatch(LABEL, k) for k in keys):
                return self.hold(", ".join(one(k) for k in keys))
            known = self.bib.load()
            for k in keys:
                if re.fullmatch(LABEL, k):
                    raise Failure(f"[{m.group(1)}] mixes a reference and a citation; write them apart")
                if k not in known:
                    raise Failure(f"[@{k}] cites a key that is not in {self.bib.path.name if self.bib.path else 'a bibliography; the notebook has none'}")
            return self.hold("(" + "; ".join(cite(k, "paren") for k in keys) + ")")

        text = re.sub(r"\[((?:@[^\s;\]]+)(?:\s*;\s*@[^\s;\]]+)*)\]", bracket, text)

        def bare(m):
            key = m.group(1).rstrip(".,:;")
            tail = m.group(1)[len(key):]
            if re.fullmatch(LABEL, key):
                return self.hold(one(key)) + tail
            if key in self.bib.load():
                return self.hold(cite(key, "text")) + tail
            return m.group(0)
        return re.sub(r"(?<![\w@/.])@([A-Za-z][\w:.-]*)", bare, text)

    def restore(self, text):
        # a block sits alone in its paragraph, and takes the paragraph with it
        text = re.sub(r"<p>\s*SLOOPHELD(\d{5})X\s*</p>", lambda m: self.held[int(m.group(1))], text)
        for _ in range(4):                  # what was held can hold something itself
            new = re.sub(r"SLOOPHELD(\d{5})X", lambda m: self.held[int(m.group(1))], text)
            if new == text:
                break
            text = new
        return text

    def render(self, body):
        for line in body.split("\n"):
            if line.startswith("<math") and 'display="block"' not in line:
                raise Failure("inline <math> may not start a line: Markdown treats it as block "
                              f"level and splits the paragraph.\n  {line[:70]}")
        # code is set aside whole, so nothing below reads a $ or an @ inside it
        code = []

        def aside(m):
            code.append(m.group(0))
            return f"SLOOPCODE{len(code) - 1:05d}X"
        body = re.sub(r"(?ms)^(```|~~~).*?^\1[ \t]*$", aside, body)
        body = re.sub(r"(`+)(?!`).+?(?<!`)\1(?!`)", aside, body)

        lines = body.split("\n")
        for i, line in enumerate(lines):
            m = EMBED.match(line)
            if m and not re.match(r"[a-z]+:", m.group("path")) \
                    and (i == 0 or not lines[i - 1].strip()) \
                    and (i + 1 == len(lines) or not lines[i + 1].strip()):
                lines[i] = self.embed(m)
            else:
                # a table typed into the page takes its caption on the line beneath it
                t = re.match(r":\s+(.*?)\s*(?:\{#(" + LABEL + r")\})?\s*$", line)
                above = next((l for l in reversed(lines[:i]) if l.strip()), "")
                if t and above.lstrip().startswith("|"):
                    label = t.group(2)
                    n = self.number(label, "tbl")
                    ident = f' id="{self.anchor(label)}"' if label else ""
                    lines[i] = self.hold(f'<p class="tblcap"{ident}>{self.caption("tbl", label, t.group(1), n)}</p>',
                                         block=True)
        body = "\n".join(lines)
        body = self.math(body)
        body = self.refer(body)
        body = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", body)
        body = re.sub(r"SLOOPCODE(\d{5})X", lambda m: code[int(m.group(1))], body)
        out = markdown_lib().markdown(body, extensions=["tables", "attr_list", "fenced_code", "md_in_html"])
        out = self.restore(out)
        out = self.fill(out)
        if self.cited:
            keys = sorted(self.cited, key=lambda k: (self.bib.names(k).lower(), self.bib.year(k)))
            out += ('<section class="refs"><p class="label">References</p><ol>'
                    + "".join(f'<li id="{self.slug}-ref-{html.escape(k)}">{self.bib.reference(k)}</li>' for k in keys)
                    + "</ol></section>")
        return out

    def fill(self, text):
        # keys come from a script's own naming, so `1e+08` has to be referenceable, and a
        # value can be a whole SVG, so neither key nor value may reach a regex raw
        for key in set(VALUE.findall(text)):
            if key not in self.values:
                raise Failure(f"no value for {{{{{key}}}}}. If a step writes it: sloop make {self.slug}")
        text = VALUE.sub(lambda m: self.values[m.group(1)], text)
        # anything still in braces would otherwise ship as literal text on the page
        stray = re.search(r"\{\{.{0,40}?\}\}", text)
        if stray:
            raise Failure(f"{stray.group(0)} is not a usable key. Letters, digits, and _ . + - only.")
        return text


def read_values(page_dir):
    """Every values file in a page: one key and one value per line, separated by a tab.
    Any step may write one, and a key written twice is refused."""
    vals, source = {}, {}
    files = sorted(p for p in page_dir.iterdir()
                   if p.is_file() and (p.name == "values.tsv" or p.name.endswith(".values.tsv")))
    for f in files:
        for line in f.read_text().splitlines():
            if "\t" not in line:
                continue
            k, v = line.split("\t", 1)
            k = k.strip()
            if k in vals:
                raise Failure(f"the value {k} is written by both {source[k]} and {f.name}")
            vals[k], source[k] = v.strip(), f.name
    return vals
