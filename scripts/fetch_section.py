#!/usr/bin/env python3
"""Fetch only one section of a documentation page.

Usage:
  python fetch_section.py <url[#anchor]> [--heading "H2>H3#n"] [--anchor ID]
                          [--id <index-id>] [--max-tokens 2500] [--data-dir DIR]

Prints `STATUS <code> ...` then the section inside <doc-data>...</doc-data>.
Only hosts listed in an index's allowed_domains can be fetched.
Status codes: OK TRUNCATED HEADING_MISSING NOT_FOUND REDIRECT BLOCKED
              DOMAIN_DENIED OFFLINE
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import FetchError, Index, est_tokens, host_allowed, http_get, iter_index_dirs, load_index  # noqa: E402
from _html import cut_section, heading_containing, html_to_md, md_outline  # noqa: E402
from lookup import index_roots  # noqa: E402

CACHE_TTL = 7 * 24 * 3600
MD_TYPES = {"text/markdown", "text/x-markdown", "text/plain"}


def find_index(url: str, index_id: str | None, roots: list[Path]) -> Index | None:
    host = urllib.parse.urlsplit(url).hostname or ""
    for d in iter_index_dirs(roots):
        idx = load_index(d, with_tables=False)
        if index_id and idx.id != index_id:
            continue
        if url.startswith("file:") and idx.meta.get("allow_private_network"):
            return idx
        if host_allowed(host, idx.meta.get("allowed_domains") or []):
            return idx
    return None


# ------------------------------------------------------------------ cache

def _cache_path(data_dir: Path | None, key: str) -> Path | None:
    if not data_dir:
        return None
    return data_dir / "cache" / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def cache_get(data_dir, key):
    p = _cache_path(data_dir, key)
    if not p or not p.exists():
        return None
    try:
        e = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - e["t"] > CACHE_TTL:
            return None
        return e["url"], e["type"], base64.b64decode(e["body"])
    except (OSError, ValueError, KeyError):
        return None


def cache_put(data_dir, key, url, ctype, body):
    p = _cache_path(data_dir, key)
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"t": time.time(), "url": url, "type": ctype,
                                 "body": base64.b64encode(body).decode()}), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------ fetch

def candidates(url: str, methods: list[str]):
    for m in methods:
        if m == "accept-md":
            yield m, url, "text/markdown"
        elif m == "dot-md":
            yield m, url.rstrip("/") + ".md", None
        elif m == "index-md" and url.endswith("/"):
            yield m, url + "index.md", None
        elif m == "html":
            yield m, url, "text/html"


def get_doc(url, idx: Index, method: str, target: str, accept, data_dir, fixtures, no_cache):
    key = f"{method}:{target}"
    hit = None if no_cache else cache_get(data_dir, key)
    if hit:
        return hit
    r = http_get(target, allowed=idx.meta.get("allowed_domains") or [],
                 allow_private=bool(idx.meta.get("allow_private_network")),
                 accept=accept, fixtures=fixtures)
    if method != "html" and r.content_type not in MD_TYPES:
        raise FetchError("NOT_FOUND", f"{method}: got {r.content_type}, not markdown")
    if method != "html" and r.text().lstrip().lower().startswith(("<!doctype", "<html")):
        raise FetchError("NOT_FOUND", f"{method}: soft 404 (HTML body)")
    cache_put(data_dir, key, r.url, r.content_type, r.body)
    return r.url, r.content_type, r.body


def to_markdown(ctype: str, body: bytes):
    text = body.decode("utf-8", errors="replace")
    if ctype in MD_TYPES:
        return text, md_outline(text)
    md, outline, _title = html_to_md(text)
    return md, outline


def log_stale(data_dir: Path | None, status: str, url: str, where: str):
    if not data_dir:
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "stale.log").open("a", encoding="utf-8") as f:
            f.write(f"{dt.date.today().isoformat()}\t{status}\t{url}\t{where}\n")
    except OSError:
        pass


def run(args) -> tuple[str, str]:
    url, _, frag = args.url.partition("#")
    anchor = args.anchor or (urllib.parse.unquote(frag) if frag else None)
    data_dir = Path(args.data_dir) if args.data_dir else None
    idx = find_index(url, args.id, index_roots(args))
    if idx is None:
        return f"STATUS DOMAIN_DENIED url={url}", "no index allows this host (only indexed doc hosts are fetched)"

    methods = list(idx.meta.get("fetch") or ["html"])
    if "html" not in methods:
        methods.append("html")
    last_err = None
    for method, target, accept in candidates(url, methods):
        try:
            final, ctype, body = get_doc(url, idx, method, target, accept, data_dir,
                                         args.fixtures, args.no_cache)
        except FetchError as e:
            last_err = e
            if e.status in ("DOMAIN_DENIED", "REDIRECT", "OFFLINE"):
                break
            continue
        md, outline = to_markdown(ctype, body)
        if method == "html" and not outline and len(md.strip()) < 200:
            last_err = FetchError("BLOCKED", "page has no readable body (JS-rendered?)")
            continue
        find = getattr(args, "find", None)
        sec = None
        if anchor or args.heading or not find:
            sec = cut_section(md, outline, anchor, args.heading, args.max_tokens)
        if find and (sec is None or sec.status == "HEADING_MISSING"):
            # keyword-window mode: the section that contains the term
            h = heading_containing(md, outline, find)
            if h is not None:
                sec = cut_section(md, outline, h.id, None if h.id else h.text, args.max_tokens)
            elif sec is None:
                sec = cut_section(md, outline, None, find, args.max_tokens)  # -> HEADING_MISSING + outline
        if sec.status == "HEADING_MISSING" and method != "html" and (anchor or args.heading):
            # markdown has no HTML ids; the HTML version may still have the anchor
            last_err = FetchError("HEADING_MISSING", sec.text)
            continue
        where = args.heading or anchor or "-"
        if sec.status != "OK":
            log_stale(data_dir, sec.status, url, where) if sec.status == "HEADING_MISSING" else None
        head = (f"STATUS {sec.status} url={final} via={method} tokens~{est_tokens(sec.text)}"
                f" index={idx.id}")
        return head, f'<doc-data source="{final}">\n{sec.text.rstrip()}\n</doc-data>'

    status = last_err.status if last_err else "NOT_FOUND"
    detail = last_err.detail if last_err else ""
    log_stale(data_dir, status, url, args.heading or anchor or "-")
    if status == "REDIRECT":
        return f"STATUS REDIRECT→{detail}", "redirect leaves the allowed domains; not followed"
    if status == "HEADING_MISSING":
        return f"STATUS HEADING_MISSING url={url}", f'<doc-data source="{url}">\n{detail}\n</doc-data>'
    return f"STATUS {status} url={url}", detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch one section of a documentation page.")
    ap.add_argument("url")
    ap.add_argument("--heading", help='heading path, e.g. "BaseModel>model_dump" or "Usage#2"')
    ap.add_argument("--anchor")
    ap.add_argument("--find", help="if the heading is not found: the section containing this term")
    ap.add_argument("--id", help="index id (default: the index whose allowed_domains match)")
    ap.add_argument("--max-tokens", type=int, default=2500)
    ap.add_argument("--data-dir")
    ap.add_argument("--index-path")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--fixtures", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    head, body = run(args)
    print(head)
    if body:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
