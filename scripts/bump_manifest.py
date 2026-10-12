#!/usr/bin/env python3
"""Change artifacts without touching anything a published manifest lists.

A published manifest (manifest_vN.json on main) and every file it lists never change.
To change artifacts, copy each to a new versioned file and point a new manifest at it:

    python3 scripts/bump_manifest.py tnsw tnsw/cda/1-application/render.json [more files...]

For each file this copies it to its next versioned name (render.json -> render_v2.json,
render_v2.json -> render_v3.json) for you to edit, and points every row of the working
manifest that listed the old path at the new one. A file this branch added is not
published yet, so it is left for you to edit in place. The working manifest is the highest
manifest_vN.json if this branch added it (it is not on origin/main yet), so one branch
makes one new manifest however often you run this; otherwise it is a new
manifest_v(N+1).json copied from the highest.

With no files it only makes sure a working manifest exists and prints its path, for adding
rows for brand-new artifacts by hand.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

MANIFEST = re.compile(r"^manifest_v([1-9][0-9]*)\.json$")
VERSIONED = re.compile(r"^(?P<stem>.*?)(?:_v(?P<n>[0-9]+))?$")


def manifest_versions(bundle):
    versions = sorted(int(m.group(1)) for m in (MANIFEST.match(f) for f in os.listdir(bundle)) if m)
    if not versions:
        sys.exit(f"{bundle}: no manifest_v1.json")
    if versions != list(range(1, len(versions) + 1)):
        sys.exit(f"{bundle}: manifest versions are not contiguous from 1: {versions}")
    return versions


def published(path):
    """Whether path exists on origin/main. Unknown counts as published, the safe side."""
    try:
        return subprocess.run(["git", "cat-file", "-e", f"origin/main:{path}"],
                              capture_output=True).returncode == 0
    except OSError:
        return True


def next_path(rel):
    """render.json -> render_v2.json, render_v2.json -> render_v3.json; skips names in use."""
    head, name = os.path.split(rel)
    stem, ext = os.path.splitext(name)
    m = VERSIONED.match(stem)
    n = int(m.group("n") or 1) + 1
    while True:
        candidate = os.path.join(head, f"{m.group('stem')}_v{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def write_manifest(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", help="bundle folder, e.g. tnsw")
    parser.add_argument("files", nargs="*", help="artifact files to change, relative to the repo root or the bundle")
    args = parser.parse_args()

    bundle = args.bundle.rstrip("/")
    versions = manifest_versions(bundle)
    latest = os.path.join(bundle, f"manifest_v{versions[-1]}.json")
    if published(latest):
        working = os.path.join(bundle, f"manifest_v{versions[-1] + 1}.json")
        shutil.copyfile(latest, working)
        print(f"created {working} from {latest}")
    else:
        working = latest
        print(f"editing {working}, which this branch added")

    with open(working, encoding="utf-8") as f:
        cfg = json.load(f)

    for arg in args.files:
        rel = arg[len(bundle) + 1:] if arg.startswith(bundle + "/") else arg
        if not published(os.path.join(bundle, rel)):
            print(f"{bundle}/{rel} is new on this branch, not published: edit it in place")
            continue
        rows = [r for r in cfg["artifacts"] if r.get("path") == rel]
        if not rows:
            sys.exit(f"{working} lists no artifact at {rel!r}; already bumped, or a typo?")
        new_rel = next_path(os.path.join(bundle, rel))[len(bundle) + 1:]
        shutil.copyfile(os.path.join(bundle, rel), os.path.join(bundle, new_rel))
        for r in rows:
            r["path"] = new_rel
        ids = ", ".join(f"{r['id']} ({r['kind']})" for r in rows)
        print(f"copied {bundle}/{rel} -> {bundle}/{new_rel}; now edit it. Points: {ids}")

    write_manifest(working, cfg)


if __name__ == "__main__":
    main()
