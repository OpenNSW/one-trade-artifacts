#!/usr/bin/env python3
"""Check that every static data a document template names is listed.

A document template (kind `html_template`, a `.gohtml` file) names a code by its
static data row with NSW-SriLanka's staticdata resolver:

    {{ staticdata "customs-cusdec--port-of-entry-and-discharge" "1" .port }}

prints the title of the row whose const is .port, in version "1" of that
static_data artifact. An id or version the deployment doesn't have only shows
when a document is rendered: the step that renders it fails and waits for an
admin. So every call must name its id and version as quoted strings, and the
manifest of the bundle the template is in (`<bundle>/manifest.json`, the one
its deployment loads) must list that id, as kind static_data, at that version.

Go template comments ({{- /* ... */ -}}) are not checked.

Usage: check_static_data_refs.py [repo-root]
"""
import json
import re
import sys
from pathlib import Path

GO_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)
ACTION = re.compile(r"\{\{.*?\}\}", re.DOTALL)
# The resolver's name as a word on its own, not part of a field (.staticdata)
# or a longer name.
CALL = re.compile(r"(?<![\w.$])staticdata(?![\w])")
LITERAL_ARGS = re.compile(r'\s+"([^"\\]*)"\s+"([^"\\]*)"')


def blank(match):
    """Replace a match with spaces, keeping its newlines so line numbers hold."""
    return re.sub(r"[^\n]", " ", match.group(0))


def calls(source):
    """Yield (line, id, version) for each staticdata call, with id None for one
    that doesn't name its id and version as quoted strings."""
    text = GO_COMMENT.sub(blank, source)
    for action in ACTION.finditer(text):
        for call in CALL.finditer(action.group(0)):
            index = action.start() + call.start()
            line = text.count("\n", 0, index) + 1
            args = LITERAL_ARGS.match(action.group(0), call.end())
            if args:
                yield line, args.group(1), args.group(2)
            else:
                yield line, None, None


def static_data(manifest):
    """The (id, version) pairs a manifest lists as static_data."""
    rows = json.loads(manifest.read_text(encoding="utf-8")).get("artifacts", [])
    return {
        (row.get("id"), row.get("version"))
        for row in rows
        if isinstance(row, dict) and row.get("kind") == "static_data"
    }


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures = checked = 0
    listed = {}
    for path in sorted(root.rglob("*.gohtml")):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in parts):
            continue
        rel = path.relative_to(root)
        manifest = root / parts[0] / "manifest.json"
        for line, sd_id, version in calls(path.read_text(encoding="utf-8")):
            checked += 1
            if sd_id is None:
                print(f"{rel}:{line}: staticdata must name its static_data id and version as quoted strings")
                failures += 1
                continue
            if not sd_id or not version:
                print(f"{rel}:{line}: staticdata names an empty id or version")
                failures += 1
                continue
            if manifest not in listed:
                listed[manifest] = static_data(manifest) if manifest.is_file() else set()
            if (sd_id, version) not in listed[manifest]:
                print(f"{rel}:{line}: static_data {sd_id!r} version {version!r} is not listed in "
                      f"{manifest.relative_to(root)}")
                failures += 1

    if failures:
        print(f"\n{failures} problem(s) in {checked} staticdata call(s)")
        return 1
    print(f"{checked} staticdata call(s) name static data their bundle lists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
