**English** | [한국어](README.ko.md)

# docpin

**Version-pinned library docs lookup for Claude Code.**
Instead of web-searching and reading whole pages, the agent looks up *where*
something is documented and fetches only that section, from the version your
project actually uses.

An index is a map, not a copy: page paths, titles, section headings, API symbols
with their anchors, and a few keywords. No documentation text is stored or
redistributed.

## Tokens saved (measured)

Per answer, with and without docpin, in headless Claude Code (Sonnet 5.5,
November 2026). Questions about Polars 2.0, SQLAlchemy 2.1 and Flet 1.0/1.1, all
released after the model's training data. Counts include every model involved:
the main conversation, the lookup agent, and web-page summarization.

| per answer (round 2, 12 questions × 2 runs) | with docpin | without (web search) | change |
|---|---|---|---|
| **new input tokens** | **25,236** | 88,808 | **−72%** |
| **output tokens** | **3,708** | 7,471 | **−50%** |
| all input tokens, incl. cache reads | 150,543 | 185,741 | −19% |
| cost | $0.072 | $0.080 | −10% |
| time | 29 s | 44 s | −34% |

*New input tokens* are what the model reads for the first time, such as page
content, billed at full price. *Cache reads* are the same instructions re-read on
every turn, billed at about a tenth of that, which is why cost falls less than
new tokens. The saving comes from reading only the needed section instead of
whole pages.

Round 1 (13 other questions, before the last ranking fixes): new input tokens
−82%, output tokens −62%, cost −22%, time −48%.

**Accuracy did not suffer.** Answers were checked against keys taken from the
official docs: 23 of 24 correct with docpin, 18 of 24 without (round 1: 23/26 vs
17/26). Web search often never found the upgrade guide and answered "the docs
don't say".

Where it does not help: for popular libraries the model already knows, cost is
about equal; for stable APIs it has memorized, the agent is not called.

Caveats: small samples (two runs per question), one model, one machine (Windows).
Questions were written after tuning, on topics never used to fix the code, and
the code was frozen during each run.

## How it works

```
question ─▶ Claude ─▶ doc-lookup agent (small model, Bash + WebFetch only)
                         │ lookup.py <lib> <terms> --fetch 2   ← one call
                         │   ranks pages/symbols in the index, picks the version
                         │   from your lockfile, fetches the top 2 sections
                         ▼
                      short answer with source URLs ─▶ Claude
```

- **Sections, not pages.** The page is cut at the matching heading or anchor
  (markdown when the site serves it, otherwise HTML), with a size cap.
- **Right version.** The version comes from `uv.lock`, `poetry.lock`,
  `requirements*.txt`, `package-lock.json` or `pnpm-lock.yaml`. If that version
  is not indexed, it says so instead of guessing.
- **Change questions.** "What changed / is X still there in 2.1?" ranks that
  version's migration notes and changelog first.
- **Honest negatives.** "Not in the index" never means "doesn't exist", except
  for exact symbol names in an inventory fully verified for that version.
- **Small, separate context.** The lookup runs in its own agent; in our tests
  the lookup itself cost about $0.002.

## Bundled indexes

| id | docs | pages | symbols |
|---|---|---|---|
| `pypi:polars` | user guide + Python API (2.x) | ~1,600 | ~1,500 |
| `pypi:sqlalchemy` | 2.1 documentation | 182 | 4,174 (+2,363 parameters) |
| `pypi:pydantic` | 2.x and 1.10 | 91 + 23 | 728 |
| `pypi:requests` | 2.x | 14 | 150 |
| `npm:vue` | 3.x | 94 | – |
| `pypi:flet` | 1.x docs + blog | 1,103 | – |

## Install

Requires Python 3.11+ on `PATH` (`python`; on Windows `py -3` also works).
The scripts use the standard library only.

In Claude Code (2.1.275 or later):

```
/plugin install docpin --marketplace dev-c2m/docpin
```

Or in two steps, on any version:

```
/plugin marketplace add dev-c2m/docpin
/plugin install docpin@docpin
```

From a shell (2.1.292 or later): `claude plugin install docpin --marketplace dev-c2m/docpin`.

Updates: auto-update is off by default for third-party marketplaces. Turn it on in
`/plugin` › Marketplaces › docpin, or update by hand with `/plugin marketplace update docpin`.

To try a local checkout instead: `git clone https://github.com/dev-c2m/docpin.git`
and start Claude Code with `claude --plugin-dir ./docpin`.

Then just ask, in any language: "How does explode() treat empty lists in Polars 2?
Check the official docs."

### Permissions

Each lookup is a Bash command. To avoid a prompt every time, allow exactly these
two scripts in your Claude Code settings. Use the plugin's install path, which
the first permission prompt shows in the command:

```json
{
  "permissions": {
    "allow": [
      "Bash(python \"/path/to/docpin/scripts/lookup.py\" *)",
      "Bash(python \"/path/to/docpin/scripts/fetch_section.py\" *)"
    ]
  }
}
```

Do not allow `Bash(python *)`: that would allow any Python code.

## Index a library you use

```
/index-docs pypi:mylib https://mylib.dev/docs/
```

Sources: `llms.txt`, Sphinx/mkdocstrings `objects.inv`, `sitemap.xml`. Building
is mechanical (no LLM) and free; ~1,500 pages take about 15 minutes because
requests are spaced out. For versioned docs, give the version and its URL part
(e.g. `--ver 2 --ver-path latest`).

**Private docs:** put your indexes in a folder of your own and set the plugin
option `index_path`; they take precedence over bundled ones. For intranet docs
set `allow_private_network: true` in that index's `INDEX.md`.

## Status lines

| status | meaning |
|---|---|
| `NO_INDEX` | no index for that library; the agent falls back to the web |
| `NO_MATCH` | no matching row; this does **not** mean the feature doesn't exist |
| `NO_VERSION_INDEX` / `AMBIGUOUS_VERSION` | your version isn't indexed, or lockfiles disagree |
| `TRUNCATED` / `HEADING_MISSING` | section too long, or the page changed; its outline is listed |
| `DOMAIN_DENIED` / `REDIRECT→…` | blocked by the index's domain allowlist |

## Limitations

- Versions are distinguished by **major** number only (SQLAlchemy 2.0 and 2.1
  share one entry; the bundled index is 2.1).
- Sites without `llms.txt`, `objects.inv` or `sitemap.xml` cannot be indexed yet;
  pages rendered only by JavaScript cannot be read.
- No authenticated docs.

## Privacy and security

- No telemetry. The scripts contact only the hosts listed in an index's
  `allowed_domains`; redirects to other hosts are not followed, and private or
  internal addresses are refused (except for private indexes that allow them).
- Index text and fetched pages are treated as data, never as instructions.
- Fetched sections are cached for 7 days in the plugin's data folder.

Report security problems privately via
[GitHub security advisories](https://github.com/dev-c2m/docpin/security/advisories/new),
not in public issues.

## License

Code: [Apache-2.0](LICENSE). Index data in `indexes/`: [CC0 1.0](indexes/LICENSE).
