#!/usr/bin/env python3
"""Tooling for the Yemeni Open Source project catalog in README.md.

Commands
--------
check   Validate the catalog: table structure, sequential numbering, project
        counts, duplicate entries, and (with --base) the quality of the rows a
        pull request adds.
fix     Renumber the catalog rows and sync the three project counts in place.
sync    Rebuild a pull request's README on top of the latest main branch, so a
        pull request that only adds entries never conflicts on numbering.

The catalog is the markdown table between the CATALOG_START and CATALOG_END
markers in README.md. Every row looks like:

    | 240 | [Project](https://github.com/owner/project) | Category | Description. |
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

CATALOG_START = "<!-- YEMEN_OPEN_SOURCE_CATALOG:START -->"
CATALOG_END = "<!-- YEMEN_OPEN_SOURCE_CATALOG:END -->"

# The project total appears in the shields.io badge and twice in the prose.
BADGE_COUNT_RE = re.compile(r"(projects-)(\d+)(-1f6feb)")
PROSE_COUNT_RE = re.compile(r"\*\*(\d+) projects\*\*")

ROW_START_RE = re.compile(r"^\|\s*\d+\s*\|")
LINK_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\((?P<url>[^\s)]+)\)$")
ARABIC_RE = re.compile(r"[؀-ۿ]")
PROMOTIONAL_RE = re.compile(
    r"\b(best|powerful|amazing|revolutionary|ultimate|awesome|world-class)\b",
    re.IGNORECASE,
)

MAX_DESCRIPTION_WORDS = 30
HARD_DESCRIPTION_WORDS = 60

# Exit codes: 0 = fine, 1 = the catalog is invalid, 2 = a human has to look.
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_MANUAL = 2


class Row:
    """One catalog table row."""

    def __init__(self, line_no: int, number: int, name: str, url: str,
                 category: str, description: str):
        self.line_no = line_no
        self.number = number
        self.name = name
        self.url = url
        self.category = category
        self.description = description

    @property
    def key(self) -> str:
        """Normalized repository URL, used to identify an entry across branches."""
        url = self.url.strip().rstrip("/")
        url = re.sub(r"\.git$", "", url)
        url = re.sub(r"^https?://(www\.)?", "", url)
        return url.lower()

    @property
    def content(self) -> tuple:
        """Everything except the index number, to detect edits to existing rows."""
        return (self.name, self.url, self.category, self.description)

    def render(self, number: int) -> str:
        return f"| {number} | [{self.name}]({self.url}) | {self.category} | {self.description} |"


def split_cells(line: str):
    """Split a markdown table row into cells, honouring escaped pipes (\\|)."""
    parts = re.split(r"(?<!\\)\|", line.rstrip("\n"))
    if len(parts) < 3 or parts[0].strip() or parts[-1].strip():
        return None
    return [part.strip() for part in parts[1:-1]]


class Catalog:
    """The README parsed into a header, the catalog rows, and a footer."""

    def __init__(self, text: str):
        self.text = text
        self.lines = text.split("\n")
        self.rows: list[Row] = []
        self.errors: list[tuple[int, str]] = []

        try:
            self.start = next(i for i, l in enumerate(self.lines) if CATALOG_START in l)
            self.end = next(i for i, l in enumerate(self.lines) if CATALOG_END in l)
        except StopIteration:
            self.start = self.end = -1
            self.errors.append((1, "the catalog markers are missing from README.md"))
            return

        for i in range(self.start, self.end):
            line = self.lines[i]
            if not ROW_START_RE.match(line):
                continue
            cells = split_cells(line)
            if cells is None or len(cells) != 4:
                self.errors.append((
                    i + 1,
                    "a catalog row needs exactly four columns: "
                    "| # | [Name](url) | Category | Description |",
                ))
                continue
            number, link, category, description = cells
            match = LINK_RE.match(link)
            if not match:
                self.errors.append((
                    i + 1,
                    f"the Project column must be a markdown link, got: {link}",
                ))
                continue
            self.rows.append(Row(
                line_no=i + 1,
                number=int(number),
                name=match.group("name").strip(),
                url=match.group("url").strip(),
                category=category,
                description=description,
            ))

    @property
    def total(self) -> int:
        return len(self.rows)

    def counts(self) -> list[int]:
        return [int(m.group(2)) for m in BADGE_COUNT_RE.finditer(self.text)] + \
               [int(m.group(1)) for m in PROSE_COUNT_RE.finditer(self.text)]

    def by_key(self) -> dict:
        return {row.key: row for row in self.rows}

    def outside_catalog(self) -> str:
        """Everything before and after the catalog block, for change detection.

        The project totals are masked out: a contributor bumping the counts by
        hand is expected, and this tool rewrites them anyway.
        """
        if self.start < 0:
            text = self.text
        else:
            text = "\n".join(self.lines[: self.start + 1] + self.lines[self.end:])
        text = BADGE_COUNT_RE.sub(lambda m: f"{m.group(1)}N{m.group(3)}", text)
        return PROSE_COUNT_RE.sub("**N projects**", text)

    def normalized(self, extra_rows: list = None) -> str:
        """Return the README with rows renumbered 1..N and every count in sync."""
        rows = list(self.rows) + list(extra_rows or [])
        rendered = [row.render(i) for i, row in enumerate(rows, start=1)]

        # Keep everything in the catalog block that is not a row (heading, note,
        # table header) and splice the rows back in where the first row was.
        head, tail, seen_row = [], [], False
        for i in range(self.start, self.end):
            line = self.lines[i]
            if ROW_START_RE.match(line):
                seen_row = True
                continue
            (tail if seen_row else head).append(line)

        block = head + rendered + tail
        lines = self.lines[: self.start] + block + self.lines[self.end:]
        text = "\n".join(lines)

        total = len(rows)
        text = BADGE_COUNT_RE.sub(lambda m: f"{m.group(1)}{total}{m.group(3)}", text)
        text = PROSE_COUNT_RE.sub(f"**{total} projects**", text)
        return text


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# --- GitHub annotations -----------------------------------------------------

class Report:
    def __init__(self, file: str):
        self.file = file
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str, line: int = 1) -> None:
        self.errors.append(message)
        print(f"::error file={self.file},line={line}::{message}")

    def warn(self, message: str, line: int = 1) -> None:
        self.warnings.append(message)
        print(f"::warning file={self.file},line={line}::{message}")

    def summary(self) -> None:
        if self.errors:
            print(f"\n{len(self.errors)} problem(s) must be fixed before this can merge.")
        elif self.warnings:
            print(f"\nNo blocking problems. {len(self.warnings)} suggestion(s) above.")
        else:
            print("\nThe catalog is valid.")


# --- Checks on the repositories a pull request links to ---------------------

def github_api(path: str):
    request = urllib.request.Request(f"https://api.github.com{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "yemeni-open-source-catalog")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    # A malformed token must not crash the request; unauthenticated still works.
    if token and not re.search(r"\s", token):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except Exception:
        return None, None


def url_is_reachable(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    request.add_header("User-Agent", "yemeni-open-source-catalog")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status < 400
    except urllib.error.HTTPError as error:
        return error.code < 400
    except Exception:
        # A network hiccup must not block a pull request.
        return True


def check_new_row(row: Row, report: Report) -> None:
    """Apply the CONTRIBUTING.md rules to a row the pull request adds."""
    if not row.name:
        report.error("the project name is empty", row.line_no)
    if not row.category:
        report.error("the Category column is empty (use — if nothing fits)", row.line_no)
    if not row.description:
        report.error("the Description column is empty", row.line_no)

    if ARABIC_RE.search(row.name) or ARABIC_RE.search(row.description):
        report.error(
            "the project table is English-only — write the name and description in English",
            row.line_no,
        )

    words = len(row.description.split())
    if words > HARD_DESCRIPTION_WORDS:
        report.error(
            f"the description is {words} words; keep it to one sentence "
            f"(under {MAX_DESCRIPTION_WORDS} words)",
            row.line_no,
        )
    elif words > MAX_DESCRIPTION_WORDS:
        report.warn(
            f"the description is {words} words; under {MAX_DESCRIPTION_WORDS} reads better",
            row.line_no,
        )

    promotional = PROMOTIONAL_RE.search(row.description)
    if promotional:
        report.warn(
            f'"{promotional.group(0)}" is promotional — say what the project does instead',
            row.line_no,
        )

    if not row.url.startswith("https://"):
        report.error("the project link must be an https:// URL", row.line_no)
        return

    match = re.match(r"^https://github\.com/([^/]+)/([^/#?]+)", row.url)
    if not match:
        if not url_is_reachable(row.url):
            report.error(f"the project link is not reachable: {row.url}", row.line_no)
        return

    owner, name = match.group(1), re.sub(r"\.git$", "", match.group(2))
    status, data = github_api(f"/repos/{owner}/{name}")
    if status is None:
        report.warn("could not reach the GitHub API to verify this repository", row.line_no)
        return
    if status == 404:
        report.error(
            f"github.com/{owner}/{name} does not exist or is not public",
            row.line_no,
        )
        return
    if status != 200 or data is None:
        report.warn(f"could not verify github.com/{owner}/{name} (HTTP {status})", row.line_no)
        return

    if data.get("fork"):
        parent = (data.get("parent") or {}).get("html_url", "the original repository")
        report.error(
            f"this links to a fork — link to the original instead: {parent}",
            row.line_no,
        )
    if not data.get("license"):
        report.error(
            "the repository has no explicit open-source license, which the "
            "eligibility criteria require",
            row.line_no,
        )
    if data.get("archived"):
        report.warn("the repository is archived", row.line_no)
    if data.get("full_name", "").lower() != f"{owner}/{name}".lower():
        report.warn(
            f"the repository has moved to {data.get('html_url')} — link there instead",
            row.line_no,
        )


# --- Commands ---------------------------------------------------------------

def command_check(args) -> int:
    report = Report(os.path.basename(args.readme))
    catalog = Catalog(read(args.readme))

    for line_no, message in catalog.errors:
        report.error(message, line_no)
    if not catalog.rows:
        report.summary()
        return EXIT_INVALID

    for expected, row in enumerate(catalog.rows, start=1):
        if row.number != expected:
            report.error(
                f"this row is numbered {row.number} but should be {expected} "
                "(the index must run 1..N with no gaps)",
                row.line_no,
            )
            break

    counts = catalog.counts()
    if not counts:
        report.error("could not find the project count in the badge or the prose")
    for count in counts:
        if count != catalog.total:
            report.error(
                f"the README says {count} projects but the table has {catalog.total} rows"
            )
            break

    seen: dict = {}
    for row in catalog.rows:
        if row.key in seen:
            report.error(
                f"{row.url} is already listed on line {seen[row.key].line_no}",
                row.line_no,
            )
        else:
            seen[row.key] = row

    # Rules that apply only to what this pull request adds, so existing entries
    # written under older conventions are never re-litigated.
    if args.base:
        base = Catalog(read(args.base))
        base_keys = set(base.by_key())
        new_rows = [row for row in catalog.rows if row.key not in base_keys]
        removed = base_keys - set(catalog.by_key())
        if removed and not args.allow_removals:
            report.error(
                f"this removes {len(removed)} existing entry/entries; label the pull "
                "request 'entry-removal' if that is intended"
            )
        print(f"This pull request adds {len(new_rows)} entry/entries.")
        for row in new_rows:
            check_new_row(row, report)

    report.summary()
    return EXIT_INVALID if report.errors else EXIT_OK


def command_fix(args) -> int:
    original = read(args.readme)
    catalog = Catalog(original)
    if catalog.errors:
        for line_no, message in catalog.errors:
            print(f"cannot fix: {message} (line {line_no})", file=sys.stderr)
        return EXIT_INVALID

    fixed = catalog.normalized()
    if fixed == original:
        print("Nothing to fix: the index and counts are already in sync.")
        return EXIT_OK
    write(args.readme, fixed)
    print(f"Renumbered {catalog.total} rows and synced every project count.")
    return EXIT_OK


def command_sync(args) -> int:
    """Rebuild the head README on top of main, keeping only the rows it adds.

    This is what removes the numbering conflicts: instead of merging two
    versions of the same table, the entries a pull request introduces are
    re-appended to the current main and the whole index is renumbered.
    """
    main = Catalog(read(args.main))
    base = Catalog(read(args.merge_base))
    head = Catalog(read(args.head))

    for catalog, label in ((main, "main"), (base, "merge base"), (head, "pull request")):
        if catalog.errors:
            print(f"the {label} README could not be parsed; a human should look at this",
                  file=sys.stderr)
            return EXIT_MANUAL

    if head.outside_catalog() != base.outside_catalog():
        print("this pull request changes the README outside the project table; "
              "resolve it by hand", file=sys.stderr)
        return EXIT_MANUAL

    base_rows, head_rows, main_rows = base.by_key(), head.by_key(), main.by_key()

    removed = set(base_rows) - set(head_rows)
    if removed:
        print(f"this pull request removes {len(removed)} entry/entries; "
              "resolve it by hand", file=sys.stderr)
        return EXIT_MANUAL

    edited = [k for k in set(base_rows) & set(head_rows)
              if base_rows[k].content != head_rows[k].content]
    if edited:
        print(f"this pull request edits {len(edited)} existing entry/entries; "
              "resolve it by hand", file=sys.stderr)
        return EXIT_MANUAL

    added = [row for row in head.rows if row.key not in base_rows]
    duplicates = [row for row in added if row.key in main_rows]
    added = [row for row in added if row.key not in main_rows]
    for row in duplicates:
        print(f"note: {row.url} is already on main; dropping the duplicate row")

    result = main.normalized(extra_rows=added)
    write(args.out or args.head, result)
    print(f"Synced with main: {len(added)} new entry/entries, "
          f"{main.total + len(added)} projects in total.")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="validate the catalog")
    check.add_argument("--readme", default="README.md")
    check.add_argument("--base", help="the README from the base branch, to find added rows")
    check.add_argument("--allow-removals", action="store_true")
    check.set_defaults(func=command_check)

    fix = commands.add_parser("fix", help="renumber rows and sync the counts")
    fix.add_argument("--readme", default="README.md")
    fix.set_defaults(func=command_fix)

    sync = commands.add_parser("sync", help="rebuild the head README on top of main")
    sync.add_argument("--main", required=True)
    sync.add_argument("--merge-base", required=True)
    sync.add_argument("--head", required=True)
    sync.add_argument("--out")
    sync.set_defaults(func=command_sync)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
