#!/usr/bin/env python3
"""Build a docpin index mechanically (no LLM).

Sources: llms.txt, objects.inv (Sphinx / mkdocstrings), page HTML/markdown
headings. Summaries come only from llms.txt link notes; otherwise left empty.

Usage:
  python build_index.py --id pypi:pydantic --base https://docs.pydantic.dev/latest/ \
      --out indexes/pypi/pydantic [--ver 2] [--merge] [--max-pages 200]

Multi-version: build the newest version first, then re-run with --merge and
--ver/--ver-path for each older version, e.g.
  --base https://pydantic.dev/docs/validation/ --ver 1 --ver-path 1.10 --merge
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
import urllib.parse
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    PAGES_COLS, SYMBOLS_COLS, FetchError, VERSION, clean_cell, denied, dump_value, est_tokens,
    http_get, load_index, write_tsv,
)
from _html import html_to_md, md_outline, signature_params  # noqa: E402

SUMMARY_BAD = re.compile(r"`|<|>|\[|\]\(|://")
KEYWORD_OK = re.compile(r"^[\w.+#@/ -]{1,80}$")
SYMBOL_ROLES = {
    "py:class", "py:function", "py:method", "py:attribute", "py:property", "py:exception",
    "py:data", "py:module", "py:classmethod", "py:staticmethod", "py:decorator",
    "js:function", "js:class", "js:method", "js:attribute", "js:module",
    "c:function", "cpp:function", "cpp:class",
}


def log(msg: str):
    print(msg, file=sys.stderr)


class Builder:
    def __init__(self, args):
        self.a = args
        self.fixtures = args.fixtures
        self.allowed: list[str] = list(args.allowed or [])
        self.delay = 0 if args.fixtures else args.delay
        self.robots_disallow: list[str] = []

    # ---------------------------------------------------------------- fetch
    def get(self, url: str, accept: str | None = None):
        if self.delay:
            time.sleep(self.delay)
        return http_get(url, allowed=self.allowed, accept=accept, fixtures=self.fixtures,
                        allow_private=self.a.allow_private)

    def try_get(self, url: str, accept: str | None = None):
        for attempt in (1, 2):
            try:
                return self.get(url, accept)
            except FetchError as e:
                # one retry for transient failures (timeouts, 5xx, rate limits)
                if attempt == 1 and e.status in ("OFFLINE", "BLOCKED") and "content-type" not in e.detail:
                    time.sleep(2 if not self.fixtures else 0)
                    continue
                log(f"  - {url}: {e.status} {e.detail}")
                return None

    # ----------------------------------------------------------------- base
    def resolve_base(self) -> str:
        base = self.a.base if self.a.base.endswith("/") else self.a.base + "/"
        host = urllib.parse.urlsplit(base).hostname or ""
        if not self.allowed:
            self.allowed = [host]
        # A redirect to another host is not followed silently: the user must
        # confirm the new host by re-running with it. Probe the version root
        # (the real docs root); a bare base like ".../en/" may redirect elsewhere.
        ver_root = base + (self.a.ver_path.strip("/") + "/" if self.a.ver_path else "")
        last: FetchError | None = None
        for probe in dict.fromkeys([ver_root, base]):  # a docs root need not be a page itself
            try:
                r = self.get(probe)
            except FetchError as e:
                if e.status == "REDIRECT":
                    new_host = urllib.parse.urlsplit(e.detail).hostname
                    raise SystemExit(f"{probe} redirects to another host: {e.detail}\n"
                                     f"re-run with --base {e.detail} (allowed domain becomes {new_host})")
                last = e
                continue
            # Same-host redirects (e.g. docs root -> a landing page) do not move the base.
            if r.url.rstrip("/") != probe.rstrip("/"):
                log(f"note: {probe} redirects to {r.url} (same host, base kept)")
            return base
        log(f"note: no page at the docs root ({last.status if last else '?'}); continuing with sources")
        return base

    def load_robots(self, base: str):
        p = urllib.parse.urlsplit(base)
        r = self.try_get(f"{p.scheme}://{p.netloc}/robots.txt")
        if not r or r.content_type not in ("text/plain", "application/octet-stream"):
            return
        applies = False
        for line in r.text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                applies = v == "*" or v.lower().startswith("docpin")
            elif k == "disallow" and applies and v:
                self.robots_disallow.append(v)

    def robots_ok(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or "/"
        return not any(path.startswith(d) for d in self.robots_disallow)

    # --------------------------------------------------------------- llms.txt
    def llms_txt(self, root: str) -> list[dict]:
        p = urllib.parse.urlsplit(root)
        candidates = [root + "llms.txt", f"{p.scheme}://{p.netloc}/llms.txt"]
        for url in dict.fromkeys(candidates):
            r = self.try_get(url)
            if not r or r.content_type not in ("text/plain", "text/markdown", "text/x-markdown"):
                continue
            text = r.text()
            head = text.splitlines()[:80]
            # Some sites put a preamble before the H1; accept an H1 near the top
            # or a list of markdown links (preamble text is ignored, never followed).
            has_h1 = any(l.startswith("# ") for l in head)
            n_links = len(re.findall(r"^\s*[-*]\s*\[[^\]]+\]\([^)\s]+\)", text, re.M))
            if not (has_h1 or n_links >= 3) or text.lstrip().lower().startswith(("<!doctype", "<html")):
                log(f"  - {url}: not an llms.txt")
                continue
            log(f"llms.txt: {url}")
            return self._parse_llms(text, r.url)
        return []

    @staticmethod
    def _parse_llms(text: str, src: str) -> list[dict]:
        links, section = [], ""
        for line in text.splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
                continue
            m = re.match(r"^\s*[-*]\s*\[([^\]]+)\]\(([^)\s]+)\)\s*(?::\s*(.*))?$", line)
            if m:
                links.append({"title": m.group(1).strip(),
                              "url": urllib.parse.urljoin(src, m.group(2)),
                              "notes": (m.group(3) or "").strip(),
                              "section": section})
        return links

    # ------------------------------------------------------------ objects.inv
    def sitemap(self, url: str, ver_root: str, include: list[str]) -> list[str]:
        """Page URLs from sitemap.xml (one level of sitemap index), under ver_root/include."""
        out: list[str] = []
        todo, seen = [url], set()
        while todo and len(seen) < 20:
            u = todo.pop(0)
            if u in seen:
                continue
            seen.add(u)
            r = self.try_get(u)
            if not r or r.content_type not in ("application/xml", "text/xml", "text/plain"):
                continue
            text = r.text()
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
            if "<sitemapindex" in text:
                todo += locs
                continue
            for loc in locs:
                if not loc.startswith(ver_root):
                    continue
                rest = loc[len(ver_root):]
                if include and not any(rest.startswith(p) for p in include):
                    continue
                out.append(loc)
        if out:
            log(f"sitemap: {url} ({len(out)} pages)")
        return out

    def objects_inv(self, root: str, url: str | None = None) -> list[dict]:
        url = url or root + "objects.inv"
        r = self.try_get(url)
        if not r:
            return []
        data = r.body
        lines, pos = [], 0
        for _ in range(4):
            nl = data.find(b"\n", pos)
            if nl < 0:
                return []
            lines.append(data[pos:nl].decode("utf-8", "replace"))
            pos = nl + 1
        if not lines[0].startswith("# Sphinx inventory version 2"):
            log(f"  - {url}: unsupported inventory header {lines[0]!r}")
            return []
        try:
            body = zlib.decompress(data[pos:]).decode("utf-8", "replace")
        except zlib.error as e:
            log(f"  - {url}: bad zlib body: {e}")
            return []
        log(f"objects.inv: {url}")
        by_url: dict[str, dict] = {}
        for line in body.splitlines():
            m = re.match(r"(.+?)\s+(\S+?:\S+)\s+(-?\d+)\s+(\S*)\s+(.*)", line)
            if not m:
                continue
            name, role, _prio, uri, disp = m.groups()
            if uri.endswith("$"):
                uri = uri[:-1] + name
            if disp == "-":
                disp = name
            full = urllib.parse.urljoin(url, uri)
            entry = {"name": name, "role": role, "url": full, "display": disp}
            prev = by_url.get(full)
            # Re-exports point to the same place: keep the canonical name
            # (equal to the anchor), else the shortest.
            if prev is None or rank_name(entry, full) < rank_name(prev, full):
                by_url[full] = entry
        return list(by_url.values())

    # ----------------------------------------------------------- fetch probe
    def probe_fetch(self, url: str) -> list[str]:
        methods = []
        r = self.try_get(url, accept="text/markdown")
        if r and r.content_type in ("text/markdown", "text/x-markdown"):
            methods.append("accept-md")
        if url.endswith("/"):
            r = self.try_get(url + "index.md")
            if r and r.content_type in ("text/markdown", "text/x-markdown", "text/plain"):
                methods.append("index-md")
        r = self.try_get(url.rstrip("/") + ".md")
        if r and r.content_type in ("text/markdown", "text/x-markdown", "text/plain"):
            methods.append("dot-md")
        methods.append("html")
        return methods

    # ------------------------------------------------------------- outline
    def page_outline(self, url: str):
        """Fetch a page as HTML (ids + dt live only there).

        Returns (outline, title, final_url) or None.
        """
        r = self.try_get(url)
        if not r:
            return None
        if r.content_type in ("text/markdown", "text/x-markdown", "text/plain"):
            md = r.text()
            return md_outline(md), None, r.url, md
        md, outline, title = html_to_md(r.text())
        return outline, title, r.url, md

    # ---------------------------------------------------------------- build
    def run(self):
        a = self.a
        base = self.resolve_base()
        ver_root = base + (a.ver_path.strip("/") + "/" if a.ver_path else "")
        self.load_robots(base)

        links = self.llms_txt(ver_root) if not a.no_llms else []
        inv = []
        if not a.no_inv:
            for inv_url in (a.inv or [None]):
                inv += self.objects_inv(ver_root, inv_url)
        site_urls = []
        if a.sitemap:
            sm = ver_root + "sitemap.xml" if a.sitemap == "auto" else a.sitemap
            site_urls = self.sitemap(sm, ver_root, a.include or [])

        pages: dict[str, dict] = {}

        def rel(url: str) -> str | None:
            url = url.split("#", 1)[0]
            if not url.startswith(ver_root):
                return None
            path = url[len(ver_root):]
            # '.md' twins and 'index.html' map to the page; plain '.html' files
            # (Sphinx) are real paths and must be kept.
            for suffix in (".md", "index.html"):
                if path.endswith(suffix):
                    path = path[: -len(suffix)]
                    break
            if path == "index" or path.endswith("/index"):
                path = path[: -len("index")]
            return path

        for u in site_urls:
            path = rel(u)
            if path is not None and self.robots_ok(u):
                pages.setdefault(path, {"path": path, "title": "", "summary": "",
                                        "cat": first_segment(path), "keywords": []})

        for l in links:
            path = rel(l["url"])
            if path is None or not self.robots_ok(l["url"]):
                continue
            pages.setdefault(path, {
                "path": path, "title": l["title"],
                "summary": sanitize_summary(l["notes"]),
                # path segment, not the llms.txt section ("Table of Contents" etc. say little)
                "cat": first_segment(path),
                "keywords": [],
            })

        symbols = []
        param_map: dict[str, list[str]] = {}  # documented parameters -> parent symbol
        for s in inv:
            path = rel(s["url"])
            if path is None:
                continue
            if s["role"] == "py:parameter":
                # e.g. sqlalchemy.create_engine.params.pool_pre_ping -> create_engine: pool_pre_ping
                parent, _, pname = s["name"].rpartition(".")
                parent = parent[: -len(".params")] if parent.endswith(".params") else parent
                pname = pname.lstrip("*")  # '*args', '**kw' -> args, kw
                if pname and pname not in param_map.get(parent, []):
                    param_map.setdefault(parent, []).append(pname)
                continue
            if s["role"] in ("std:label", "std:doc"):  # section labels: page discovery only
                pages.setdefault(path, {"path": path, "title": "", "summary": "",
                                        "cat": first_segment(path), "keywords": []})
                continue
            if s["role"] not in SYMBOL_ROLES:
                continue
            anchor = s["url"].split("#", 1)[1] if "#" in s["url"] else ""
            symbols.append({"symbol": s["name"], "path": path, "anchor": anchor, "role": s["role"]})
            pages.setdefault(path, {"path": path, "title": "", "summary": "",
                                    "cat": first_segment(path), "keywords": []})

        for path in [p for p in pages if is_special_page(p)]:
            pages.pop(path)
        symbols = [s for s in symbols if s["path"] in pages]
        if not pages:
            raise SystemExit("no pages found (no llms.txt / objects.inv under the base URL). "
                             "Other sources come in later versions.")

        # Visit pages: verify they exist, collect titles, headings, anchors.
        # llms.txt pages first (curated, most useful), then sitemap pages, then the rest.
        llms_first = [p for p in (rel(l["url"]) for l in links) if p in pages]
        site_first = [p for p in (rel(u) for u in site_urls) if p in pages]
        paths = list(dict.fromkeys(llms_first + site_first + sorted(pages)))[: a.max_pages]
        if len(pages) > a.max_pages:
            log(f"note: {len(pages)} pages, visiting first {a.max_pages} (raise --max-pages)")
        outlines, mds = {}, {}
        missing = []
        for i, path in enumerate(paths, 1):
            url = ver_root + path
            res = self.page_outline(url)
            if res is None:
                missing.append(path)
                continue
            outline, title, final, md = res
            canon = rel(final)
            if canon is not None and canon != path:
                # e.g. llms.txt linked 'concepts/models.md', the page lives at 'concepts/models/'
                moved = pages.pop(path)
                if canon in pages:
                    for k in ("title", "summary"):
                        pages[canon][k] = pages[canon][k] or moved[k]
                else:
                    pages[canon] = {**moved, "path": canon}
                path = canon
            outlines[path] = outline
            mds[path] = md
            pg = pages[path]
            h1 = next((h.text for h in outline if h.level == 1), None)
            pg["h1"], pg["html_title"] = h1, title
            pg["title"] = pg["title"] or h1 or (title or path)
            pg["keywords"] = dedupe(heading_keywords(outline) + code_terms(md))
            if i % 25 == 0:
                log(f"  visited {i}/{len(paths)}")
        for path in missing:
            pages.pop(path, None)
        unvisited = set(pages) - set(outlines)
        fix_site_name_titles(pages)

        # Resolve symbol heading paths / kinds from page outlines.
        sym_rows = []
        unreachable_syms = unverified_syms = anchor_missing = 0
        for s in symbols:
            if s["path"] not in pages:
                unreachable_syms += 1  # page checked and missing (404): stale inventory entry
                continue
            outline = outlines.get(s["path"])
            if outline is None:
                unverified_syms += 1  # page not visited (beyond --max-pages)
                continue
            kind, hp, params = "heading", "", []
            if True:
                h = next((h for h in outline if h.id == s["anchor"]), None)
                if h is None:
                    anchor_missing += 1  # anchor not on the page: stale inventory entry
                    continue
                kind = h.kind
                last = s["symbol"].rsplit(".", 1)[-1]
                params = list(param_map.get(s["symbol"], []))[:30]
                if h.kind == "dt":
                    params += [p for p in signature_params(h.text) if p not in params]
                    # dt text is a whole signature: locate by parent headings + short name
                    parent = next((p for p in reversed(outline[: outline.index(h)])
                                   if p.kind == "heading"), None)
                    hp = (heading_path(outline, parent) + ">" if parent else "") + last
                else:
                    hp = heading_path(outline, h)
                # option-like identifiers written as `code` in the section (e.g. exclude_none);
                # skipped for classes/modules whose sections cover many unrelated names
                if s["role"] not in ("py:class", "py:module", "js:class", "js:module", "cpp:class"):
                    for t in code_terms(section_text(mds[s["path"]], outline, h)):
                        if (t not in params and t != last and len(params) < 12
                                and t[0].islower() and t not in ("self", "cls")):
                            params.append(t)
            sym_rows.append({"ver": a.ver, "symbol": s["symbol"], "kind": kind, "path": s["path"],
                             "anchor": s["anchor"], "heading_path": hp, "params": ",".join(params)})

        page_rows = []
        for path, pg in sorted(pages.items()):
            if path in unvisited:
                continue  # beyond --max-pages: not verified, not written
            page_rows.append({"ver": a.ver, "path": path, "cat": pg["cat"] or "docs",
                              "title": clean_title(pg["title"]), "summary": pg["summary"],
                              "keywords": fit_keywords(pg, a.ver)})

        # Probe a few pages (llms.txt pages first: they are the most "documenty").
        llms_paths = [p for p in (rel(l["url"]) for l in links) if p in outlines]
        probe = list(dict.fromkeys(llms_paths + sorted(outlines)))[:3]
        fetch: list[str] = []
        for path in probe:
            for m in self.probe_fetch(ver_root + path):
                if m not in fetch:
                    fetch.append(m)
        fetch = [m for m in fetch if m != "html"] + ["html"]
        log(f"fetch methods: {fetch}")

        # "Complete" licenses negative answers ("X is not a documented API"), so be strict:
        # every symbol page checked; pages that were checked and are gone (stale inventory
        # entries) at most 2%; stale anchors at most 20%. An inventory of another version
        # (most pages missing) is never complete.
        total = len(symbols)
        complete = (bool(sym_rows) and unverified_syms == 0
                    and unreachable_syms <= 0.02 * total
                    and anchor_missing <= 0.2 * total)
        if symbols and not complete:
            log(f"note: symbol inventory NOT marked complete for ver {a.ver} (unverified: "
                f"{unverified_syms}, missing pages: {unreachable_syms}, missing anchors: {anchor_missing})")
        self.write(base, page_rows, sym_rows, fetch, complete=complete)
        log(f"pages: {len(page_rows)}  symbols: {len(sym_rows)}  dropped (unreachable): {len(missing)}")

    # ---------------------------------------------------------------- write
    def write(self, base, page_rows, sym_rows, fetch, complete):
        a = self.a
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        ver_map = {a.ver: a.ver_path.strip("/") if a.ver_path else ""}
        caps = []
        complete_vers = [a.ver] if complete else []
        if not a.merge and (out / "INDEX.md").exists():
            # Rebuild: keep hand-written Capabilities, drop old tables entirely.
            try:
                caps = load_index(out, with_tables=False).capabilities
            except (ValueError, OSError):
                caps = []
            for old_file in list(out.glob("symbols*.tsv")) + [out / "pages.tsv"]:
                old_file.unlink(missing_ok=True)
        if a.merge and (out / "INDEX.md").exists():
            old = load_index(out)
            old_map = {str(k): v for k, v in (old.meta.get("ver_map") or {}).items()}
            ver_map = {**old_map, **ver_map}
            caps = old.capabilities
            page_rows = merge_rows(old.pages, page_rows, a.ver, list(ver_map),
                                   ("path",), same=("path", "title"), combine=combine_pages)
            sym_rows = merge_rows(old.symbols, sym_rows, a.ver, list(ver_map),
                                  ("symbol", "path", "anchor"))
            old_cs = old.meta.get("complete_symbols")
            old_vers = list(old_map) if old_cs is True else [str(v) for v in old_cs or []]
            complete_vers = [v for v in old_vers if v != a.ver] + complete_vers
            fetch = old.meta.get("fetch") or fetch
        cats = sorted({r["cat"] for r in page_rows})
        meta = {
            "id": a.id, "schema": 1, "status": "active", "replaced_by": None, "lang": a.lang,
            "aliases": a.aliases or [],
            "base": base,
            "url_template": "{base}{ver_path}/{path}" if len(ver_map) > 1 or a.ver_path else "{base}{path}",
            "ver_map": ver_map, "fetch": fetch, "heading_slug": a.heading_slug,
            "complete_symbols": complete_vers, "allowed_domains": self.allowed,
            "allow_private_network": bool(a.allow_private),
            "maintainers": a.maintainers or [],
            "generator": {"version": VERSION, "llm": "none"},
            "checked": dt.date.today().isoformat(),
        }
        lines = ["---"] + [f"{k}: {dump_value(v)}" for k, v in meta.items()] + ["---", "## Capabilities"]
        lines += [f"- {c}" for c in caps] or ["<!-- add 10-20 one-line capabilities (summarize_index.py or by hand) -->"]
        lines += ["", "## Categories", " · ".join(cats), ""]
        # The HTML comment above would fail validation; drop it when empty.
        text = "\n".join(l for l in lines if not l.startswith("<!--"))
        (out / "INDEX.md").write_text(text, encoding="utf-8")
        write_tsv(out / "pages.tsv", PAGES_COLS, page_rows)
        if sym_rows:
            write_tsv(out / "symbols.tsv", SYMBOLS_COLS, sym_rows)
        log(f"wrote {out}")


# ------------------------------------------------------------------ helpers

def rank_name(entry: dict, url: str) -> tuple:
    anchor = url.split("#", 1)[1] if "#" in url else ""
    return (entry["name"] != anchor, len(entry["name"]), entry["name"])


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:30]


def first_segment(path: str) -> str:
    seg = path.strip("/").split("/", 1)[0]
    return slugify(seg) or "docs"


def fix_site_name_titles(pages: dict) -> None:
    """Some layouts put the site name in <h1> on every page (e.g. SQLAlchemy).
    When one h1 repeats on many pages, use the page <title> minus the site suffix."""
    counts: dict[str, int] = {}
    for pg in pages.values():
        if pg.get("h1"):
            counts[pg["h1"]] = counts.get(pg["h1"], 0) + 1
    repeated = {t for t, n in counts.items() if n >= 5 and n >= 0.3 * len(pages)}
    for pg in pages.values():
        if pg.get("title") in repeated and pg.get("html_title"):
            t = re.split(r"\s+[—–|-]\s+", pg["html_title"])[0].strip()
            if t:
                pg["title"] = t


SPECIAL_PAGES = re.compile(r"^(genindex|py-modindex|search|_modules|_sources|404)(/|$)")
# Pages that list people (authors, maintainers, contributors): no API content, and
# their headings are often personal names, which indexes must not carry.
PEOPLE_PAGES = re.compile(r"(^|/)[\w-]*(authors?|people|maintainers|contributors|team-members)(/|\.html|$)", re.I)


def is_special_page(path: str) -> bool:
    """Generated index/search pages and people-listing pages: not indexed."""
    return bool(SPECIAL_PAGES.match(path) or PEOPLE_PAGES.search(path))


def clean_title(t: str) -> str:
    t = re.sub(r"\s*\{#[^}]*\}\s*", " ", t or "")   # VitePress/MkDocs heading ids
    t = t.replace("\\", "")                         # markdown escapes: \<script setup\>
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+[-|–—]\s+[^-|–—]+$", "", t) if len(t) > 60 else t  # drop " - Site Name"
    t = SUMMARY_BAD.sub("", t)
    return "(title removed)" if denied(t) else t[:100]


def sanitize_summary(s: str) -> str:
    s = SUMMARY_BAD.sub("", re.sub(r"\s+", " ", s or "")).strip()
    return "" if denied(s) else s[:100]


# Section names that appear on every API page and carry no meaning on their own.
GENERIC_HEADINGS = {
    "attributes", "methods", "parameters", "returns", "raises", "yields", "examples", "example",
    "usage", "notes", "note", "see also", "overview", "introduction", "summary", "arguments",
    "properties", "functions", "classes", "constructor parameters", "type parameters",
    "source code", "references", "warning", "warnings", "todo", "fields", "args", "kwargs",
}


def heading_keywords(outline) -> list[str]:
    kws = []
    for h in outline:
        if h.kind != "heading" or h.level not in (2, 3):
            continue
        k = re.sub(r"[^\w.+#@/ -]", "", h.text).strip()
        if (k and len(k) <= 80 and k.lower() not in GENERIC_HEADINGS and KEYWORD_OK.match(k)
                and not denied(k)):
            kws.append(k)
    return kws


def dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out


_INLINE_CODE = re.compile(r"`([A-Za-z_][\w.]{1,39})(?:\(\))?(?:=[^`]*)?`")
# Literals and basic type names: present everywhere, useless for finding a page.
CODE_STOP = {
    "true", "false", "null", "none", "undefined", "this", "self", "cls", "nan",
    "string", "number", "boolean", "object", "array", "any", "int", "str", "float",
    "bool", "dict", "list", "tuple", "set", "map", "get", "date", "function", "symbol",
    "bytes", "type", "value", "key", "name", "data", "text", "default", "args", "kwargs",
}


def code_terms(md: str, limit: int = 20) -> list[str]:
    """Identifiers written as inline `code` outside fenced blocks, in order."""
    out, fence = [], False
    for line in md.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        for t in _INLINE_CODE.findall(line):
            if t.lower() in CODE_STOP:
                continue
            if t not in out:
                out.append(t)
                if len(out) >= limit:
                    return out
    return out


def section_text(md: str, outline, h) -> str:
    lines = md.splitlines()
    i = outline.index(h)
    end = len(lines)
    for nxt in outline[i + 1:]:
        if nxt.level <= h.level:
            end = nxt.line
            break
    return "\n".join(lines[h.line + 1:end])


def fit_keywords(pg: dict, ver: str, cap_tokens: int = 1500) -> str:
    kws: list[str] = []
    fixed = "\t".join([ver, pg["path"], pg["cat"], pg["title"], pg["summary"]])
    for k in pg["keywords"]:
        trial = ",".join(kws + [k])
        if est_tokens(fixed + "\t" + trial) > cap_tokens:
            break
        kws.append(k)
    return ",".join(kws)


def heading_path(outline, h) -> str:
    chain, lvl = [h.text], h.level
    i = outline.index(h)
    for prev in reversed(outline[:i]):
        if prev.level < lvl and prev.kind == "heading":
            chain.append(prev.text)
            lvl = prev.level
            if lvl <= 2:
                break
    text = ">".join(reversed([re.sub(r"[<>`]", "", c)[:60] for c in chain]))
    same = [x for x in outline[: i + 1] if x.text == h.text]
    return text + (f"#{len(same)}" if len(same) > 1 else "")


def combine_pages(rows: list[dict]) -> dict:
    """One '*' row from per-version rows of the same page: richest summary, union of keywords."""
    out = dict(rows[0])
    out["summary"] = max((r["summary"] for r in rows), key=len)
    kws: list[str] = []
    for r in rows:
        for k in filter(None, r["keywords"].split(",")):
            if k not in kws:
                kws.append(k)
    out["keywords"] = fit_keywords({"path": out["path"], "cat": out["cat"], "title": out["title"],
                                    "summary": out["summary"], "keywords": kws}, "*")
    return out


def merge_rows(old_rows, new_rows, ver, all_vers, key_cols, same=None, combine=None):
    """Expand '*' rows, replace rows of `ver`, then collapse rows present in all versions.

    Rows collapse when the `same` columns match in every version (default: all
    columns); `combine` builds the '*' row from the per-version rows.
    """
    cols = [c for c in (old_rows[0] if old_rows else new_rows[0] if new_rows else {}) if c != "_line"]
    expanded = []
    old_vers = [v for v in all_vers if v != ver]
    for r in old_rows:
        r = {c: r[c] for c in cols}
        if r["ver"] == "*":
            for v in old_vers:
                expanded.append({**r, "ver": v})
        elif r["ver"] != ver:
            expanded.append(r)
    expanded += [{c: r.get(c, "") for c in cols} for r in new_rows]
    key_of = same or tuple(c for c in cols if c != "ver")
    groups: dict[tuple, dict[str, dict]] = {}
    for r in expanded:
        k = tuple(r[c] for c in key_of)
        groups.setdefault(k, {})[r["ver"]] = r
    out = []
    for k, by_ver in groups.items():
        if set(by_ver) == set(all_vers) and len(all_vers) > 1:
            row = combine(list(by_ver.values())) if combine else next(iter(by_ver.values()))
            out.append({**row, "ver": "*"})
        else:
            out.extend(by_ver.values())
    out.sort(key=lambda r: tuple(r[c] for c in key_cols) + (r["ver"],))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a docpin index (mechanical, no LLM).")
    ap.add_argument("--id", required=True, help="e.g. pypi:pydantic")
    ap.add_argument("--base", required=True, help="docs root URL (version-independent part)")
    ap.add_argument("--out", required=True, help="index directory to write")
    ap.add_argument("--ver", default="0", help="major version these pages belong to (default 0 = unversioned)")
    ap.add_argument("--ver-path", default="", help="URL path part for this version under --base, e.g. 'latest' or '1.10'")
    ap.add_argument("--merge", action="store_true", help="merge into an existing index (add a version)")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--aliases", nargs="*")
    ap.add_argument("--maintainers", nargs="*")
    ap.add_argument("--allowed", nargs="*", help="allowed domains (default: base host)")
    ap.add_argument("--heading-slug", default="mkdocs")
    ap.add_argument("--max-pages", type=int, default=300)
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    ap.add_argument("--no-llms", action="store_true")
    ap.add_argument("--no-inv", action="store_true")
    ap.add_argument("--inv", nargs="*", help="objects.inv URL(s) when not at <version root>/objects.inv")
    ap.add_argument("--sitemap", help="'auto' (<version root>/sitemap.xml) or a sitemap URL")
    ap.add_argument("--include", nargs="*", help="with --sitemap: only paths starting with these prefixes")
    ap.add_argument("--allow-private", action="store_true", help="private docs only (http/file/private IPs)")
    ap.add_argument("--fixtures", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    Builder(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
