#!/usr/bin/env python3
"""Check that every HTML document template carries no active content.

A document template (kind `html_template`, a `.gohtml` file) is rendered into a
document, such as the phytosanitary certificate, that is stored and opened in the
browser from the file store's origin. The renderer (htmlgen, on Go's html/template)
escapes every value for the place it lands, so the template's own markup is what has
to stay safe. A template is safe when:

  * it has no tag that runs code, loads another page or takes input: script, iframe,
    frame, frameset, object, embed, applet, form, input, link, base, svg, math, or a
    meta with http-equiv;
  * it has no event handler attribute (onclick, onerror, ...) and no javascript: URL;
  * it loads nothing from outside: a URL attribute (href, src, srcset, action,
    formaction, poster, background, data, xlink:href, ping) may only point at a
    fragment (#...), and CSS may not @import or take a url() other than a data: one;
  * a value is only ever printed as element text: no {{ }} action sits inside a tag
    (an attribute) or inside a <style> block, where html/template escapes for a URL,
    an attribute or CSS instead.

Go template comments ({{- /* ... */ -}}) and HTML comments are not checked.

The templates are the manifests' html_template entries and every .gohtml file.

Usage: check_html_templates.py [repo-root]
"""
import json
import re
import sys
from pathlib import Path

FORBIDDEN_TAGS = {
    "script", "iframe", "frame", "frameset", "object", "embed", "applet",
    "form", "input", "link", "base", "svg", "math",
}
URL_ATTRIBUTES = {
    "href", "src", "srcset", "action", "formaction", "poster", "background",
    "data", "xlink:href", "ping",
}
RAW_TEXT_TAGS = {"style", "script"}

GO_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_START = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:-]*)")
ATTRIBUTE = re.compile(r"""([^\s"'<>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?""")
SCRIPT_URL = re.compile(r"(?:java|vb)script\s*:", re.IGNORECASE)
CSS_IMPORT = re.compile(r"@import", re.IGNORECASE)
CSS_URL = re.compile(r"url\(\s*(?!['\"]?(?:data:|#))", re.IGNORECASE)
CSS_SCRIPT = re.compile(r"expression\s*\(|behavior\s*:|-moz-binding", re.IGNORECASE)


def blank(match):
    """Replace a match with spaces, keeping its newlines so line numbers hold."""
    return re.sub(r"[^\n]", " ", match.group(0))


def check_css(css, line, problems):
    """Check CSS that starts on line, reporting each problem on its own line."""
    for pattern, message in (
        (CSS_IMPORT, "CSS @import loads another file"),
        (CSS_URL, "CSS url() other than a data: or # one loads from outside"),
        (CSS_SCRIPT, "CSS runs script"),
    ):
        for m in pattern.finditer(css):
            problems.append((line + css.count("\n", 0, m.start()), message))


def check_tag(name, closing, body, line, problems):
    """Check one tag, with its attributes in body. A closing tag has none to check."""
    if closing:
        return
    if name in FORBIDDEN_TAGS:
        problems.append((line, f"<{name}> is not allowed in a document"))
    attrs = {}
    for m in ATTRIBUTE.finditer(body):
        value = (m.group(2) or "").strip("\"'")
        attrs[m.group(1).lower()] = value
    if name == "meta" and "http-equiv" in attrs:
        problems.append((line, "<meta http-equiv> is not allowed in a document"))
    for attr, value in attrs.items():
        if re.fullmatch(r"on[a-z]+", attr):
            problems.append((line, f"event handler attribute {attr}"))
        elif attr in URL_ATTRIBUTES and not value.startswith("#"):
            problems.append((line, f"{attr} may only point at a fragment (#...), not {value!r}"))
        elif attr == "style":
            check_css(value, line, problems)


def scan(source):
    """Return the (line, problem) pairs of one template, in order."""
    text = HTML_COMMENT.sub(blank, GO_COMMENT.sub(blank, source))
    problems = []

    def line_of(index):
        return text.count("\n", 0, index) + 1

    def action_end(index):
        end = text.find("}}", index)
        return len(text) if end < 0 else end + 2

    i, n = 0, len(text)
    while i < n:
        if text.startswith("{{", i):
            i = action_end(i)
            continue
        if text[i] != "<":
            i += 1
            continue
        m = TAG_START.match(text, i)
        if not m:
            # <!DOCTYPE ...> and a stray <: skip to the end of the declaration.
            end = text.find(">", i)
            i = n if end < 0 else end + 1
            continue

        closing, name = bool(m.group(1)), m.group(2).lower()
        tag_line = line_of(i)
        i = m.end()
        body_start, quote = i, ""
        while i < n:
            if text.startswith("{{", i):
                problems.append((line_of(i), f"an action inside <{name}> prints a value into an attribute"))
                i = action_end(i)
                continue
            c = text[i]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c == ">":
                break
            i += 1
        check_tag(name, closing, text[body_start:i], tag_line, problems)
        i += 1

        if not closing and name in RAW_TEXT_TAGS:
            end = re.compile(rf"</{name}\s*>", re.IGNORECASE).search(text, i)
            stop = end.start() if end else n
            raw_line = line_of(i)
            raw = text[i:stop]
            if "{{" in raw:
                problems.append((raw_line + raw[:raw.index("{{")].count("\n"),
                                 f"an action inside <{name}> prints a value into CSS or script"))
            if name == "style":
                check_css(raw, raw_line, problems)
            i = end.end() if end else n

    for m in SCRIPT_URL.finditer(text):
        problems.append((line_of(m.start()), "script URL (javascript: or vbscript:)"))
    return sorted(set(problems))


def manifest_templates(root):
    """Yield the html_template paths each manifest lists, and the manifest they are in."""
    for manifest in sorted(root.glob("*/manifest.json")):
        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("kind") == "html_template" and isinstance(node.get("path"), str):
                    found.append(node["path"])
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(json.loads(manifest.read_text(encoding="utf-8")))
        for path in found:
            yield manifest, manifest.parent / path


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures = 0
    files = {
        p for p in root.rglob("*.gohtml")
        if not any(part.startswith(".") for part in p.relative_to(root).parts)
    }
    for manifest, path in manifest_templates(root):
        if path.is_file():
            files.add(path)
        else:
            print(f"{manifest.relative_to(root)}: html_template {path.relative_to(root)} does not exist")
            failures += 1

    for path in sorted(files):
        for line, problem in scan(path.read_text(encoding="utf-8")):
            print(f"{path.relative_to(root)}:{line}: {problem}")
            failures += 1

    if failures:
        print(f"\n{failures} problem(s) in {len(files)} HTML template(s)")
        return 1
    print(f"{len(files)} HTML template(s) carry no active content")
    return 0


if __name__ == "__main__":
    sys.exit(main())
