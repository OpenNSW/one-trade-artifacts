#!/usr/bin/env python3
"""Check that every static-data field of a form fits the static data it names.

A form field with `x-search.service: "static-data"` takes its options from one
static_data artifact, picked by `x-search.params.id` and `.version`. Its
`displayTemplate` fills `{key}` from each row of that artifact, and
`x-search.params.searchKeys` names the row keys a typed search matches. A key no
row has only shows when someone opens the form: the label keeps the literal
`{key}`, or the search finds nothing. So for every such field in the generic
templates a bundle's manifest lists (`<bundle>/manifest.json`):

- the id and version must be listed as static_data in that manifest, and the file
  must hold a static data object with a `data` list;
- every `{key}` of the displayTemplate must be a key the rows have, and its value a
  string, number or boolean (a row key such as `parents` holds a list, which a
  template cannot print). That every row has the same keys is
  check_static_data_rows.py's job;
- `searchKeys` must be a list of at most MAX_SEARCH_KEYS different names, each a
  key some row has.

`{id}` and `{name}` are legacy names for a row's const and title, which the
trader-app still sets on every option, so deployed forms keep working. They are
warnings, never errors, and reported as one list so that the many existing forms
don't bury the rest.

Function calls in a template (`{today(yyyy)}`), fields without a displayTemplate
and other search services (`cha`) are not checked.

Usage: check_static_data_fields.py [repo-root]
"""
import json
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

# The renderer's placeholder (jsonforms-renderers, utils/template.ts): {key} is a
# row value, and {name(arg)} a function call.
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)(?:\(([^(){}]*)\))?\}")
# What nsw-srilanka's static-data API accepts as searchKeys (internal/staticdata,
# handler.go): names of letters, digits and underscores, at most this many.
SEARCH_KEY = re.compile(r"[A-Za-z0-9_]+")
MAX_SEARCH_KEYS = 10
# The trader-app sets id and name from const and title on every option.
LEGACY_PLACEHOLDERS = {"id": "const", "name": "title"}
# The values a template prints.
PRINTABLE = (str, int, float, bool)


class Rows:
    """The keys of one static data file's rows."""

    def __init__(self, rows):
        self.count = Counter()  # key -> number of rows that have it
        self.unprintable = set()  # keys with a value a template cannot print
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                self.count[key] += 1
                if not isinstance(value, PRINTABLE):
                    self.unprintable.add(key)

    def keys(self):
        return ", ".join(sorted(self.count))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def load_rows(path):
    data = read_json(path)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("it is not a static data file: it needs an object with a data list")
    return Rows(rows)


def x_searches(node, path=""):
    """Yield (json path, x-search) for each static-data field under node."""
    if isinstance(node, dict):
        search = node.get("x-search")
        if isinstance(search, dict) and search.get("service") == "static-data":
            yield path or "/", search
        for key, value in node.items():
            yield from x_searches(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from x_searches(value, f"{path}[{i}]")


def placeholder_keys(template):
    """The row keys a displayTemplate names, once each in order, without its function calls."""
    return list(dict.fromkeys(m.group(1) for m in PLACEHOLDER.finditer(template) if m.group(2) is None))


def check_field(search, listed, load=load_rows):
    """Return ([(level, message)], legacy placeholders used) for one static-data field.

    listed maps (id, version) to the path of that static data file, as its bundle's
    manifest lists it; load reads a path into Rows.
    """
    params = search.get("params") if isinstance(search.get("params"), dict) else {}
    sd_id, version = params.get("id"), params.get("version")
    if not (isinstance(sd_id, str) and sd_id and isinstance(version, str) and version):
        return [("error", "x-search.params.id and .version must be non-empty strings")], []
    path = listed.get((sd_id, version))
    if path is None:
        return [("error", f"static_data {sd_id!r} version {version!r} is not listed in the bundle's manifest")], []
    try:
        rows = load(path)
    except (OSError, ValueError) as err:
        return [("error", f"{path.name}: {err}")], []

    problems, legacy = [], []
    template = search.get("displayTemplate")
    for key in placeholder_keys(template) if isinstance(template, str) else []:
        if key in LEGACY_PLACEHOLDERS:
            legacy.append(key)
        elif not rows.count[key]:
            problems.append(("error", f"displayTemplate {{{key}}} is not a key of any row of {path.name} (keys: {rows.keys()})"))
        elif key in rows.unprintable:
            problems.append(("error", f"displayTemplate {{{key}}} is not a string, number or boolean in {path.name}, so it cannot be printed"))

    if "searchKeys" in params:
        keys = params["searchKeys"]
        if not (isinstance(keys, list) and all(isinstance(k, str) and SEARCH_KEY.fullmatch(k) for k in keys)):
            problems.append(("error", "x-search.params.searchKeys must be a list of key names (letters, digits and underscores)"))
        else:
            distinct = list(dict.fromkeys(keys))
            if len(distinct) > MAX_SEARCH_KEYS:
                problems.append(("error", f"x-search.params.searchKeys names {len(distinct)} different keys, at most {MAX_SEARCH_KEYS} are allowed"))
            for key in distinct:
                if not rows.count[key]:
                    hint = f" (a search matches the rows' own keys: use {LEGACY_PLACEHOLDERS[key]!r})" if key in LEGACY_PLACEHOLDERS else ""
                    problems.append(("error", f"searchKeys names {key!r}, which no row of {path.name} has{hint} (keys: {rows.keys()})"))
                elif key in rows.unprintable:
                    # A search matches strings, numbers and booleans only, as a template prints only these.
                    problems.append(("error", f"searchKeys names {key!r}, which is not a string, number or boolean in {path.name}, so a search never matches it"))
    return problems, legacy


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    errors = fields = 0
    legacy_lines = []
    for manifest in sorted(root.glob("*/manifest.json")):
        bundle = manifest.parent
        if bundle.name.startswith("."):
            continue
        artifacts = [row for row in read_json(manifest).get("artifacts", []) if isinstance(row, dict)]
        listed = {
            (row.get("id"), row.get("version")): bundle / row["path"]
            for row in artifacts
            if row.get("kind") == "static_data" and "path" in row
        }
        for row in artifacts:
            path = str(row.get("path", ""))
            if row.get("kind") != "generic_template" or not path.endswith(".json"):
                continue
            form = bundle / path
            rel = form.relative_to(root).as_posix()
            try:
                document = read_json(form)
            except (OSError, ValueError) as err:
                print(f"::error file={rel}::{rel}: cannot read the form: {err}")
                errors += 1
                continue
            for json_path, search in x_searches(document):
                fields += 1
                problems, legacy = check_field(search, listed)
                for level, message in problems:
                    print(f"::{level} file={rel}::{rel} {json_path}: {message}")
                    errors += level == "error"
                if legacy:
                    names = ", ".join(f"{{{key}}} -> {{{LEGACY_PLACEHOLDERS[key]}}}" for key in legacy)
                    legacy_lines.append(f"{rel} {json_path}: {names}")

    if legacy_lines:
        print(f"::group::{len(legacy_lines)} static-data field(s) use the legacy {{id}} / {{name}}")
        print("\n".join(legacy_lines))
        print("::endgroup::")
        print(
            f"::warning::{len(legacy_lines)} static-data field(s) use {{id}} / {{name}}. They keep working, "
            "because deployed forms use them; new fields should use {const} / {title}."
        )
    if errors:
        print(f"\n{errors} problem(s) in {fields} static-data field(s)")
        return 1
    print(f"{fields} static-data field(s) fit the static data they name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
