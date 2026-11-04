#!/usr/bin/env python3
"""Publish one bundle's current manifest.json as an immutable release.

The repository holds only the current state of each bundle: one manifest.json
and files edited in place. A release turns that state into version X in an
artifact store, which only ever grows:

  A) X is the highest manifest_vN.json in the store plus one (1 if there is
     none). Versions are found the way the backend finds them: by reading
     manifest_v1.json, manifest_v2.json, ... until one is missing.
  B) Each file the manifest lists is stored under a name that carries the first
     16 hex characters of its SHA-256, e.g. npqs/1-apply/render.json becomes
     npqs/1-apply/render.3fa9c1e07b2d4a61.json.
  C) A file is uploaded only if that name isn't in the store yet, so a file that
     didn't change is shared by every version that lists it.
  D) manifest_vX.json is written last, and only if no manifest_vX.json exists.

Nothing is overwritten or deleted, so a flow pinned to an older version keeps
reading exactly what it read when it started.

Each manifest_vX.json records where it came from: "version", "commit" (the git
commit of the bundle) and "released_at" at the top, and on every row "source"
(the file's path in the repository) and "since" (the version in which the file
last changed). The rows whose "since" is X are what release X changed.

A release that would change nothing (every row the same as in the highest
published manifest) publishes nothing and exits successfully, so the script can
run on every merge.

A row with a "version" (static data a form fetches by id and version) must keep
its content in every release: to change it, add a row with the next version.
The backend refuses to load a store where an (id, kind, version) points at
different files, so the release refuses it first.

Destinations:
  s3://<bucket>[/<prefix>]  an S3-compatible bucket, such as Cloudflare R2.
                            Credentials come from the usual AWS environment
                            variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY);
                            pass --endpoint-url (or set AWS_ENDPOINT_URL) for R2.
                            Needs boto3.
  <directory>               a local directory, for a local backend or a test.

The prefix is where the backend's artifact loader points (its s3.prefix), so it
need not match the bundle's folder name.

Before writing, a release shows what it would publish (destination, commit,
version, and each changed or removed row) and asks on the terminal. --dry-run
shows the same and stops; --yes publishes without asking, for a wrapper or CI
that has already reviewed a dry run. Without a terminal and without --yes, the
release is refused.

Examples:
  # Preview releasing tnsw to the dev bucket.
  scripts/release_manifest.py tnsw --dest s3://tnsw-artifacts-dev/tnsw \\
      --endpoint-url https://<account>.r2.cloudflarestorage.com --dry-run

  # Release every bundle to a local directory without asking, reporting as JSON.
  for b in cda efc fcau npqs sltb tnsw; do
    scripts/release_manifest.py "$b" --dest "/tmp/artifacts/$b" --yes --json
  done

Exit status: 0 on a release, a dry run, or a release with nothing to publish; 1
when the bundle or the store is refused or the release isn't confirmed; 2 on bad
arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import posixpath
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

MANIFEST = "manifest.json"
HASH_CHARS = 16


def manifest_path(version: int) -> str:
    return f"manifest_v{version}.json"


class ReleaseError(Exception):
    """The bundle or the store is in a state the release refuses."""


class ConflictError(ReleaseError):
    """Another release wrote the same object first."""


# --- stores -------------------------------------------------------------------


class Store(Protocol):
    def get(self, key: str) -> bytes | None:
        """Return the object at key, or None if there is none."""

    def exists(self, key: str) -> bool: ...

    def put_new(self, key: str, data: bytes, content_type: str) -> None:
        """Write key only if it doesn't exist; raise ConflictError if it does."""

    def describe(self) -> str: ...


class LocalStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*key.split("/"))

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put_new(self, key: str, data: bytes, content_type: str) -> None:
        # The bytes go to a temporary file first and are linked into place whole, so no
        # reader or later release sees a partly written file under its final name.
        # os.link fails if the name exists, which keeps the write create-only.
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, p)
            except FileExistsError:
                raise ConflictError(f"{key} already exists in {self.describe()}") from None
        finally:
            os.unlink(tmp)

    def describe(self) -> str:
        return str(self.root)


def s3_region(region: str | None, endpoint_url: str | None, configured: str | None) -> str | None:
    """The region to sign S3 requests for.

    --region wins, then the region boto3 finds in the environment or the AWS profile
    (configured). Without either, a custom endpoint such as R2 gets "auto", which R2
    expects; AWS itself gets None, so boto3 uses its own default.
    """
    if region:
        return region
    if configured:
        return configured
    if endpoint_url or os.environ.get("AWS_ENDPOINT_URL"):
        return "auto"
    return None


class S3Store:
    def __init__(
        self,
        bucket: str,
        prefix: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        client=None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise ReleaseError(
                    "an s3:// destination needs boto3 (pip install boto3)"
                ) from None
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=s3_region(region, endpoint_url, boto3.Session().region_name),
                # R2 rejects the checksum headers newer boto3 adds by default.
                config=Config(
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        self.client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _missing(err) -> bool:
        code = err.response.get("Error", {}).get("Code", "")
        return code in ("NoSuchKey", "404", "NotFound")

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            out = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as err:
            if self._missing(err):
                return None
            raise
        return out["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as err:
            if self._missing(err):
                return False
            raise
        return True

    def put_new(self, key: str, data: bytes, content_type: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=data,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise ConflictError(
                    f"{key} already exists in {self.describe()}"
                ) from None
            raise

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"


def open_store(dest: str, endpoint_url: str | None, region: str | None) -> Store:
    if dest.startswith("s3://"):
        bucket, _, prefix = dest[len("s3://") :].partition("/")
        if not bucket:
            raise ReleaseError(f"--dest {dest!r} names no bucket")
        return S3Store(bucket, prefix, endpoint_url=endpoint_url, region=region)
    if "://" in dest:
        raise ReleaseError(f"--dest {dest!r}: only s3:// and local directories are supported")
    return LocalStore(dest)


# --- the release ----------------------------------------------------------------


def hashed_path(source: str, data: bytes) -> str:
    """render.json -> render.<hash>.json, keeping the file's folder and extension."""
    digest = hashlib.sha256(data).hexdigest()[:HASH_CHARS]
    folder, name = posixpath.split(source)
    stem, ext = posixpath.splitext(name)
    if not stem:  # a dotfile such as ".config"
        stem, ext = name, ""
    return posixpath.join(folder, f"{stem}.{digest}{ext}")


def content_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json"
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith("text/"):
        return f"{guessed}; charset=utf-8"
    return guessed or "application/octet-stream"


def row_key(row: dict) -> tuple[str, str, str]:
    return (row["id"], row["kind"], row.get("version") or "")


def comparable(row: dict) -> dict:
    """A row without the fields that only describe its release."""
    return {k: v for k, v in row.items() if k != "since"}


@dataclass
class Plan:
    version: int
    latest: int
    manifest: dict
    uploads: dict[str, bytes] = field(default_factory=dict)  # stored path -> content
    shared: int = 0  # files already in the store
    changed: list[str] = field(default_factory=list)  # sources whose row is new in this version
    removed: list[str] = field(default_factory=list)  # ids the latest manifest listed and this one doesn't

    @property
    def publishes(self) -> bool:
        return self.version > self.latest


def read_bundle(bundle: Path) -> tuple[list[dict], dict[str, bytes]]:
    """The bundle's manifest rows and the content of every file they list."""
    try:
        manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError(f"{bundle}/{MANIFEST} does not exist") from None
    except json.JSONDecodeError as err:
        raise ReleaseError(f"{bundle}/{MANIFEST} is not valid JSON: {err}") from None

    rows = manifest.get("artifacts")
    if not isinstance(rows, list):
        raise ReleaseError(f"{bundle}/{MANIFEST} has no artifacts list")

    problems: list[str] = []
    seen: dict[tuple[str, str, str], int] = {}
    files: dict[str, bytes] = {}
    root = bundle.resolve()
    for i, row in enumerate(rows):
        where = f"artifacts[{i}]"
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) and row.get(k) for k in ("id", "kind", "path")
        ):
            problems.append(f"{where}: needs a non-empty id, kind and path")
            continue
        key = row_key(row)
        if key in seen:
            problems.append(
                f"{where}: {key[0]} ({key[1]}"
                + (f", version {key[2]}" if key[2] else "")
                + f") is also artifacts[{seen[key]}]"
            )
        seen.setdefault(key, i)

        path = row["path"]
        if path.startswith("/") or ".." in path.split("/"):
            problems.append(f"{where}: path {path!r} leaves the bundle")
            continue
        if path in files:
            continue
        full = (bundle / path).resolve()
        if root not in full.parents:
            problems.append(f"{where}: path {path!r} leaves the bundle")
            continue
        try:
            data = full.read_bytes()
        except FileNotFoundError:
            problems.append(f"{where}: {path} does not exist")
            continue
        if path.endswith(".json"):
            try:
                json.loads(data)
            except ValueError as err:
                problems.append(f"{where}: {path} is not valid JSON: {err}")
                continue
        files[path] = data

    if problems:
        raise ReleaseError(f"{bundle}/{MANIFEST} cannot be released:\n  " + "\n  ".join(problems))
    return rows, files


def published_manifests(store: Store) -> list[dict]:
    """Every published manifest, oldest first, up to the first missing version."""
    out: list[dict] = []
    while True:
        version = len(out) + 1
        raw = store.get(manifest_path(version))
        if raw is None:
            return out
        try:
            out.append(json.loads(raw))
        except ValueError as err:
            raise ReleaseError(
                f"{manifest_path(version)} in {store.describe()} is not valid JSON: {err}"
            ) from None


def plan_release(
    bundle: Path, store: Store, commit: str, released_at: str
) -> Plan:
    rows, files = read_bundle(bundle)
    published = published_manifests(store)
    latest = len(published)
    version = latest + 1

    stored = {source: hashed_path(source, data) for source, data in files.items()}

    # An explicitly versioned row names the same file in every manifest that lists it.
    explicit: dict[tuple[str, str, str], tuple[str, int]] = {}
    for n, m in enumerate(published, start=1):
        for row in m.get("artifacts", []):
            key = row_key(row)
            if key[2]:
                explicit.setdefault(key, (row["path"], n))
    problems = []
    for row in rows:
        key = row_key(row)
        if key[2] and key in explicit and explicit[key][0] != stored[row["path"]]:
            problems.append(
                f"{key[0]} ({key[1]}) version {key[2]}: {row['path']} differs from the "
                f"file manifest_v{explicit[key][1]}.json published for that version; "
                "add a row with a new version instead of changing this one"
            )
    if problems:
        raise ReleaseError("release refused:\n  " + "\n  ".join(problems))

    previous = {row_key(r): r for r in published[-1].get("artifacts", [])} if published else {}

    out_rows: list[dict] = []
    changed: list[str] = []
    for row in rows:
        new = dict(row)
        new["path"] = stored[row["path"]]
        new["source"] = row["path"]
        before = previous.get(row_key(row))
        if before is not None and comparable(before) == new:
            new["since"] = before.get("since", latest)
        else:
            new["since"] = version
            changed.append(row["path"])
        out_rows.append(new)

    current = {row_key(r) for r in rows}
    removed = sorted({k[0] for k in previous if k not in current})

    if published and not changed and not removed:
        return Plan(version=latest, latest=latest, manifest=published[-1])

    plan = Plan(
        version=version,
        latest=latest,
        manifest={
            "version": version,
            "commit": commit,
            "released_at": released_at,
            "artifacts": out_rows,
        },
        changed=changed,
        removed=removed,
    )
    for source, path in stored.items():
        if store.exists(path):
            plan.shared += 1
        else:
            plan.uploads[path] = files[source]
    return plan


def apply(plan: Plan, store: Store) -> None:
    for path, data in sorted(plan.uploads.items()):
        try:
            store.put_new(path, data, content_type(path))
        except ConflictError:
            pass  # another release stored the same content under the same hash
    body = (json.dumps(plan.manifest, indent=2, ensure_ascii=False) + "\n").encode()
    try:
        store.put_new(manifest_path(plan.version), body, "application/json")
    except ConflictError:
        raise ConflictError(
            f"{manifest_path(plan.version)} was published by another release while this "
            f"one ran; nothing of this release is live. Run it again."
        ) from None


def changed_files(plan: Plan) -> list[dict]:
    """Each changed file: its repository path, its path in the store, and whether it uploads."""
    stored = {r["source"]: r["path"] for r in plan.manifest["artifacts"] if "source" in r}
    return [
        {"source": src, "path": stored[src], "upload": stored[src] in plan.uploads}
        for src in plan.changed
    ]


def describe(plan: Plan, bundle: Path, store: Store, commit: str, log) -> None:
    """Print what the release would publish."""
    log(f"bundle     {bundle}")
    log(f"dest       {store.describe()}")
    log(f"commit     {commit}")
    log(
        f"version    manifest_v{plan.version}.json"
        + (f" (latest published: v{plan.latest})" if plan.latest else " (first release)")
    )
    log(f"rows       {len(plan.manifest['artifacts'])}: {len(plan.changed)} changed, {len(plan.removed)} removed")
    log(f"files      {len(plan.uploads)} to upload, {plan.shared} already stored")
    for c in changed_files(plan):
        state = "upload" if c["upload"] else "already stored"
        log(f"  changed  {c['source']} -> {c['path']} ({state})")
    for ident in plan.removed:
        log(f"  removed  {ident}")


def confirm(plan: Plan, store: Store) -> bool:
    """Ask on the terminal whether to publish. Without a terminal, refuse: pass --yes."""
    if not sys.stdin.isatty():
        raise ReleaseError(
            "no terminal to confirm the release on; review it with --dry-run, then pass --yes"
        )
    print(
        f"Publish manifest_v{plan.version}.json to {store.describe()}? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def git_commit(bundle: Path, allow_dirty: bool) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=bundle, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--", ".")
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise ReleaseError(f"{bundle} is not in a git checkout; pass --commit") from None
    if dirty and not allow_dirty:
        raise ReleaseError(
            f"{bundle} has uncommitted changes, so commit {commit[:12]} doesn't describe "
            "what would be released. Commit them, or pass --allow-dirty."
        )
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a bundle's manifest.json as the next immutable manifest_vN.json.",
        epilog="See the module docstring for the release rules and examples.",
    )
    parser.add_argument("bundle", help="the bundle folder, e.g. tnsw or npqs")
    parser.add_argument(
        "--dest",
        required=True,
        help="s3://<bucket>[/<prefix>] or a local directory; the backend's loader points here",
    )
    parser.add_argument("--endpoint-url", help="S3 endpoint, e.g. https://<account>.r2.cloudflarestorage.com")
    parser.add_argument(
        "--region",
        help='S3 region (default: the AWS environment or profile; "auto" with a custom endpoint such as R2)',
    )
    parser.add_argument("--commit", help="commit to record (default: the bundle's git HEAD)")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="release even if the bundle has uncommitted changes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what the release would publish and write nothing",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="publish without asking; without it, a release asks on the terminal first",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a JSON summary on stdout (progress goes to stderr)",
    )
    args = parser.parse_args(argv)

    out = sys.stderr if args.json else sys.stdout

    def log(*a):
        print(*a, file=out, flush=True)

    bundle = Path(args.bundle)
    try:
        if not bundle.is_dir():
            raise ReleaseError(f"{bundle} is not a folder")
        commit = args.commit or git_commit(bundle, args.allow_dirty)
        store = open_store(args.dest, args.endpoint_url, args.region)
        released_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        plan = plan_release(bundle, store, commit, released_at)

        published = False
        if not plan.publishes:
            log(f"{bundle}: nothing changed since manifest_v{plan.latest}.json in {store.describe()}")
        else:
            describe(plan, bundle, store, commit, log)
            if args.dry_run:
                log("dry run: nothing written")
            elif not args.yes and not confirm(plan, store):
                log("not published")
                return 1
            else:
                apply(plan, store)
                published = True
                log(f"published manifest_v{plan.version}.json")
    except ReleaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(
            {
                "bundle": str(bundle),
                "dest": store.describe(),
                "version": plan.version,
                "published": published,
                "dry_run": args.dry_run,
                "changed": changed_files(plan),
                "removed": plan.removed,
                "uploaded": len(plan.uploads) if plan.publishes else 0,
                "commit": commit,
            },
            sys.stdout,
            indent=2,
        )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
