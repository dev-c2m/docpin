#!/usr/bin/env python3
"""Find where something is documented. Prints a status line and the top-k rows.

Usage:
  python lookup.py <name|id> <terms...> [--ver X] [--cat C] [--k 10]
  python lookup.py <name|id> --browse [--cat C]
  python lookup.py <name> --where            # installed location + version (no import)

Index roots, in priority order: --index-path, $CLAUDE_PLUGIN_OPTION_INDEX_PATH,
$DOCPIN_INDEX_PATH, <data-dir>/indexes, the plugin's bundled indexes/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    Index, default_ver, has_hangul, iter_index_dirs, load_index, parse_frontmatter, tokens,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STALE_DAYS = 180
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml")


# ------------------------------------------------------------ index roots

def index_roots(args) -> list[Path]:
    roots = []
    for p in (args.index_path, os.environ.get("CLAUDE_PLUGIN_OPTION_INDEX_PATH"),
              os.environ.get("DOCPIN_INDEX_PATH")):
        if p:
            roots.append(Path(p))
    if args.data_dir:
        roots.append(Path(args.data_dir) / "indexes")
    roots.append(PLUGIN_ROOT / "indexes")
    return roots


def norm_name(s: str) -> str:
    return re.sub(r"[-_.]+", "-", s.strip().lower())


def resolve(name: str, roots: list[Path], project: Path) -> tuple[list[Path], str]:
    """Return (matching index dirs, how). First root wins for identical ids."""
    want = norm_name(name)
    hits: dict[str, Path] = {}
    for d in iter_index_dirs(roots):
        try:
            meta, _ = parse_frontmatter((d / "INDEX.md").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        iid = str(meta.get("id", ""))
        short = iid.split(":", 1)[-1]
        aliases = [norm_name(str(a)) for a in meta.get("aliases") or []]
        if want in (norm_name(iid), norm_name(short)) or want in aliases:
            hits.setdefault(iid, d)
    if len(hits) > 1:
        eco = "pypi" if any((project / f).exists() for f in ("pyproject.toml", "uv.lock", "poetry.lock")) \
            else "npm" if (project / "package.json").exists() else None
        pref = {k: v for k, v in hits.items() if eco and k.startswith(eco + ":")}
        if len(pref) == 1:
            return list(pref.values()), "ecosystem"
    return list(hits.values()), "name"


# ------------------------------------------------------- project / versions

def find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for d in [cur, *cur.parents]:
        if any((d / f).exists() for f in LOCKFILES + ("pyproject.toml", "package.json")) \
                or list(d.glob("requirements*.txt")):
            return d
    return cur


def lockfile_versions(root: Path, pkg: str) -> dict[str, str]:
    """{source file: version} for `pkg` in the lockfiles found in `root`."""
    want = norm_name(pkg)
    found: dict[str, str] = {}
    for name in ("uv.lock", "poetry.lock"):
        p = root / name
        if p.exists():
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError):
                continue
            for entry in data.get("package", []):
                if norm_name(str(entry.get("name", ""))) == want and entry.get("version"):
                    found[name] = str(entry["version"])
    for p in sorted(root.glob("requirements*.txt")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)", line)
            if m and norm_name(m.group(1)) == want:
                found[p.name] = m.group(2)
    p = root / "package-lock.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            pk = data.get("packages", {}).get(f"node_modules/{pkg}") or \
                data.get("dependencies", {}).get(pkg) or {}
            if pk.get("version"):
                found[p.name] = pk["version"]
        except (json.JSONDecodeError, OSError):
            pass
    p = root / "pnpm-lock.yaml"
    if p.exists():
        rx = re.compile(r"^\s{2}['\"]?/?" + re.escape(pkg) + r"[@/](\d[^:('\"\s]*)")
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = rx.match(line)
            if m:
                found[p.name] = m.group(1)
                break
    return found


def major(version: str) -> str | None:
    m = re.match(r"\D*(\d+)", version or "")
    return m.group(1) if m else None


def pick_version(idx: Index, args, project: Path) -> tuple[str | None, str, list[str]]:
    """Return (ver column value | None, note, problems)."""
    ver_map = {str(k): v for k, v in (idx.meta.get("ver_map") or {}).items()}
    if list(ver_map) == ["0"]:
        return "0", "unversioned docs", []
    if args.ver:
        mj = major(args.ver) if args.ver not in ver_map else args.ver
        if mj in ver_map:
            return mj, f"from --ver {args.ver}", []
        return None, f"--ver {args.ver}", [f"no index for major {mj}; available: {sorted(ver_map)}"]
    name = idx.id.split(":", 1)[-1]
    found = lockfile_versions(project, name)
    majors = {major(v) for v in found.values() if major(v)}
    if len(majors) > 1:
        return None, "", ["AMBIGUOUS " + ", ".join(f"{k}={v}" for k, v in found.items())]
    if majors:
        mj = majors.pop()
        src = ", ".join(f"{k} {v}" for k, v in found.items())
        if mj in ver_map:
            return mj, f"from {src}", []
        return None, f"from {src}", [f"no index for major {mj}; available: {sorted(ver_map)}"]
    return default_ver(idx), "version unknown -> newest indexed", []


# ---------------------------------------------------------------- scoring

def stem(t: str) -> str:
    """Very light English plural folding: proxies→proxy, timeouts→timeout, classes→class."""
    if len(t) <= 3 or has_hangul(t):
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith(("sses", "xes", "ches", "shes")):
        return t[:-2]
    if t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def _field_tokens(text: str) -> list[str]:
    return [stem(t) for t in tokens(text)]


def score_row(q: list[str], fields: list[tuple[str, float]],
              tw: dict[str, float] | None = None) -> tuple[float, int]:
    total, matched = 0.0, 0
    tw = tw or {}
    for t in q:
        best = 0.0
        for text, w in fields:
            if not text:
                continue
            if has_hangul(t):
                if t in text.lower():
                    best = max(best, w)
                continue
            ftoks = _field_tokens(text)
            if t in ftoks:
                best = max(best, w)
            elif len(t) >= 3 and any(ft.startswith(t) or (len(ft) >= 4 and t.startswith(ft)) for ft in ftoks):
                best = max(best, w * 0.5)
        if best:
            matched += 1
        total += best * tw.get(t, 1.0)
    return total, matched


# Pages that describe changes between versions.
CHANGE_PAGE = re.compile(r"migrat|upgrad|changelog|change[-_ ]log|release[-_ ]?notes|whats[-_ ]?new|"
                         r"what.s new|breaking|deprecat", re.I)
# Words that make a question about a change ("is X still there", "what is the new default").
CHANGE_WORDS = re.compile(
    r"\b(change[sd]?|new|added|remove[sd]?|deprecat\w*|breaking|migrat\w*|upgrad\w*|defaults?|"
    r"renamed|replace[sd]?|replacement|still|anymore|longer|since|introduced|dropped)\b|"
    r"바뀌|바뀐|변경|새로|추가|제거|삭제|폐기|기본값|아직", re.I)
VERSION_TOKEN = re.compile(r"^v?(\d+(?:\.\d+)+)$", re.I)


def version_norm(v: str) -> str:
    return re.sub(r"\D", "", v)


def query_versions(terms: list[str], ver_arg: str | None) -> set[str]:
    """Versions named in the query ('2.1', 'v2.0') or a minor --ver ('2.1'), digits only."""
    out = set()
    for t in terms:
        for part in re.split(r"\s+", t):
            m = VERSION_TOKEN.match(part.strip())
            if m:
                out.add(version_norm(m.group(1)))
    if ver_arg and "." in ver_arg:
        out.add(version_norm(ver_arg))
    return out


def change_intent(terms: list[str], ver_arg: str | None) -> bool:
    return bool(query_versions(terms, ver_arg)) or bool(CHANGE_WORDS.search(" ".join(terms)))


def page_versions(r: dict) -> set[str]:
    """Version numbers a change page is about: 'What's New in 2.1' / 'migration_21' / 'upgrade/2/'."""
    text = f"{r['title']} {r['path']}"
    found = {version_norm(m) for m in re.findall(r"(?<!\d)\d{1,2}(?:[._-]\d{1,2})*(?!\d)", text)}
    return {v for v in found if v}


def change_version_match(r: dict, qvers: set[str], major_ver: str) -> bool | None:
    """True: the page is about the asked version; False: about another version;
    None: the page names no version (e.g. a general changelog)."""
    pv = page_versions(r)
    if not pv:
        return None
    if qvers:
        return bool(qvers & pv)
    if major_ver and major_ver not in ("0", "*"):
        # no minor version asked: '2' matches 'Version 2.0', 'upgrade/2', 'migration_21'
        return any(v == major_ver or v.startswith(major_ver) for v in pv)
    return None


def is_change_page(r: dict) -> bool:
    return bool(CHANGE_PAGE.search(f"{r['title']} {r['path']}"))


def token_weights(idx: Index) -> dict[str, float]:
    """Down-weight tokens found on many pages (site name, header chrome like
    'SQLAlchemy 2.1 Documentation'): >60% of pages x0.1, >30% x0.5.
    Common domain words keep some weight; they still matter in combination."""
    if not idx.pages or len(idx.pages) < 8:
        return {}
    df: dict[str, int] = {}
    for r in idx.pages:
        for t in {stem(x) for x in tokens(f"{r['keywords']} {r['title']}")}:
            df[t] = df.get(t, 0) + 1
    n = len(idx.pages)
    return {t: (0.1 if c > 0.6 * n else 0.5) for t, c in df.items() if c > 0.3 * n}


def search(idx: Index, ver: str, terms: list[str], cat: str | None, k: int,
           ver_arg: str | None = None):
    q = []
    for t in terms:
        q += [stem(x) for x in tokens(t)]
    q = list(dict.fromkeys(q))
    joined = "".join(terms).lower()
    tw = token_weights(idx)
    intent = change_intent(terms, ver_arg)
    qvers = query_versions(terms, ver_arg)
    results = []
    for r in idx.symbols:
        if r["ver"] not in (ver, "*"):
            continue
        s, m = score_row(q, [(r["symbol"], 5), (r["params"].replace(",", " "), 3), (r["heading_path"], 2)], tw)
        sym = r["symbol"].lower()
        if joined and (joined == sym or sym.endswith("." + joined)):
            s += 5
        if s:
            results.append(("sym", s * (1.5 if m == len(q) else 1), r))
    for r in idx.pages:
        if r["ver"] not in (ver, "*") or (cat and r["cat"] != cat):
            continue
        s, m = score_row(q, [(r["keywords"].replace(",", " "), 3), (r["title"], 2), (r["summary"], 1),
                             (r["path"].replace("/", " "), 1)], tw)
        if s:
            s *= 1.5 if m == len(q) else 1
            # One heading containing several query terms beats terms scattered
            # over unrelated headings ("Default PostgreSQL driver changed ...").
            qs = set(q)
            best = max((len(qs & {stem(x) for x in tokens(kw)})
                        for kw in r["keywords"].split(",") if kw), default=0)
            if best >= 2:
                s += 2 * best
            if intent and is_change_page(r):
                # Change questions: the migration notes / changelog of the asked
                # version are the authoritative source; general pages may lag behind.
                match = change_version_match(r, qvers, ver)
                if match is False:
                    s *= 0.4  # change notes of another version
                else:
                    s = s * 1.3 + 4 + (6 if match else 0)
            elif is_change_page(r) and change_version_match(r, set(), ver) is False:
                s *= 0.5  # not a change question: old release notes should not lead
            results.append(("page", s, r))
    results.sort(key=lambda x: (-x[1], 0 if x[0] == "sym" else 1, len(x[2]["path"])))
    # Reserve slots so API symbols cannot crowd out guide pages (and vice versa):
    # "how do I use proxies" needs the guide even when 20 symbols mention proxies.
    reserve = min(3, k // 3)
    pages = [r for r in results if r[0] == "page"][:reserve]
    syms = [r for r in results if r[0] == "sym"][:reserve]
    chosen = pages + syms
    for r in results:
        if len(chosen) >= k:
            break
        if r not in chosen:
            chosen.append(r)
    if intent:  # make sure the best change page is in the list
        best_change = next((x for x in results if x[0] == "page" and is_change_page(x[2])
                            and change_version_match(x[2], qvers, ver) is not False), None)
        if best_change and best_change not in chosen:
            chosen[-1:] = [best_change]
    chosen.sort(key=lambda x: (-x[1], 0 if x[0] == "sym" else 1, len(x[2]["path"])))
    return chosen, max(0, len(results) - len(chosen))


def matched_keywords(keywords: str, terms: list[str], limit: int = 3,
                     tw: dict[str, float] | None = None) -> list[str]:
    """Keywords (often section headings) containing the query terms, best first.
    Weighted like the search, so site chrome ('X 2.1 Documentation') loses."""
    q = set()
    for t in terms:
        q.update(stem(x) for x in tokens(t))
    tw = tw or {}
    scored = []
    for kw in filter(None, keywords.split(",")):
        hits = q & {stem(x) for x in tokens(kw)}
        if hits:
            scored.append((-sum(tw.get(h, 1.0) for h in hits), len(kw), kw))
    return [kw for _, _, kw in sorted(scored)[:limit]]


def exact_symbol_absent(idx: Index, ver: str, terms: list[str]) -> bool:
    """True when a single symbol-like term is absent from a complete inventory."""
    cs = idx.meta.get("complete_symbols")
    complete = ver in [str(v) for v in cs] if isinstance(cs, list) else cs is True
    if len(terms) != 1 or not complete:
        return False
    t = terms[0]
    if not re.fullmatch(r"[A-Za-z_][\w.]*", t) or not re.search(r"[._]|[a-z][A-Z]", t):
        return False
    low = t.lower()
    for r in idx.symbols:
        if r["ver"] in (ver, "*"):
            s = r["symbol"].lower()
            if s == low or s.endswith("." + low):
                return False
    return True


# ---------------------------------------------------------------- output

def status_of(idx: Index) -> str:
    st = str(idx.meta.get("status", "active"))
    if st == "deprecated":
        return f"DEPRECATED replaced_by={idx.meta.get('replaced_by') or '-'}"
    try:
        age = (dt.date.today() - dt.date.fromisoformat(str(idx.meta.get("checked")))).days
    except ValueError:
        age = 0
    return "STALE" if st == "stale" or age > STALE_DAYS else "OK"


def print_browse(idx: Index, ver: str, cat: str | None, k: int):
    if not cat:
        print("Capabilities:")
        for c in idx.capabilities or ["(none listed)"]:
            print(f"- {c}")
        counts: dict[str, int] = {}
        for r in idx.pages:
            if r["ver"] in (ver, "*"):
                counts[r["cat"]] = counts.get(r["cat"], 0) + 1
        print("Categories: " + " · ".join(f"{c}({n})" for c, n in sorted(counts.items())))
        print("(use --browse --cat <name> to list pages)")
        return
    rows = [r for r in idx.pages if r["ver"] in (ver, "*") and r["cat"] == cat]
    print("title\turl\tsummary")
    for r in rows[:k]:
        print(f"{r['title']}\t{idx.url_for(r['ver'], r['path'])}\t{r['summary'] or '-'}")
    if len(rows) > k:
        print(f"(+{len(rows) - k} more)")


def cmd_where(name: str, project: Path):
    """Installed location and version, found without importing anything."""
    found = []
    venvs = [Path(os.environ["VIRTUAL_ENV"])] if os.environ.get("VIRTUAL_ENV") else []
    venvs += [project / ".venv", project / "venv"]
    mod_names = {name.replace("-", "_"), name.replace("-", "_").lower(), name.split("/")[-1]}
    for v in venvs:
        for sp in list(v.glob("Lib/site-packages")) + list(v.glob("lib/python*/site-packages")):
            for dist in sp.glob("*.dist-info"):
                dn, _, dv = dist.name[: -len(".dist-info")].partition("-")
                if norm_name(dn) == norm_name(name):
                    found.append(f"python\t{dv}\t{dist}")
            for m in mod_names:
                for cand in (sp / m, sp / f"{m}.py"):
                    if cand.exists():
                        found.append(f"source\t-\t{cand}")
    nm = project / "node_modules" / name
    if (nm / "package.json").exists():
        try:
            pj = json.loads((nm / "package.json").read_text(encoding="utf-8"))
            types = pj.get("types") or pj.get("typings") or ""
            found.append(f"node\t{pj.get('version', '-')}\t{nm}" + (f"\ttypes={nm / types}" if types else ""))
        except (json.JSONDecodeError, OSError):
            found.append(f"node\t-\t{nm}")
    if not found:
        print(f"STATUS NOT_INSTALLED name={name} project={project}")
        return
    print(f"STATUS OK name={name}  (read stubs/sources with Grep/Read; nothing was imported)")
    print("kind\tversion\tpath")
    for f in dict.fromkeys(found):
        print(f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Look up documentation locations in docpin indexes.")
    ap.add_argument("name")
    ap.add_argument("terms", nargs="*")
    ap.add_argument("--ver")
    ap.add_argument("--cat")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--browse", action="store_true")
    ap.add_argument("--where", action="store_true")
    ap.add_argument("--project", type=Path, default=Path.cwd())
    ap.add_argument("--index-path")
    ap.add_argument("--data-dir")
    ap.add_argument("--fetch", type=int, default=0, metavar="N",
                    help="also fetch the sections of the top N results (one call instead of N+1)")
    ap.add_argument("--max-tokens", type=int, default=1500, help="per fetched section (with --fetch)")
    ap.add_argument("--fixtures", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    project = find_project_root(args.project)
    if args.where:
        cmd_where(args.name, project)
        return 0

    dirs, _how = resolve(args.name, index_roots(args), project)
    if not dirs:
        print(f"STATUS NO_INDEX name={args.name}")
        return 0
    if len(dirs) > 1:
        ids = [load_index(d, with_tables=False).id for d in dirs]
        print(f"STATUS AMBIGUOUS_ID candidates={','.join(ids)}  (pass the full id)")
        return 0

    idx = load_index(dirs[0])
    ver, note, problems = pick_version(idx, args, project)
    head = f"id={idx.id} lang={idx.meta.get('lang', 'en')} checked={idx.meta.get('checked')}"
    if problems:
        kind = "AMBIGUOUS_VERSION" if problems[0].startswith("AMBIGUOUS") else "NO_VERSION_INDEX"
        print(f"STATUS {kind} {head} {note}".rstrip())
        for p in problems:
            print(f"  {p.removeprefix('AMBIGUOUS ')}")
        mig = [r for r in idx.pages if r["cat"] == "migration"]
        for r in mig[:3]:
            print(f"  migration: {idx.url_for(r['ver'], r['path'])}")
        print("  -> pass --ver <major> to choose")
        return 0

    status = status_of(idx)
    print(f"STATUS {status} {head} ver={ver} ({note})")

    if args.browse or not args.terms:
        print_browse(idx, ver, args.cat, args.k)
        return 0

    rows, more = search(idx, ver, args.terms, args.cat, args.k, ver_arg=args.ver)
    intent = change_intent(args.terms, args.ver)
    tw = token_weights(idx)
    if exact_symbol_absent(idx, ver, args.terms):
        print(f"NOTE '{args.terms[0]}' is not in the complete symbol inventory for ver {ver} "
              "(evidence it is not a documented API; rows below are partial matches)")
    if not rows:
        print("NO_MATCH")
        if idx.meta.get("lang", "en") != "en":
            print(f"NOTE index language is {idx.meta.get('lang')}: retry with terms in that language")
        return 0
    print("score\tkind\tname\tcat\turl\theading")
    targets = []  # (url, heading) for --fetch
    for kind, s, r in rows:
        if kind == "sym":
            url = idx.url_for(r["ver"], r["path"]) + (f"#{r['anchor']}" if r["anchor"] else "")
            print(f"{s:.1f}\tsym\t{r['symbol']}\t-\t{url}\t{r['heading_path'] or '-'}")
            targets.append((url, [], False))
        else:
            hints = matched_keywords(r["keywords"], args.terms, tw=tw)
            hint = ("kw:" + "|".join(hints)) if hints else "-"
            url = idx.url_for(r["ver"], r["path"])
            kind_label = "change" if intent and is_change_page(r) else "page"
            print(f"{s:.1f}\t{kind_label}\t{r['title']}\t{r['cat']}\t{url}\t{hint}")
            targets.append((url, hints, kind_label == "change"))
    if more:
        print(f"(+{more} more)")
    if args.fetch:
        # Change questions: one fetch slot always goes to the best change note.
        if intent and args.fetch >= 2:
            first_change = next((t for t in targets if t[2]), None)
            if first_change and first_change not in targets[: args.fetch]:
                targets.remove(first_change)
                targets.insert(args.fetch - 1, first_change)
        fetch_targets([(t[0], t[1]) for t in targets], args)
    return 0


def fetch_targets(targets: list[tuple[str, list[str]]], args) -> None:
    """Fetch the top results' sections in the same call (saves a model turn per section).

    For a page, the matched keywords are tried in order as headings: a keyword
    can be a code identifier rather than a heading, so the first may not cut.
    """
    import argparse as _ap
    import fetch_section  # local import: fetch_section imports this module

    seen, done = set(), 0
    for url, hints in targets:
        key = (url.split("#")[0], tuple(hints), url.partition("#")[2])
        if key in seen:
            continue
        seen.add(key)
        # try the hints as headings; if none is a heading, cut the section that
        # contains the first hint (identifiers listed in tables, code terms)
        # best hint first: as a heading, then as a term to locate; then the next hint
        tries = [t for h in hints[:3] for t in ((h, None), (None, h))] if hints else [(None, None)]
        for heading, find in tries:
            ns = _ap.Namespace(url=url, heading=heading, anchor=None,
                               id=None, find=find, max_tokens=args.max_tokens, data_dir=args.data_dir,
                               index_path=args.index_path, no_cache=False, fixtures=args.fixtures)
            head, body = fetch_section.run(ns)
            if "HEADING_MISSING" not in head:
                break
        print(f"\n--- fetch {done + 1}: {head}")
        if body:
            print(body)
        done += 1
        if done >= args.fetch:
            break


if __name__ == "__main__":
    sys.exit(main())
