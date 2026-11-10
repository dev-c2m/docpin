"""Shared helpers for docpin scripts.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1
VERSION = "0.1.0"
REPO_URL = "https://github.com/dev-c2m/docpin"
USER_AGENT = f"docpin/{VERSION} (+{REPO_URL})"

PAGES_COLS = ["ver", "path", "cat", "title", "summary", "keywords"]
SYMBOLS_COLS = ["ver", "symbol", "kind", "path", "anchor", "heading_path", "params"]

STATUSES = {"active", "stale", "deprecated"}
FETCH_METHODS = {"accept-md", "dot-md", "index-md", "html"}
SYMBOL_KINDS = {"heading", "dt"}
ECOSYSTEMS = {"pypi", "npm", "crates", "go", "maven", "gem", "nuget", "site"}

# Hosts shared by many unrelated owners. A bare root here would let an index
# fetch from anyone's site, so indexes must name the owner-specific subdomain.
SHARED_ROOTS = {
    "github.io", "gitlab.io", "readthedocs.io", "readthedocs.org",
    "githubusercontent.com", "raw.githubusercontent.com", "github.com",
    "netlify.app", "vercel.app", "pages.dev", "web.app", "firebaseapp.com",
}


# Phrases typical of prompt injection. Hygiene only: the real defence is on the
# reading side (index and page text are data, never instructions). Kept as
# phrases so ordinary doc headings ("Instructions", "Run tests") still pass.
DENYLIST = [
    "ignore previous", "ignore all previous", "ignore the above", "disregard previous",
    "disregard the above", "system prompt", "you must", "you are now", "as an ai",
    "new instructions", "follow these instructions", "follow the instructions",
    "assistant:", "curl ", "wget ", "sudo ", "rm -rf", "powershell -", "invoke-expression",
    "eval(", "이전 지시", "지시를 무시", "무시하고", "시스템 프롬프트", "명령을 실행",
]


def denied(text: str) -> str | None:
    low = (text or "").lower()
    return next((d for d in DENYLIST if d in low), None)


# ---------------------------------------------------------------- text utils

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^0-9a-zA-Z가-힣]+")
_HANGUL = re.compile(r"[가-힣]")


def tokens(text: str) -> list[str]:
    """Lowercase tokens; splits snake_case, dots, punctuation and camelCase.

    A camelCase word also keeps its whole form, so acronym-style names match
    either way: "PostgreSQL" -> postgresql, postgre, sql; "excludeNone" ->
    excludenone, exclude, none.
    """
    out = []
    for word in _SPLIT.split(text or ""):
        if not word:
            continue
        parts = [p for p in _CAMEL.sub(" ", word).lower().split() if p]
        if len(parts) > 1:
            out.append(word.lower())
        out += parts
    return out


def has_hangul(text: str) -> bool:
    return bool(_HANGUL.search(text or ""))


def est_tokens(text: str) -> int:
    """Conservative token estimate: UTF-8 bytes / 3 (Hangul ~1 token per char)."""
    return (len((text or "").encode("utf-8")) + 2) // 3


def clean_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"[\t\r\n]+", " ", str(value)).strip()


# ------------------------------------------------------- frontmatter (subset)
# Supported YAML subset: one `key: value` per line; value is a scalar,
# a flow list `[a, b]` or a flow map `{a: b}`. `#` starts a comment when
# preceded by whitespace and outside quotes.

def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line


def _split_flow(s: str) -> list[str]:
    parts, depth, quote, cur = [], 0, None, []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


def _split_key(s: str) -> tuple[str, str]:
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ":" and (i + 1 == len(s) or s[i + 1] == " "):
            return s[:i].strip(), s[i + 1:].strip()
    raise ValueError(f"missing ':' in {s!r}")


def _scalar(s: str):
    s = s.strip()
    if s in ("", "null", "~"):
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def parse_value(s: str):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [parse_value(x) for x in _split_flow(inner)] if inner else []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        out = {}
        for part in _split_flow(inner) if inner else []:
            k, v = _split_key(part)
            out[str(_scalar(k))] = parse_value(v)
        return out
    return _scalar(s)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("INDEX.md must start with '---' frontmatter")
    meta = {}
    for n, raw in enumerate(lines[1:], start=2):
        if raw.strip() == "---":
            return meta, "\n".join(lines[n:])
        line = _strip_comment(raw)
        if not line.strip():
            continue
        try:
            k, v = _split_key(line)
        except ValueError as e:
            raise ValueError(f"line {n}: {e}") from None
        meta[k] = parse_value(v)
    raise ValueError("unterminated frontmatter")


def dump_value(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(dump_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))}: {dump_value(x)}" for k, x in v.items()) + "}"
    s = str(v)
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", s) and not re.fullmatch(r"-?[\d.]+|true|false|null|~", s):
        return s
    return json.dumps(s, ensure_ascii=False)


def parse_body(body: str) -> tuple[list[str], list[str]]:
    """Return (capabilities, categories) from the INDEX.md body."""
    caps, cats, section = [], [], None
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("## "):
            section = s[3:].strip().lower()
            continue
        if not s:
            continue
        if section == "capabilities" and s.startswith("- "):
            caps.append(s[2:].strip())
        elif section == "categories":
            cats += [c.strip() for c in re.split(r"[·,]", s) if c.strip()]
    return caps, cats


# --------------------------------------------------------------------- TSV

def read_tsv(path: Path, cols: list[str]) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        return rows, [f"{path.name}: empty file"]
    header = lines[0].split("\t")
    if header[0] != f"#schema={SCHEMA}" or header[1:] != cols:
        errors.append(f"{path.name}:1: header must be '#schema={SCHEMA}' + tab-separated {cols}")
        return rows, errors
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip() or line.startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) != len(cols):
            errors.append(f"{path.name}:{n}: expected {len(cols)} columns, got {len(cells)}")
            continue
        row = dict(zip(cols, cells))
        row["_line"] = n
        rows.append(row)
    return rows, errors


def write_tsv(path: Path, cols: list[str], rows: list[dict]) -> None:
    out = ["\t".join([f"#schema={SCHEMA}", *cols])]
    for r in rows:
        out.append("\t".join(clean_cell(r.get(c, "")) for c in cols))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ------------------------------------------------------------- index model

@dataclass
class Index:
    dir: Path
    meta: dict
    capabilities: list[str]
    categories: list[str]
    pages: list[dict] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    def url_for(self, ver: str, path: str) -> str:
        """Build a page URL. `ver` is a ver-column value (major or '*')."""
        ver_map = self.meta.get("ver_map") or {}
        if ver == "*":
            ver = default_ver(self)
        ver_path = str(ver_map.get(str(ver), "")) if ver_map else ""
        tmpl = self.meta.get("url_template") or "{base}{path}"
        base = str(self.meta.get("base", ""))
        url = tmpl.replace("{base}", base).replace("{ver_path}", ver_path).replace("{path}", path)
        # collapse accidental '//' in the path part (not in the scheme)
        scheme, rest = url.split("://", 1) if "://" in url else ("", url)
        rest = re.sub(r"/{2,}", "/", rest)
        return f"{scheme}://{rest}" if scheme else rest


def default_ver(index: Index) -> str:
    """Highest major in ver_map (as the 'latest' choice)."""
    keys = list((index.meta.get("ver_map") or {}).keys())
    nums = [k for k in keys if re.fullmatch(r"\d+", str(k))]
    if nums:
        return max(nums, key=int)
    return keys[0] if keys else "*"


def load_index(d: Path, with_tables: bool = True) -> Index:
    idx_md = d / "INDEX.md"
    meta, body = parse_frontmatter(idx_md.read_text(encoding="utf-8"))
    caps, cats = parse_body(body)
    index = Index(d, meta, caps, cats)
    if with_tables:
        if (d / "pages.tsv").exists():
            index.pages, errs = read_tsv(d / "pages.tsv", PAGES_COLS)
            index.errors += errs
        for sp in sorted(d.glob("symbols*.tsv")):
            rows, errs = read_tsv(sp, SYMBOLS_COLS)
            index.symbols += rows
            index.errors += errs
    return index


def iter_index_dirs(roots: list[Path]):
    seen = set()
    for root in roots:
        if not root or not root.exists():
            continue
        for p in sorted(root.rglob("INDEX.md")):
            d = p.parent.resolve()
            if d not in seen:
                seen.add(d)
                yield d


def id_to_relpath(index_id: str) -> Path:
    eco, _, name = index_id.partition(":")
    return Path(eco, *name.split("/"))


# ------------------------------------------------------------ safe fetching

class FetchError(Exception):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class Response:
    url: str
    content_type: str
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for d in allowed or []:
        d = str(d).lower().rstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _check_public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchError("OFFLINE", f"cannot resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise FetchError("DOMAIN_DENIED", f"{host} resolves to non-public address {ip}")


def check_url(url: str, allowed: list[str], allow_private: bool = False, resolve: bool = True) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme == "file":
        if not allow_private:
            raise FetchError("DOMAIN_DENIED", "file:// only allowed for private indexes")
        return
    if p.scheme != "https" and not (allow_private and p.scheme == "http"):
        raise FetchError("DOMAIN_DENIED", f"scheme {p.scheme!r} not allowed (https only)")
    if not host_allowed(p.hostname or "", allowed):
        raise FetchError("DOMAIN_DENIED", f"{p.hostname} not in allowed_domains {allowed}")
    if resolve and not allow_private:
        _check_public(p.hostname or "")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, check):
        super().__init__()
        self._check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            self._check(newurl)
        except FetchError as e:
            raise FetchError("REDIRECT", newurl) if e.status == "DOMAIN_DENIED" else e
        return super().redirect_request(req, fp, code, msg, headers, newurl)


ALLOWED_TYPES = {
    "text/html", "application/xhtml+xml", "text/markdown", "text/x-markdown",
    "text/plain", "application/octet-stream", "application/json",
    "application/xml", "text/xml",  # sitemaps
}


def http_get(url: str, *, allowed: list[str], allow_private: bool = False,
             accept: str | None = None, max_bytes: int = 5_000_000,
             timeout: float = 20.0, fixtures: Path | None = None) -> Response:
    """GET with domain allowlist, redirect re-checks, SSRF block and size cap.

    With `fixtures`, reads `<fixtures>/urls.json` ({url: {file, type}}) instead
    of the network. A key prefixed with 'accept-md:' answers requests sent
    with `Accept: text/markdown`.
    """
    check_url(url, allowed, allow_private, resolve=fixtures is None)
    if fixtures is not None:
        return _fixture_get(url, fixtures, accept,
                            lambda u: check_url(u, allowed, allow_private, resolve=False))
    if url.startswith("file:"):
        path = Path(urllib.request.url2pathname(urllib.parse.urlsplit(url).path))
        if not path.exists():
            raise FetchError("NOT_FOUND", str(path))
        ctype = "text/markdown" if path.suffix in (".md", ".markdown") else "text/html"
        return Response(url, ctype, path.read_bytes()[:max_bytes])

    opener = urllib.request.build_opener(_Redirect(lambda u: check_url(u, allowed, allow_private)))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept or "*/*"})
    try:
        resp = opener.open(req, timeout=timeout)
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        status = "NOT_FOUND" if e.code in (404, 410) else "BLOCKED"
        raise FetchError(status, f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, FetchError):
            raise e.reason from None
        raise FetchError("OFFLINE", str(e.reason)) from None
    except (TimeoutError, socket.timeout):
        raise FetchError("OFFLINE", "timeout") from None
    with resp:
        ctype = resp.headers.get_content_type()
        if ctype not in ALLOWED_TYPES:
            raise FetchError("BLOCKED", f"content-type {ctype} not allowed")
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise FetchError("BLOCKED", f"response larger than {max_bytes} bytes")
        return Response(resp.geturl(), ctype, body)


def _fixture_get(url: str, fixtures: Path, accept: str | None, check) -> Response:
    table = json.loads((fixtures / "urls.json").read_text(encoding="utf-8"))
    key = ("accept-md:" + url) if accept and "text/markdown" in accept else url
    entry = table.get(key)
    if entry is None:
        if key != url:  # server ignored the Accept header: serve the plain entry
            entry = table.get(url)
        if entry is None:
            raise FetchError("NOT_FOUND", f"fixture missing for {key}")
    if entry.get("redirect"):
        try:
            check(entry["redirect"])
        except FetchError as e:
            raise FetchError("REDIRECT", entry["redirect"]) if e.status == "DOMAIN_DENIED" else e
        return _fixture_get(entry["redirect"], fixtures, accept, check)
    body = (fixtures / entry["file"]).read_bytes()
    ctype = entry.get("type", "text/html")
    if ctype not in ALLOWED_TYPES:  # same rule as the network path, so tests catch it
        raise FetchError("BLOCKED", f"content-type {ctype} not allowed")
    return Response(entry.get("final", url), ctype, body)
