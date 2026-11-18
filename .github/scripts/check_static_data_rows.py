#!/usr/bin/env python3
"""Check that every row of a static data file has the same keys as its first row.

A `*.data.json` file is `{"data": [row, ...]}`. Its first row sets the file's keys
(`const`, `title` and any other keys), and every other row must have exactly those
keys. A form's `displayTemplate` and `searchKeys` name keys of the file, so a row
without one shows a literal `{key}` or is never found by that key. Files can differ
from each other (`const`/`title`, or `const`/`title`/`description`); one file never
mixes key sets.

That `const` and `title` are strings, and what other keys may hold, is
schemas/staticdata.schema.json's job. This script checks only that the rows agree.

Usage: check_static_data_rows.py [repo-root]
"""
import json
import sys
from pathlib import Path

# A file with many mismatched rows (a wrong first row) reports this many, then a count.
MAX_REPORTED = 5


def key_set_problems(rows):
    """Return one message per row whose keys differ from the first row's, in row order."""
    if not rows or not isinstance(rows[0], dict):
        return []  # the schema reports an empty list or a first row that is not an object
    expected = set(rows[0])
    problems = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"row {index} is not an object")
            continue
        keys = set(row)
        if keys == expected:
            continue
        parts = []
        if expected - keys:
            parts.append("missing " + ", ".join(sorted(expected - keys)))
        if keys - expected:
            parts.append("extra " + ", ".join(sorted(keys - expected)))
        problems.append(f"row {index} has different keys from row 0: {'; '.join(parts)}")
    return problems


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    files = failed = 0
    for path in sorted(root.rglob("*.data.json")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        files += 1
        rel = path.relative_to(root).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            print(f"::error file={rel}::{rel}: cannot read the file: {err}")
            failed += 1
            continue
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue  # the schema reports a file without a data list
        problems = key_set_problems(rows)
        if not problems:
            continue
        failed += 1
        first = ", ".join(sorted(rows[0])) if isinstance(rows[0], dict) else "?"
        for message in problems[:MAX_REPORTED]:
            print(f"::error file={rel}::{rel}: {message} (row 0 keys: {first})")
        if len(problems) > MAX_REPORTED:
            print(f"::error file={rel}::{rel}: {len(problems) - MAX_REPORTED} more rows differ from row 0")

    if failed:
        print(f"\n{failed} of {files} static data file(s) have rows with different keys")
        return 1
    print(f"all rows of {files} static data file(s) have the keys of their first row")
    return 0


if __name__ == "__main__":
    sys.exit(main())
