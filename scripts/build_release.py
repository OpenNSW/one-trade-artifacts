#!/usr/bin/env python3
"""Build the next release snapshot of every artifact bundle.

The repository holds only the current state of each bundle: one manifest.json
and files edited in place. A release turns that state into an immutable,
environment-agnostic snapshot, built from the previous release plus what changed
since it. This script only builds the snapshot into a folder; publishing it as a
GitHub release is the release workflow's job, and deploying it is out of scope.

Inputs:
  --repo      the repository at the release commit (default: the current folder).
  --baseline  a folder holding the previous release's assets. Only its summary,
              release.json, is read. Leave it out for the first release, where
              every bundle starts at v1.

Per bundle (cda, efc, fcau, npqs, sltb, tnsw, each read from <bundle>/manifest.json):
  - Each listed file is stored under a name that carries the first 16 hex
    characters of its SHA-256, e.g. 1-apply/render.json becomes
    1-apply/render.3fa9c1e07b2d4a61.json, so an unchanged file keeps its name
    across versions.
  - Each row gets the hashed "path", "source" (its path in the repository) and
    "since" (the version it last changed in): copied from the previous snapshot
    when the row is unchanged, otherwise this version.
  - If any row changed or was removed, the bundle's next version N is the
    previous one plus one, and manifest_vN.json records "version", "commit" and
    "released_at". An unchanged bundle is skipped and keeps its version.

The build is refused (exit 1, listing every problem, writing nothing) on:
  - a duplicate (id, kind, version) row;
  - a listed file that is missing, leaves the bundle, or (for .json) is not valid JSON;
  - a versioned row (static data a form fetches by id and version) whose content
    differs from the file an earlier release published for that (id, version),
    compared by hash, so moving the file is fine; add the next version instead;
  - uncommitted changes in a bundle, since the recorded commit must describe
    what is built (--allow-dirty skips this, for a local trial build).

Outputs, in --out (which must be empty or absent):
  <bundle>.tar.gz   one per changed bundle: its manifest_vN.json and every file
                    it lists, at their hashed paths.
  snapshot.tar.gz   every bundle at its current version, changed or not, each
                    under <bundle>/: its manifest_vN.json and every file it
                    lists. One download holds the whole state of the release.
  release.json      the summary: per bundle, its current version, whether this
                    build changed it, and its changed and removed rows. It also
                    carries each bundle's latest manifest and every versioned row
                    ever published, which is what the next build reads as its
                    baseline, so a bundle skipped here is still known next time.
  release-notes.md  the per-bundle changes, for the release notes.

release.json is written even when nothing changed; its top-level "changed" is
false then, and nothing should be published.

Examples:
  # The first release.
  scripts/build_release.py --out build/

  # The next release, from the previous release's assets.
  gh release download <tag> --dir baseline/
  scripts/build_release.py --baseline baseline/ --out build/

Exit status: 0 when built (changed or not); 1 when the build is refused; 2 on
bad arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import posixpath
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

BUNDLES = ("cda", "efc", "fcau", "npqs", "sltb", "tnsw")
MANIFEST = "manifest.json"
SUMMARY = "release.json"
SNAPSHOT = "snapshot.tar.gz"
NOTES = "release-notes.md"
SUMMARY_FORMAT = 1
HASH_CHARS = 16


class BuildError(Exception):
    """The repository, the baseline or the output folder is in a state the build refuses."""


def manifest_name(version: int) -> str:
    return f"manifest_v{version}.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hashed_path(source: str, data: bytes) -> str:
    """render.json -> render.<hash>.json, keeping the file's folder and extension."""
    digest = sha256(data)[:HASH_CHARS]
    folder, name = posixpath.split(source)
    stem, ext = posixpath.splitext(name)
    if not stem:  # a dotfile such as ".config"
        stem, ext = name, ""
    return posixpath.join(folder, f"{stem}.{digest}{ext}")


def row_key(row: dict) -> tuple[str, str, str]:
    return (row["id"], row["kind"], row.get("version") or "")


def key_text(key: tuple[str, str, str]) -> str:
    return f"{key[0]} ({key[1]}" + (f", version {key[2]})" if key[2] else ")")


def comparable(row: dict) -> dict:
    """A row without the field that only describes its release."""
    return {k: v for k, v in row.items() if k != "since"}


def dumps(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode()


# --- reading --------------------------------------------------------------------


def read_bundle(bundle: Path, name: str) -> tuple[list[dict], dict[str, bytes], list[str]]:
    """The bundle's manifest rows, the content of every file they list, and its problems."""
    where_manifest = f"{name}/{MANIFEST}"
    try:
        manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], {}, [f"{where_manifest} does not exist"]
    except ValueError as err:
        return [], {}, [f"{where_manifest} is not valid JSON: {err}"]
    rows = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(rows, list):
        return [], {}, [f"{where_manifest} has no artifacts list"]

    problems: list[str] = []
    good: list[dict] = []
    seen: dict[tuple[str, str, str], int] = {}
    files: dict[str, bytes] = {}
    root = bundle.resolve()
    for i, row in enumerate(rows):
        where = f"{where_manifest} artifacts[{i}]"
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) and row.get(k) for k in ("id", "kind", "path")
        ):
            problems.append(f"{where}: needs a non-empty id, kind and path")
            continue
        if not isinstance(row.get("version", ""), str):
            problems.append(f"{where}: version must be a string")
            continue
        key = row_key(row)
        if key in seen:
            problems.append(f"{where}: {key_text(key)} is also artifacts[{seen[key]}]")
            continue
        seen[key] = i

        path = row["path"]
        if path not in files:
            full = (bundle / path).resolve()
            if path.startswith("/") or ".." in path.split("/") or root not in full.parents:
                problems.append(f"{where}: path {path!r} leaves the bundle")
                continue
            try:
                data = full.read_bytes()
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                problems.append(f"{where}: {path} does not exist")
                continue
            if path.endswith(".json"):
                try:
                    json.loads(data)
                except ValueError as err:
                    problems.append(f"{where}: {path} is not valid JSON: {err}")
                    continue
            files[path] = data
        good.append(row)
    return good, files, problems


@dataclass
class Previous:
    """A bundle as the previous release left it."""

    version: int = 0
    manifest: dict | None = None
    # every versioned row ever published: key -> {"sha256", "path", "since"}
    versioned: dict[tuple[str, str, str], dict] = field(default_factory=dict)


def load_baseline(folder: Path | None) -> dict[str, Previous]:
    """Each bundle's state after the previous release, read from its release.json."""
    if folder is None:
        return {}
    path = folder / SUMMARY
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError(
            f"{path} does not exist; a baseline must hold the previous release's {SUMMARY} "
            "(leave --baseline out only for the first release)"
        ) from None
    except ValueError as err:
        raise BuildError(f"{path} is not valid JSON: {err}") from None
    if not isinstance(summary, dict) or summary.get("format") != SUMMARY_FORMAT:
        raise BuildError(f"{path} is not a format {SUMMARY_FORMAT} release summary")

    out: dict[str, Previous] = {}
    try:
        for name, b in summary["bundles"].items():
            version = b["version"]
            manifest = b["manifest"]
            if not isinstance(version, int) or version < 1 or manifest.get("version") != version:
                raise ValueError(f"bundle {name} has no manifest for its version")
            versioned = {
                (v["id"], v["kind"], v["version"]): {
                    "sha256": v["sha256"],
                    "path": v["path"],
                    "since": v["since"],
                }
                for v in b["versioned"]
            }
            for row in manifest["artifacts"]:
                row_key(row), row["path"]  # every row has what a build compares
            out[name] = Previous(version=version, manifest=manifest, versioned=versioned)
    except (KeyError, TypeError, AttributeError, ValueError) as err:
        raise BuildError(f"{path} is not a valid release summary: {err!r}") from None
    return out


# --- building -------------------------------------------------------------------


@dataclass
class BundleBuild:
    name: str
    version: int  # the bundle's version after this build
    previous: int  # its version before (0 if it had none)
    manifest: dict
    versioned: dict[tuple[str, str, str], dict]
    # hashed path -> content of every file the manifest lists; for an unchanged bundle
    # these are the repository's files, which hash to the previous manifest's paths
    files: dict[str, bytes] = field(default_factory=dict)
    changed: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)

    @property
    def released(self) -> bool:
        return self.version > self.previous

    @property
    def archive(self) -> str | None:
        return f"{self.name}.tar.gz" if self.released else None


def build_bundle(
    name: str,
    rows: list[dict],
    files: dict[str, bytes],
    prev: Previous,
    commit: str,
    released_at: str,
) -> tuple[BundleBuild | None, list[str]]:
    version = prev.version + 1
    stored = {source: hashed_path(source, data) for source, data in files.items()}

    problems = []
    versioned = {k: dict(v) for k, v in prev.versioned.items()}
    for row in rows:
        key = row_key(row)
        if not key[2]:
            continue
        digest = sha256(files[row["path"]])
        published = versioned.get(key)
        if published is None:
            versioned[key] = {"sha256": digest, "path": stored[row["path"]], "since": version}
        elif published["sha256"] != digest:
            problems.append(
                f"{name}: {key_text(key)}: {row['path']} differs from the file "
                f"{manifest_name(published['since'])} published for that version; "
                "add a row with the next version instead of changing this one"
            )
    if problems:
        return None, problems

    before = {row_key(r): r for r in prev.manifest["artifacts"]} if prev.manifest else {}
    out_rows: list[dict] = []
    changed: list[dict] = []
    for row in rows:
        new = dict(row)
        new["path"] = stored[row["path"]]
        new["source"] = row["path"]
        old = before.get(row_key(row))
        if old is not None and comparable(old) == new:
            new["since"] = old.get("since", prev.version)
        else:
            new["since"] = version
            changed.append(new)
        out_rows.append(new)
    current = {row_key(r) for r in rows}
    removed = [
        {"id": k[0], "kind": k[1], "version": k[2]} for k in before if k not in current
    ]

    if prev.manifest is not None and not changed and not removed:
        return (
            BundleBuild(
                name, prev.version, prev.version, prev.manifest, prev.versioned,
                files={stored[source]: data for source, data in files.items()},
            ),
            [],
        )

    build = BundleBuild(
        name=name,
        version=version,
        previous=prev.version,
        manifest={
            "version": version,
            "commit": commit,
            "released_at": released_at,
            "artifacts": out_rows,
        },
        versioned=versioned,
        files={stored[source]: data for source, data in files.items()},
        changed=changed,
        removed=removed,
    )
    return build, []


def git_commit(repo: Path, bundles: list[str], allow_dirty: bool) -> tuple[str, list[str]]:
    """The repository's HEAD, and a problem for each bundle with uncommitted changes."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        problems = []
        if not allow_dirty:
            for name in bundles:
                if git("status", "--porcelain", "--", name):
                    problems.append(
                        f"{name} has uncommitted changes, so commit {commit[:12]} doesn't "
                        "describe what would be built; commit them, or pass --allow-dirty"
                    )
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise BuildError(f"{repo} is not a git checkout; pass --commit") from None
    return commit, problems


def build(
    repo: Path,
    baseline: dict[str, Previous],
    commit: str,
    released_at: str,
    bundles: list[str],
    problems: list[str] | None = None,
) -> list[BundleBuild]:
    """Build every bundle, or raise BuildError listing every problem."""
    problems = list(problems or [])
    builds: list[BundleBuild] = []
    for name in bundles:
        rows, files, found = read_bundle(repo / name, name)
        problems += found
        if found:
            continue
        b, found = build_bundle(name, rows, files, baseline.get(name, Previous()), commit, released_at)
        problems += found
        if b is not None:
            builds.append(b)
    gone = sorted(set(baseline) - set(bundles))
    if gone:
        problems.append(f"the baseline has bundles this build doesn't: {', '.join(gone)}")
    if problems:
        raise BuildError("build refused:\n  " + "\n  ".join(problems))
    return builds


# --- writing --------------------------------------------------------------------


def bundle_entries(b: BundleBuild, prefix: str = "") -> dict[str, bytes]:
    """manifest_vN.json and every file it lists, by their path under prefix."""
    entries = dict(b.files)
    entries[manifest_name(b.version)] = dumps(b.manifest)
    return {prefix + path: data for path, data in entries.items()}


def archive_bytes(entries: dict[str, bytes], mtime: int) -> bytes:
    """A gzipped tar of entries (path -> content), the same for the same input."""
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=mtime) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(entries):
                info = tarfile.TarInfo(path)
                info.size = len(entries[path])
                info.mtime = mtime
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(entries[path]))
    return raw.getvalue()


def summary(builds: list[BundleBuild], commit: str, released_at: str) -> dict:
    return {
        "format": SUMMARY_FORMAT,
        "commit": commit,
        "released_at": released_at,
        "changed": any(b.released for b in builds),
        "snapshot": SNAPSHOT if any(b.released for b in builds) else None,
        "bundles": {
            b.name: {
                "version": b.version,
                "previous": b.previous,
                "released": b.released,
                "archive": b.archive,
                "changed": [
                    {k: r.get(k, "") for k in ("id", "kind", "version", "source", "path")}
                    for r in b.changed
                ],
                "removed": b.removed,
                "manifest": b.manifest,
                "versioned": [
                    {"id": k[0], "kind": k[1], "version": k[2], **v}
                    for k, v in sorted(b.versioned.items())
                ],
            }
            for b in builds
        },
    }


def notes(builds: list[BundleBuild]) -> str:
    def row(r: dict) -> str:
        version = f" v{r['version']}" if r.get("version") else ""
        source = f" — `{r['source']}`" if r.get("source") else ""
        return f"- `{r['id']}` ({r['kind']}{version}){source}"

    out = ["## Bundles", ""]
    for b in builds:
        if not b.released:
            out += [f"### {b.name} — unchanged (v{b.version})", ""]
            continue
        if b.previous == 0:
            out += [
                f"### {b.name} — v{b.version} (first release)",
                "",
                f"{len(b.manifest['artifacts'])} rows.",
                "",
            ]
            continue
        out += [f"### {b.name} — v{b.version} (was v{b.previous})", ""]
        if b.changed:
            out += [f"Changed ({len(b.changed)}):", ""] + [row(r) for r in b.changed] + [""]
        if b.removed:
            out += [f"Removed ({len(b.removed)}):", ""] + [row(r) for r in b.removed] + [""]
    return "\n".join(out)


def write(out: Path, builds: list[BundleBuild], commit: str, released_at: str) -> None:
    mtime = int(dt.datetime.strptime(released_at, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=dt.timezone.utc).timestamp())
    out.mkdir(parents=True, exist_ok=True)
    for b in builds:
        if b.released:
            (out / b.archive).write_bytes(archive_bytes(bundle_entries(b), mtime))
    if any(b.released for b in builds):
        snapshot: dict[str, bytes] = {}
        for b in builds:
            snapshot.update(bundle_entries(b, f"{b.name}/"))
        (out / SNAPSHOT).write_bytes(archive_bytes(snapshot, mtime))
    (out / NOTES).write_text(notes(builds), encoding="utf-8")
    (out / SUMMARY).write_bytes(dumps(summary(builds, commit, released_at)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the next release snapshot of every bundle.",
        epilog="See the module docstring for the release rules and examples.",
    )
    parser.add_argument("--repo", default=".", help="the repository at the release commit (default: .)")
    parser.add_argument(
        "--baseline",
        help=f"a folder holding the previous release's assets ({SUMMARY}); leave out for the first release",
    )
    parser.add_argument("--out", required=True, help="the folder to build into; must be empty or absent")
    parser.add_argument("--commit", help="commit to record (default: the repository's HEAD, which must be clean)")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build even if a bundle has uncommitted changes (for a local trial build)",
    )
    parser.add_argument(
        "--released-at",
        help="release time to record, as YYYY-MM-DDTHH:MM:SSZ (default: now)",
    )
    parser.add_argument(
        "--bundles",
        default=",".join(BUNDLES),
        help=f"comma-separated bundle folders (default: {','.join(BUNDLES)})",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    out = Path(args.out)
    bundles = [b for b in args.bundles.split(",") if b]
    released_at = args.released_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        try:
            dt.datetime.strptime(released_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise BuildError(f"--released-at {released_at!r} is not YYYY-MM-DDTHH:MM:SSZ") from None
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise BuildError(f"{out} is not an empty folder")
        if args.commit:
            commit, problems = args.commit, []
        else:
            commit, problems = git_commit(repo, bundles, args.allow_dirty)
        baseline = load_baseline(Path(args.baseline) if args.baseline else None)
        builds = build(repo, baseline, commit, released_at, bundles, problems)
        write(out, builds, commit, released_at)
    except BuildError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    for b in builds:
        if b.released:
            print(
                f"{b.name}: v{b.version}"
                + (f" (was v{b.previous})" if b.previous else " (first release)")
                + f", {len(b.changed)} changed, {len(b.removed)} removed -> {b.archive}"
            )
        else:
            print(f"{b.name}: unchanged (v{b.version})")
    if not any(b.released for b in builds):
        print("nothing changed; nothing to publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
