#!/usr/bin/env python3
"""Check that every static data a document template names is listed.

A document template (kind `html_template`, a `.gohtml` file) names a code by its
static data row with NSW-SriLanka's staticdata resolver:

    {{ staticdata "customs-cusdec--port-of-entry-and-discharge" .port }}

prints the title of the row whose const is .port, in that static_data artifact as
the task's manifest version lists it. An id the manifest doesn't list only shows
when a document is rendered: the step that renders it fails and waits for an
admin. So every call must name its id as a quoted string, and the manifest of the
bundle the template is in (`<bundle>/manifest.json`, the one its deployment
loads) must list that id as kind static_data.

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
# An action's delimiters, with their trim markers.
DELIMS = re.compile(r"^\{\{(?:-\s)?|(?:\s-)?\}\}$")
# A string or rune literal: interpreted, raw or rune.
STRING = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`|\'(?:[^\'\\]|\\.)*\'')
LITERAL_ID = re.compile(r'"([^"\\]*)"')


def blank(match):
    """Replace a match with spaces, keeping its newlines so line numbers hold."""
    return re.sub(r"[^\n]", " ", match.group(0))


def operands(body, i):
    """The operands from body[i] to the end of the command: the end of the
    action, a | or a ) closing an enclosing group."""
    args = []
    while True:
        while i < len(body) and body[i].isspace():
            i += 1
        if i >= len(body) or body[i] in ")|":
            return args
        start, depth = i, 0
        while i < len(body):
            literal = STRING.match(body, i)
            if literal:
                i = literal.end()
                continue
            c = body[i]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and (c.isspace() or c == "|"):
                break
            i += 1
        args.append(body[start:i])


def calls(source):
    """Yield (line, args) for each staticdata call: the operands it passes, with
    a value piped into it (.x | staticdata "id") counted as the last."""
    text = GO_COMMENT.sub(blank, source)
    for action in ACTION.finditer(text):
        body = DELIMS.sub(blank, action.group(0))
        # Look for the name outside string literals, so "staticdata" printed as
        # text isn't taken for a call.
        masked = STRING.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], body)
        for call in CALL.finditer(masked):
            line = text.count("\n", 0, action.start() + call.start()) + 1
            args = operands(body, call.end())
            if masked[: call.start()].rstrip().endswith("|"):
                args.append("<piped>")
            yield line, args


def static_data(manifest):
    """The ids a manifest lists as static_data."""
    rows = json.loads(manifest.read_text(encoding="utf-8")).get("artifacts", [])
    return {
        row.get("id")
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
        for line, args in calls(path.read_text(encoding="utf-8")):
            checked += 1
            literal = LITERAL_ID.fullmatch(args[0]) if args else None
            if not literal:
                print(f"{rel}:{line}: staticdata must name its static_data id as a quoted string")
                failures += 1
                continue
            sd_id = literal.group(1)
            if not sd_id:
                print(f"{rel}:{line}: staticdata names an empty id")
                failures += 1
                continue
            if len(args) != 2:
                print(f"{rel}:{line}: staticdata takes an id and a value, got {len(args)} argument(s)")
                failures += 1
                continue
            if manifest not in listed:
                listed[manifest] = static_data(manifest) if manifest.is_file() else set()
            if sd_id not in listed[manifest]:
                print(f"{rel}:{line}: static_data {sd_id!r} is not listed in {manifest.relative_to(root)}")
                failures += 1

    if failures:
        print(f"\n{failures} problem(s) in {checked} staticdata call(s)")
        return 1
    print(f"{checked} staticdata call(s) name static data their bundle lists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
