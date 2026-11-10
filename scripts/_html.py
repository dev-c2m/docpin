"""HTML → markdown-ish text with a heading outline, and section cutting.

Both HTML and markdown sources end up as markdown text whose headings may
carry `{#id}` markers, so one `cut_section` works for every source.
Standard library only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from _common import est_tokens, tokens

SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside",
             "svg", "button", "form", "template", "iframe", "select"}
# Chrome of common doc generators (Sphinx, MkDocs Material, VitePress, Docusaurus).
SKIP_CLASSES = {
    "sphinxsidebar", "sphinxsidebarwrapper", "related", "headerlink", "viewcode-link",
    "md-sidebar", "md-header", "md-footer", "md-nav", "md-source", "md-tabs",
    "sidebar", "toc", "breadcrumbs", "breadcrumb", "navbar", "pagination-nav", "prev-next",
    "VPSidebar", "VPNav", "VPLocalNav", "VPDocAside", "VPDocFooter", "VPFooter",
    "theme-doc-sidebar-container", "theme-doc-toc-desktop", "theme-doc-footer",
    "table-of-contents", "edit-this-page", "skip-link",
}
SKIP_ROLES = {"navigation", "search", "banner", "contentinfo", "complementary"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "ul", "ol", "table", "blockquote",
              "dl", "dd", "figure", "figcaption", "details", "summary", "hr"}
HEADINGS = {f"h{i}": i for i in range(1, 7)}
VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "col", "area", "base", "embed"}
HEADERLINK = re.compile(r"[¶§#]\s*$|\s*Permalink.*$")


@dataclass
class Heading:
    level: int
    text: str
    id: str | None
    kind: str  # heading | dt
    line: int  # index into the markdown lines


class _ToMarkdown(HTMLParser):
    def __init__(self, scope: str | None):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.outline: list[Heading] = []
        self.buf: list[str] = []
        # scope: "main"/"article" (tag) or "role=main" (attribute on any tag)
        self.scope = scope
        self.scope_tag: str | None = None
        self.scope_depth = 0 if scope else 1
        self.skip_tag: str | None = None   # tag name of the element being skipped
        self.skip_depth = 0
        self.pre = 0
        self.cap: dict | None = None
        self.pending_id: str | None = None
        self.last_level = 1
        self.title: str | None = None
        self._in_title = False
        self._title_buf: list[str] = []
        self._in_code = 0

    # -- helpers
    @property
    def active(self) -> bool:
        return self.scope_depth > 0 and self.skip_depth == 0

    @staticmethod
    def _should_skip(tag: str, a: dict) -> bool:
        if tag in SKIP_TAGS:
            return True
        if (a.get("role") or "") in SKIP_ROLES:
            return True
        classes = set((a.get("class") or "").split())
        return bool(classes & SKIP_CLASSES)

    def _is_scope(self, tag: str, a: dict) -> bool:
        if self.scope == "role=main":
            return a.get("role") == "main"
        return tag == self.scope

    def flush(self):
        text = "".join(self.buf)
        self.buf = []
        if self.pre:
            if text:
                self.lines.extend(text.split("\n"))
            return
        text = re.sub(r"[ \t\r\n]+", " ", text).strip()
        if text:
            self.lines.append(text)

    def blank(self):
        self.flush()
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag in VOID:
            if tag == "br" and self.active and self.cap is None:
                self.flush()
            return
        # nesting of the element being skipped / the scope element
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if self.scope_tag is not None and tag == self.scope_tag:
            self.scope_depth += 1
        elif self.scope and self.scope_tag is None and self._is_scope(tag, a):
            self.scope_tag = tag
            self.scope_depth = 1
        if self._should_skip(tag, a):
            self.skip_tag, self.skip_depth = tag, 1
            return
        if not self.active:
            return
        if tag == "section" and a.get("id"):
            self.pending_id = a["id"]
        if tag in HEADINGS or tag == "dt":
            self.blank()
            if tag == "dt":
                level = min(self.last_level + 1, 6)
                kind = "dt"
            else:
                level = HEADINGS[tag]
                kind = "heading"
            hid = a.get("id") or (self.pending_id if kind == "heading" else None)
            self.pending_id = None
            self.cap = {"tag": tag, "level": level, "id": hid, "kind": kind, "text": []}
            return
        if self.cap is not None:
            if a.get("id") and not self.cap["id"] and tag == "a" and a.get("name"):
                self.cap["id"] = a["id"]
            return
        if tag == "pre":
            self.blank()
            self.lines.append("```")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self._in_code += 1
            self.buf.append("`")
        elif tag == "li":
            self.flush()
            self.buf.append("- ")
        elif tag == "tr":
            self.flush()
        elif tag in ("td", "th"):
            self.buf.append(" | ")
        elif tag in BLOCK_TAGS:
            self.blank()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            self.title = re.sub(r"\s+", " ", "".join(self._title_buf)).strip()
            return
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if self.scope_tag is not None and tag == self.scope_tag and self.scope_depth > 0:
            self.scope_depth -= 1
        if not self.active and self.cap is None:
            return
        if self.cap is not None and tag == self.cap["tag"]:
            text = re.sub(r"\s+", " ", "".join(self.cap["text"])).strip()
            text = HEADERLINK.sub("", text).strip()
            c, self.cap = self.cap, None
            if not text:
                return
            if c["kind"] == "heading":
                self.last_level = c["level"]
            marker = f" {{#{c['id']}}}" if c["id"] else ""
            self.outline.append(Heading(c["level"], text, c["id"], c["kind"], len(self.lines)))
            self.lines.append("#" * c["level"] + " " + text + marker)
            self.lines.append("")
            return
        if self.cap is not None:
            return
        if tag == "pre" and self.pre:
            self.flush()
            self.pre -= 1
            self.lines.append("```")
            self.lines.append("")
        elif tag == "code" and self._in_code:
            self._in_code -= 1
            self.buf.append("`")
        elif tag in ("li", "tr", "dd"):
            self.flush()
        elif tag in BLOCK_TAGS:
            self.blank()

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
            return
        if not self.active:
            return
        if self.cap is not None:
            self.cap["text"].append(data)
        else:
            self.buf.append(data)


def _pick_scope(html: str) -> str | None:
    for tag in ("main", "article"):
        if re.search(rf"<{tag}[\s>]", html, re.I):
            return tag
    if re.search(r'role=["\']main["\']', html, re.I):
        return "role=main"  # e.g. Sphinx: <div class="body" role="main">
    return None


def html_to_md(html: str) -> tuple[str, list[Heading], str | None]:
    """Return (markdown text, outline, <title>)."""
    p = _ToMarkdown(_pick_scope(html))
    p.feed(html)
    p.close()
    p.flush()
    md = "\n".join(p.lines).strip("\n") + "\n"
    return md, p.outline, p.title


# ---------------------------------------------------------------- markdown

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*(?:\{#([^}\s]+)\})?\s*#*\s*$")


def md_outline(md: str) -> list[Heading]:
    out, fence = [], None
    for i, line in enumerate(md.splitlines()):
        s = line.lstrip()
        m = re.match(r"^(```+|~~~+)", s)
        if m:
            if fence is None:
                fence = m.group(1)[0] * 3
            elif s.startswith(fence):
                fence = None
            continue
        if fence:
            continue
        hm = _MD_HEADING.match(line)
        if hm:
            text = HEADERLINK.sub("", hm.group(2)).strip()
            out.append(Heading(len(hm.group(1)), text, hm.group(3), "heading", i))
    return out


def _norm(s: str) -> str:
    return " ".join(tokens(s))


def slug(s: str) -> str:
    return "-".join(tokens(s))


def find_heading(outline: list[Heading], anchor: str | None = None,
                 heading_path: str | None = None) -> Heading | None:
    if anchor:
        for h in outline:
            if h.id == anchor:
                return h
        a = slug(anchor)
        for h in outline:
            if slug(h.text) == a:
                return h
    if heading_path:
        hp, nth = heading_path, 1
        m = re.search(r"#(\d+)$", hp)
        if m:
            hp, nth = hp[: m.start()], int(m.group(1))
        parts = [_norm(p) for p in hp.split(">") if p.strip()]
        if not parts:
            return None
        hits = []
        for i, h in enumerate(outline):
            if _norm(h.text) != parts[-1]:
                continue
            # ancestors: walk back collecting headings with decreasing level
            chain, lvl = [], h.level
            for prev in reversed(outline[:i]):
                if prev.level < lvl:
                    chain.append(_norm(prev.text))
                    lvl = prev.level
            want = parts[:-1]
            it = iter(chain)
            if all(any(w == c for c in it) for w in reversed(want)):
                hits.append(h)
        if len(hits) >= nth:
            return hits[nth - 1]
    return None


def heading_containing(md: str, outline: list[Heading], term: str) -> Heading | None:
    """The innermost heading whose section contains the first occurrence of `term`
    (e.g. a code identifier listed in a table under a generic heading)."""
    if not term:
        return None
    lines = md.splitlines()
    pat = re.compile(re.escape(term))
    hit = next((i for i, l in enumerate(lines) if pat.search(l)), None)
    if hit is None:
        pat = re.compile(re.escape(term), re.I)
        hit = next((i for i, l in enumerate(lines) if pat.search(l)), None)
    if hit is None:
        return None
    before = [h for h in outline if h.line <= hit]
    return before[-1] if before else None


@dataclass
class Section:
    status: str          # OK | TRUNCATED | HEADING_MISSING
    text: str
    heading: Heading | None


def cut_section(md: str, outline: list[Heading], anchor: str | None = None,
                heading_path: str | None = None, max_tokens: int = 2500) -> Section:
    lines = md.splitlines()
    if not anchor and not heading_path:
        body = md
        h = None
        start_i = 0
        end = len(lines)
    else:
        h = find_heading(outline, anchor, heading_path)
        if h is None:
            toc = "\n".join("  " * (x.level - 1) + "- " + x.text for x in outline)
            return Section("HEADING_MISSING", "Headings on this page:\n" + toc, None)
        start_i = outline.index(h)
        end = len(lines)
        for nxt in outline[start_i + 1:]:
            if nxt.level <= h.level:
                end = nxt.line
                break
        body = "\n".join(lines[h.line:end]).strip("\n") + "\n"
    if est_tokens(body) <= max_tokens:
        return Section("OK", body, h)
    # Too long: keep the intro up to the first child heading, then list children.
    children = [x for x in outline[(start_i + 1) if h else 0:]
                if x.line < end and (h is None or x.level > h.level)]
    first_child_line = children[0].line if children else end
    intro_start = h.line if h else 0
    intro = "\n".join(lines[intro_start:first_child_line]).strip("\n")
    budget = max_tokens * 3
    if len(intro.encode("utf-8")) > budget:
        intro = intro.encode("utf-8")[:budget].decode("utf-8", "ignore") + "\n…"
    min_lvl = min((c.level for c in children), default=0)
    toc = "\n".join("  " * (c.level - min_lvl) + "- " + c.text for c in children)
    text = intro + "\n\n[TRUNCATED] Sub-sections (request one with --heading):\n" + toc + "\n"
    return Section("TRUNCATED", text, h)


def signature_params(sig: str) -> list[str]:
    """Parameter names from a signature-like text: `f(a, *, b: int = 1, **kw)`."""
    start = sig.find("(")
    if start < 0:
        return []
    depth, cur, parts = 0, [], []
    for ch in sig[start + 1:]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    names = []
    for p in parts:
        m = re.match(r"\s*\*{0,2}\s*([A-Za-z_]\w*)", p)
        if m and m.group(1) not in ("self", "cls") and m.group(1) not in names:
            names.append(m.group(1))
    return names[:20]
