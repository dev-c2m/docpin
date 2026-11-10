#!/usr/bin/env python3
"""Validate docpin indexes (schema 1) and optionally write REGISTRY.tsv.

Usage:
  python validate_index.py <indexes-root-or-index-dir>... [--write-registry FILE]

Exit code 0 when no errors (warnings allowed), 1 otherwise.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DENYLIST, ECOSYSTEMS, FETCH_METHODS, PAGES_COLS, SCHEMA, SHARED_ROOTS, STATUSES, SYMBOL_KINDS,
    clean_cell, est_tokens, id_to_relpath, iter_index_dirs, load_index, write_tsv,
)

REQUIRED_META = ["id", "schema", "status", "lang", "base", "url_template", "ver_map",
                 "fetch", "allowed_domains", "checked"]
ALLOWED_FILES = re.compile(r"^(INDEX\.md|pages\.tsv|symbols(\.[a-z0-9_-]+)?\.tsv)$")

INDEX_MD_TOKEN_CAP = 800
# Rows are never shown to the model in full (lookup prints title + URL only),
# so the cap guards file size, not context use. Long pages need many headings.
ROW_TOKEN_CAP = 1600
SUMMARY_MAX = 100
KEYWORD_RE = re.compile(r"^[\w.+#@/ -]{1,80}$")
SUMMARY_FORBIDDEN = re.compile(r"`|<|>|\[|\]\(|://|\n")
ID_RE = re.compile(r"^([a-z]+):(\S+)$")


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, where: str, msg: str):
        self.errors.append(f"ERROR {where}: {msg}")

    def warn(self, where: str, msg: str):
        self.warnings.append(f"WARN  {where}: {msg}")


HEADING_FORBIDDEN = re.compile(r"`|<|\]\(|://|\n")  # '>' separates heading levels


def check_text(rep: Report, where: str, text: str, *, max_len: int | None = None,
               forbidden: re.Pattern = SUMMARY_FORBIDDEN):
    low = text.lower()
    for bad in DENYLIST:
        if bad in low:
            rep.err(where, f"forbidden phrase {bad!r}")
    if max_len is not None and len(text) > max_len:
        rep.err(where, f"longer than {max_len} chars")
    if forbidden.search(text):
        rep.err(where, f"contains a forbidden character sequence ({forbidden.pattern})")


def check_keywords(rep: Report, where: str, value: str):
    if not value:
        return
    for kw in value.split(","):
        kw = kw.strip()
        if not KEYWORD_RE.match(kw):
            rep.err(where, f"bad keyword {kw!r}")
        check_text(rep, where, kw)


def check_domains(rep: Report, where: str, meta: dict):
    base = str(meta.get("base", ""))
    p = urllib.parse.urlsplit(base)
    private = bool(meta.get("allow_private_network"))
    if p.scheme != "https" and not (private and p.scheme in ("http", "file")):
        rep.err(where, "base must be https")
    host = (p.hostname or "").lower()
    allowed = meta.get("allowed_domains") or []
    if not isinstance(allowed, list) or not allowed:
        rep.err(where, "allowed_domains must be a non-empty list")
        return
    for d in allowed:
        d = str(d).lower()
        if d in SHARED_ROOTS:
            rep.err(where, f"allowed_domains entry {d!r} is a shared host root; use the owner-specific host")
        related = host == d or host.endswith("." + d) or d.endswith("." + host)
        if p.scheme != "file" and not related:
            rep.err(where, f"allowed_domains entry {d!r} is unrelated to base host {host!r}")


def validate_dir(d: Path, root: Path | None, rep: Report, ids: dict) -> dict | None:
    rel = d.relative_to(root) if root else d
    where = str(rel)
    try:
        idx = load_index(d)
    except (ValueError, OSError) as e:
        rep.err(where, f"INDEX.md: {e}")
        return None
    for e in idx.errors:
        rep.err(where, e)
    meta = idx.meta

    for f in d.iterdir():
        if not ALLOWED_FILES.match(f.name):
            rep.err(where, f"unexpected file {f.name!r} (only INDEX.md, pages.tsv, symbols*.tsv)")

    for k in REQUIRED_META:
        if k not in meta:
            rep.err(where, f"missing frontmatter key {k!r}")
    if meta.get("schema") != SCHEMA:
        rep.err(where, f"schema must be {SCHEMA}")

    index_id = str(meta.get("id", ""))
    m = ID_RE.match(index_id)
    if not m or m.group(1) not in ECOSYSTEMS:
        rep.err(where, f"bad id {index_id!r} (expected <ecosystem>:<name>, ecosystem in {sorted(ECOSYSTEMS)})")
    else:
        if index_id != index_id.lower() and m.group(1) in ("pypi", "npm"):
            rep.err(where, "pypi/npm ids must be lowercase")
        if m.group(1) == "pypi" and re.search(r"[_.]", m.group(2)):
            rep.err(where, "pypi ids are PEP 503 normalized ('_' and '.' -> '-')")
        if root is not None and rel != id_to_relpath(index_id):
            rep.err(where, f"directory must be {id_to_relpath(index_id).as_posix()} for id {index_id}")
        if index_id in ids:
            rep.err(where, f"duplicate id {index_id} (also in {ids[index_id]})")
        ids[index_id] = where

    status = meta.get("status")
    if status not in STATUSES:
        rep.err(where, f"status must be one of {sorted(STATUSES)}")
    if status == "deprecated" and not meta.get("replaced_by"):
        rep.warn(where, "deprecated index without replaced_by")

    fetch = meta.get("fetch") or []
    if not isinstance(fetch, list) or not fetch or any(f not in FETCH_METHODS for f in fetch):
        rep.err(where, f"fetch must be a non-empty list of {sorted(FETCH_METHODS)}")

    ver_map = meta.get("ver_map")
    if not isinstance(ver_map, dict) or not ver_map:
        rep.err(where, "ver_map must be a non-empty map {major: url-path-part}")
        ver_map = {}
    tmpl = str(meta.get("url_template", ""))
    if "{path}" not in tmpl or "{base}" not in tmpl:
        rep.err(where, "url_template must contain {base} and {path}")
    if len(ver_map) > 1 and "{ver_path}" not in tmpl:
        rep.err(where, "url_template needs {ver_path} when ver_map has several versions")

    check_domains(rep, where, meta)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(meta.get("checked", ""))):
        rep.err(where, "checked must be YYYY-MM-DD")

    md_text = (d / "INDEX.md").read_text(encoding="utf-8")
    if est_tokens(md_text) > INDEX_MD_TOKEN_CAP:
        rep.err(where, f"INDEX.md is ~{est_tokens(md_text)} tokens (cap {INDEX_MD_TOKEN_CAP})")
    if not idx.capabilities:
        rep.warn(where, "no Capabilities (browse/recommend mode will be empty)")
    if len(idx.capabilities) > 20:
        rep.err(where, "more than 20 Capabilities")
    for i, cap in enumerate(idx.capabilities, 1):
        check_text(rep, f"{where} capability {i}", cap, max_len=SUMMARY_MAX)
    for field in ("one_line",):
        if meta.get(field):
            check_text(rep, f"{where} {field}", str(meta[field]), max_len=SUMMARY_MAX)

    valid_vers = {str(k) for k in ver_map} | {"*"}
    cats = set(idx.categories)

    seen_pages = set()
    for r in idx.pages:
        w = f"{where}/pages.tsv:{r['_line']}"
        if r["ver"] not in valid_vers:
            rep.err(w, f"ver {r['ver']!r} not in ver_map keys or '*'")
        path = r["path"]
        if "://" in path or path.startswith("/") or ".." in path.split("/"):
            rep.err(w, "path must be relative to the version root (no scheme, no leading '/', no '..')")
        if cats and r["cat"] and r["cat"] not in cats:
            rep.warn(w, f"cat {r['cat']!r} not listed in Categories")
        if not r["title"]:
            rep.err(w, "empty title")
        check_text(rep, w, r["title"], max_len=SUMMARY_MAX)
        check_text(rep, w, r["summary"], max_len=SUMMARY_MAX)
        check_keywords(rep, w, r["keywords"])
        key = (r["ver"], path)
        if key in seen_pages:
            rep.err(w, f"duplicate page {key}")
        seen_pages.add(key)
        row_tok = est_tokens("\t".join(r[c] for c in PAGES_COLS))
        if row_tok > ROW_TOKEN_CAP:
            rep.err(w, f"row ~{row_tok} tokens (cap {ROW_TOKEN_CAP})")

    seen_syms = set()
    for r in idx.symbols:
        w = f"{where}/symbols.tsv:{r['_line']}"
        if r["ver"] not in valid_vers:
            rep.err(w, f"ver {r['ver']!r} not in ver_map keys or '*'")
        if r["kind"] not in SYMBOL_KINDS:
            rep.err(w, f"kind must be one of {sorted(SYMBOL_KINDS)}")
        if not r["symbol"] or not re.fullmatch(r"[\w.$:<>,\[\] -]{1,200}", r["symbol"]):
            rep.err(w, f"bad symbol {r['symbol']!r}")
        if "://" in r["path"] or r["path"].startswith("/"):
            rep.err(w, "path must be relative")
        if not r["anchor"] and not r["heading_path"]:
            rep.err(w, "need anchor or heading_path")
        check_text(rep, w, r["heading_path"], max_len=200, forbidden=HEADING_FORBIDDEN)
        check_keywords(rep, w, r["params"])
        key = (r["ver"], r["symbol"], r["path"], r["anchor"])
        if key in seen_syms:
            rep.err(w, "duplicate symbol row")
        seen_syms.add(key)

    return {
        "id": index_id,
        "aliases": ",".join(str(a) for a in meta.get("aliases") or []),
        "lang": meta.get("lang", ""),
        "status": status or "",
        "one_line": clean_cell(meta.get("one_line") or (idx.capabilities[0] if idx.capabilities else "")),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--write-registry", type=Path)
    args = ap.parse_args(argv)

    rep, ids, registry = Report(), {}, []
    for p in args.paths:
        p = p.resolve()
        root = None if (p / "INDEX.md").exists() else p
        for d in iter_index_dirs([p]):
            entry = validate_dir(d, root, rep, ids)
            if entry:
                registry.append(entry)

    for line in rep.warnings + rep.errors:
        print(line)
    print(f"checked {len(registry)} index(es): {len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")

    if args.write_registry and not rep.errors:
        registry.sort(key=lambda r: r["id"])
        write_tsv(args.write_registry, ["id", "aliases", "lang", "status", "one_line"], registry)
        print(f"wrote {args.write_registry}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
