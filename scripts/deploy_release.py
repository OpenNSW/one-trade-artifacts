#!/usr/bin/env python3
"""Deploy one bundle of a GitHub release into an artifact store.

A release (scripts/build_release.py, published by .github/workflows/release.yml)
decides every version: its snapshot.tar.gz holds, under <bundle>/, every
manifest_vN.json of that bundle from v1 up and every file any of them lists, at
content-hashed names. Deploying copies that into a store the backend's artifact
loader reads (its s3.prefix), and decides nothing itself:

  A) The release is checked first: its manifests run from manifest_v1.json up
     with no gap, every file they list is there with content matching the hash
     in its name, and it holds nothing else.
  B) The store's manifests are read the way the backend reads them:
     manifest_v1.json, manifest_v2.json, ... until one is missing. Each must be
     byte for byte the release's manifest of that version. A store holding a
     different manifest_vN.json was filled from somewhere else, and the deploy
     is refused: clear the store, or deploy to another prefix.
  C) The files the missing manifests list are uploaded, except the ones the
     store's manifests already list or the store already holds.
  D) The missing manifests are written last, oldest first, each only if it
     doesn't exist yet, after every upload has succeeded.

Nothing is overwritten or deleted, so a flow pinned to an older version keeps
reading exactly what it read when it started. Deploying a release the store
already holds, or one older than what it holds, writes nothing and succeeds.

Destinations:
  s3://<bucket>[/<prefix>]  an S3-compatible bucket, such as Cloudflare R2.
                            Credentials come from the usual AWS environment
                            variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY);
                            pass --endpoint-url (or set AWS_ENDPOINT_URL) for R2.
                            Needs boto3.
  <directory>               a local directory, for a local backend or a test.

Before writing, a deploy shows what it would write and asks on the terminal.
--dry-run shows the same and stops; --yes deploys without asking, for CI that
has already reviewed a dry run. Without a terminal and without --yes, the deploy
is refused.

Examples:
  gh release download v1.1.0 --pattern snapshot.tar.gz

  # Preview deploying tnsw to the dev bucket.
  scripts/deploy_release.py snapshot.tar.gz tnsw --dest s3://tnsw-artifacts-dev/tnsw \\
      --endpoint-url https://<account>.r2.cloudflarestorage.com --dry-run

  # Deploy every bundle to a local directory without asking, reporting as JSON.
  for b in cda efc fcau npqs sltb tnsw; do
    scripts/deploy_release.py snapshot.tar.gz "$b" --dest "/tmp/artifacts/$b" --yes --json
  done

Exit status: 0 on a deploy, a dry run, or a deploy with nothing to write; 1 when
the release or the store is refused or the deploy isn't confirmed; 2 on bad
arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, TypeVar

HASH_CHARS = 16
DEFAULT_JOBS = 16
MANIFEST_NAME = re.compile(r"manifest_v([1-9][0-9]*)\.json")

T = TypeVar("T")
R = TypeVar("R")


def manifest_path(version: int) -> str:
    return f"manifest_v{version}.json"


def parallel(fn: Callable[[T], R], items: Iterable[T], jobs: int) -> list[R]:
    """fn over items, jobs at a time, results in the order of items.

    The first error is raised once the calls already running have finished; the
    calls that hadn't started are cancelled.
    """
    items = list(items)
    if jobs <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    pool = ThreadPoolExecutor(max_workers=min(jobs, len(items)))
    try:
        futures = [pool.submit(fn, item) for item in items]
        return [f.result() for f in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


class DeployError(Exception):
    """The release or the store is in a state the deploy refuses."""


class ConflictError(DeployError):
    """Another writer created the same object first."""


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
        # reader sees a partly written file under its final name. os.link fails if the
        # name exists, which keeps the write create-only.
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
        max_connections: int = DEFAULT_JOBS,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise DeployError("an s3:// destination needs boto3 (pip install boto3)") from None
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=s3_region(region, endpoint_url, boto3.Session().region_name),
                config=Config(
                    # R2 rejects the checksum headers newer boto3 adds by default.
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    # One connection per concurrent request; the client is shared by
                    # every thread (boto3 clients are thread-safe).
                    max_pool_connections=max(max_connections, 10),
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
                raise ConflictError(f"{key} already exists in {self.describe()}") from None
            raise

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"


def open_store(
    dest: str, endpoint_url: str | None, region: str | None, jobs: int = DEFAULT_JOBS
) -> Store:
    if dest.startswith("s3://"):
        bucket, _, prefix = dest[len("s3://") :].partition("/")
        if not bucket:
            raise DeployError(f"--dest {dest!r} names no bucket")
        return S3Store(bucket, prefix, endpoint_url=endpoint_url, region=region, max_connections=jobs)
    if "://" in dest:
        raise DeployError(f"--dest {dest!r}: only s3:// and local directories are supported")
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


@dataclass
class Release:
    """One bundle's whole history, as a release's snapshot holds it."""

    manifests: list[bytes]  # manifest_v1.json first, exactly as released
    files: dict[str, bytes]  # stored path -> content
    lists: list[set[str]]  # per version, the paths its manifest lists

    @property
    def version(self) -> int:
        return len(self.manifests)


def read_release(snapshot: Path, bundle: str) -> Release:
    """The bundle's history from snapshot.tar.gz, checked whole (rule A)."""
    prefix = f"{bundle}/"
    entries: dict[str, bytes] = {}
    try:
        with tarfile.open(snapshot, mode="r:gz") as tar:
            for m in tar.getmembers():
                if not m.name.startswith(prefix) or m.isdir():
                    continue
                path = m.name[len(prefix):]
                parts = path.split("/")
                if not m.isfile() or "" in parts or ".." in parts or "." in parts:
                    raise DeployError(f"{snapshot}: unexpected entry {m.name!r}")
                entries[path] = tar.extractfile(m).read()
    except FileNotFoundError:
        raise DeployError(f"{snapshot} does not exist") from None
    except (tarfile.TarError, OSError, EOFError) as err:
        raise DeployError(f"{snapshot} is not a readable archive: {err}") from None
    if not entries:
        raise DeployError(f"{snapshot} holds no bundle {bundle!r}")

    versions = sorted(int(m.group(1)) for p in entries if (m := MANIFEST_NAME.fullmatch(p)))
    if versions != list(range(1, len(versions) + 1)):
        raise DeployError(
            f"{snapshot}: {bundle}'s manifests are {[manifest_path(v) for v in versions]}, "
            "not manifest_v1.json up with no gap, so this isn't a release's whole history"
        )

    problems: list[str] = []
    manifests: list[bytes] = []
    lists: list[set[str]] = []
    for v in versions:
        raw = entries[manifest_path(v)]
        manifests.append(raw)
        listed: set[str] = set()
        try:
            manifest = json.loads(raw)
            if manifest.get("version") != v:
                raise ValueError(f"its version is {manifest.get('version')!r}")
            for row in manifest["artifacts"]:
                path = row["path"]
                listed.add(path)
                if path not in entries:
                    problems.append(f"{manifest_path(v)} lists {path}, which isn't in the snapshot")
                elif hashed_path(row["source"], entries[path]) != path:
                    problems.append(f"{manifest_path(v)} lists {path}, whose content doesn't match its name")
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            problems.append(f"{manifest_path(v)} is not a valid release manifest: {err!r}")
        lists.append(listed)
    every = set().union(*lists) if lists else set()
    stray = sorted(p for p in entries if p not in every and not MANIFEST_NAME.fullmatch(p))
    if stray:
        problems.append(f"files no manifest lists: {', '.join(stray)}")
    if problems:
        raise DeployError(f"{snapshot}: {bundle} is refused:\n  " + "\n  ".join(problems))

    files = {p: d for p, d in entries.items() if p in every}
    return Release(manifests=manifests, files=files, lists=lists)


# --- the deploy -----------------------------------------------------------------


def stored_manifests(store: Store, jobs: int = DEFAULT_JOBS) -> list[bytes]:
    """Every manifest in the store, oldest first, up to the first missing version.

    Manifests are read jobs versions at a time; versions after the first missing
    one are ignored, as the backend ignores them.
    """
    out: list[bytes] = []
    window = max(jobs, 1)
    while True:
        first = len(out) + 1
        versions = range(first, first + window)
        raws = parallel(store.get, [manifest_path(v) for v in versions], jobs)
        for raw in raws:
            if raw is None:
                return out
            out.append(raw)


@dataclass
class Plan:
    release: int  # the release's version of the bundle
    stored: int  # the store's version before the deploy
    writes: list[int] = field(default_factory=list)  # manifest versions to write, oldest first
    uploads: dict[str, bytes] = field(default_factory=dict)  # stored path -> content
    present: int = 0  # files the new manifests list that the store already holds


def plan_deploy(release: Release, store: Store, jobs: int = DEFAULT_JOBS) -> Plan:
    stored = stored_manifests(store, jobs)
    for v, raw in enumerate(stored[: release.version], start=1):
        if raw != release.manifests[v - 1]:
            raise DeployError(
                f"{store.describe()}/{manifest_path(v)} differs from this release's "
                f"{manifest_path(v)}, so the store was filled from somewhere else; nothing "
                "was written. Clear the store, or deploy to another prefix."
            )
    plan = Plan(release=release.version, stored=len(stored))
    plan.writes = list(range(len(stored) + 1, release.version + 1))
    if not plan.writes:
        return plan

    # Every file a stored manifest lists is in the store already (rule D), so only
    # the others are checked.
    have = set().union(*release.lists[: len(stored)]) if stored else set()
    need = sorted(set().union(*(release.lists[v - 1] for v in plan.writes)) - have)
    present = dict(zip(need, parallel(store.exists, need, jobs)))
    for path in need:
        if present[path]:
            plan.present += 1
        else:
            plan.uploads[path] = release.files[path]
    return plan


def apply(plan: Plan, release: Release, store: Store, jobs: int = DEFAULT_JOBS) -> None:
    def upload(item: tuple[str, bytes]) -> None:
        path, data = item
        try:
            store.put_new(path, data, content_type(path))
        except ConflictError:
            pass  # another deploy stored the same content under the same hash

    # Raises on the first failed upload, before any manifest is written, so nothing
    # of the deploy goes live.
    parallel(upload, sorted(plan.uploads.items()), jobs)
    for v in plan.writes:
        body = release.manifests[v - 1]
        try:
            store.put_new(manifest_path(v), body, "application/json")
        except ConflictError:
            if store.get(manifest_path(v)) != body:
                raise ConflictError(
                    f"{manifest_path(v)} was written by someone else while this deploy ran, "
                    "with other content; manifests before it are live, it and later ones aren't"
                ) from None


def describe(plan: Plan, bundle: str, store: Store, log) -> None:
    """Print what the deploy would write."""
    log(f"bundle     {bundle}")
    log(f"dest       {store.describe()}")
    log(f"release    v{plan.release}")
    log(f"store      " + (f"v{plan.stored}" if plan.stored else "empty"))
    if not plan.writes:
        log("writes     nothing: the store holds this release's every version")
        return
    log(f"writes     {', '.join(manifest_path(v) for v in plan.writes)}")
    log(f"files      {len(plan.uploads)} to upload, {plan.present} already stored")


def confirm(store: Store) -> bool:
    """Ask on the terminal whether to deploy. Without a terminal, refuse: pass --yes."""
    if not sys.stdin.isatty():
        raise DeployError("no terminal to confirm on; pass --yes, or --dry-run to preview")
    answer = input(f"Deploy to {store.describe()}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy one bundle of a GitHub release into an artifact store.",
        epilog="See the module docstring for the rules and examples.",
    )
    parser.add_argument("snapshot", help="the release's snapshot.tar.gz")
    parser.add_argument("bundle", help="the bundle to deploy, a top-level folder in the snapshot")
    parser.add_argument("--dest", required=True, help="s3://<bucket>[/<prefix>] or a local directory")
    parser.add_argument("--endpoint-url", help="S3 endpoint, e.g. Cloudflare R2's")
    parser.add_argument("--region", help="S3 region (default: the environment's, or \"auto\" with an endpoint)")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help=f"store requests at a time (default: {DEFAULT_JOBS})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would be written and stop")
    mode.add_argument("--yes", action="store_true", help="deploy without asking")
    parser.add_argument("--json", action="store_true", help="report as JSON on stdout; the plan goes to stderr")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    log = (lambda s: print(s, file=sys.stderr)) if args.json else print
    try:
        release = read_release(Path(args.snapshot), args.bundle)
        store = open_store(args.dest, args.endpoint_url, args.region, args.jobs)
        plan = plan_deploy(release, store, args.jobs)
        describe(plan, args.bundle, store, log)
        deployed = False
        if plan.writes and not args.dry_run:
            if not args.yes and not confirm(store):
                raise DeployError("not confirmed; nothing was written")
            apply(plan, release, store, args.jobs)
            deployed = True
            log(f"deployed {', '.join(manifest_path(v) for v in plan.writes)}")
    except DeployError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "bundle": args.bundle,
            "dest": store.describe(),
            "release": plan.release,
            "stored": plan.stored,
            "writes": [manifest_path(v) for v in plan.writes],
            "uploads": len(plan.uploads),
            "deployed": deployed,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
