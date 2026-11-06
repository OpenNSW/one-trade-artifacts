"""Tests for deploy_release.py. Run with: python3 -m unittest discover -s scripts -p 'test_*.py'"""

import contextlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_release as br  # noqa: E402
import deploy_release as dr  # noqa: E402


class Releases:
    """A repository with one bundle, released with build_release.py one version at a time."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundle = root / "repo" / "alpha"
        self.bundle.mkdir(parents=True)
        self.rows: list[dict] = []
        self.count = 0
        self.previous: Path | None = None

    def add(self, ident, path, content):
        self.rows.append({"id": ident, "kind": "workflow", "version": "", "path": path})
        self.write(path, content)

    def write(self, path, content):
        p = self.bundle / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(content))
        (self.bundle / "manifest.json").write_text(json.dumps({"artifacts": self.rows}))

    def release(self) -> Path:
        """Build the next release; returns its snapshot.tar.gz."""
        self.count += 1
        out = self.root / f"release{self.count}"
        argv = ["--repo", str(self.root / "repo"), "--out", str(out), "--commit", f"c{self.count}",
                "--released-at", "2026-11-06T00:00:00Z", "--bundles", "alpha"]
        if self.previous is not None:
            argv += ["--baseline", str(self.previous)]
        with contextlib.redirect_stdout(io.StringIO()):
            assert br.main(argv) == 0
        self.previous = out
        return out / "snapshot.tar.gz"


def rewrite(snapshot: Path, change) -> Path:
    """A copy of snapshot with change(entries) applied."""
    with tarfile.open(snapshot) as tar:
        entries = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    change(entries)
    out = snapshot.with_name("changed.tar.gz")
    out.write_bytes(br.archive_bytes(entries, 0))
    return out


class DeployTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.r = Releases(self.root)
        self.r.add("flow", "flow/workflow.json", {"nodes": [1]})
        self.r.add("render", "flow/render.json", {"title": "A"})
        self.dest = self.root / "store"

    def deploy(self, snapshot, *extra, bundle="alpha"):
        """Run a deploy; returns (exit code, JSON report or None)."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = dr.main([str(snapshot), bundle, "--dest", str(self.dest), "--json", *extra])
        self.stderr = stderr.getvalue()
        return code, json.loads(stdout.getvalue()) if code == 0 else None

    def stored(self):
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*") if p.is_file())

    def released(self, snapshot):
        """The bundle's entries in snapshot, as the store should hold them."""
        with tarfile.open(snapshot) as tar:
            return {m.name[len("alpha/"):]: tar.extractfile(m).read()
                    for m in tar.getmembers() if m.name.startswith("alpha/")}

    def assertStoreHolds(self, snapshot):
        expected = self.released(snapshot)
        self.assertEqual(self.stored(), sorted(expected))
        for path, data in expected.items():
            self.assertEqual((self.dest / path).read_bytes(), data, path)

    # --- deploys ---------------------------------------------------------------------

    def test_first_deploy_copies_the_release(self):
        snap = self.r.release()
        code, report = self.deploy(snap, "--yes")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((report["release"], report["stored"], report["writes"]), (1, 0, ["manifest_v1.json"]))
        self.assertEqual((report["uploads"], report["deployed"]), (2, True))
        self.assertStoreHolds(snap)

    def test_later_release_adds_only_what_is_new(self):
        self.deploy(self.r.release(), "--yes")
        self.r.write("flow/render.json", {"title": "B"})
        self.r.release()
        self.r.write("flow/render.json", {"title": "C"})
        snap = self.r.release()

        code, report = self.deploy(snap, "--yes")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((report["stored"], report["writes"]), (1, ["manifest_v2.json", "manifest_v3.json"]))
        self.assertEqual(report["uploads"], 2)  # the two new renders; the workflow is shared
        self.assertStoreHolds(snap)

    def test_deploying_again_writes_nothing(self):
        snap = self.r.release()
        self.deploy(snap, "--yes")
        code, report = self.deploy(snap, "--yes")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((report["writes"], report["deployed"]), ([], False))

    def test_older_release_than_the_store_writes_nothing(self):
        old = self.r.release()
        self.r.write("flow/render.json", {"title": "B"})
        self.deploy(self.r.release(), "--yes")
        code, report = self.deploy(old, "--yes")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((report["release"], report["stored"], report["writes"]), (1, 2, []))

    def test_dry_run_writes_nothing(self):
        code, report = self.deploy(self.r.release(), "--dry-run")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((report["writes"], report["deployed"]), (["manifest_v1.json"], False))
        self.assertFalse(self.dest.exists())

    def test_without_a_terminal_needs_yes(self):
        code, _ = self.deploy(self.r.release())
        self.assertEqual(code, 1)
        self.assertIn("pass --yes", self.stderr)
        self.assertFalse(self.dest.exists())

    def test_manifests_are_written_after_the_files(self):
        snap = self.r.release()
        order = []

        class Recording(dr.LocalStore):
            def put_new(self, key, data, content_type):
                order.append(key)
                super().put_new(key, data, content_type)

        release = dr.read_release(snap, "alpha")
        store = Recording(str(self.dest))
        dr.apply(dr.plan_deploy(release, store), release, store)
        self.assertEqual(order[-1], "manifest_v1.json")

    def test_failed_upload_writes_no_manifest(self):
        snap = self.r.release()

        class Failing(dr.LocalStore):
            def put_new(self, key, data, content_type):
                if key.startswith("flow/render."):
                    raise OSError("upload failed")
                super().put_new(key, data, content_type)

        release = dr.read_release(snap, "alpha")
        store = Failing(str(self.dest))
        with self.assertRaises(OSError):
            dr.apply(dr.plan_deploy(release, store, jobs=1), release, store, jobs=1)
        self.assertNotIn("manifest_v1.json", self.stored())

    # --- refusals -------------------------------------------------------------------

    def test_store_filled_from_elsewhere_is_refused(self):
        self.dest.mkdir()
        (self.dest / "manifest_v1.json").write_text('{"version": 1, "artifacts": []}')
        code, _ = self.deploy(self.r.release(), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("manifest_v1.json differs from this release's manifest_v1.json", self.stderr)
        self.assertEqual(self.stored(), ["manifest_v1.json"])

    def test_snapshot_with_a_gap_is_refused(self):
        self.r.release()
        self.r.write("flow/render.json", {"title": "B"})
        snap = rewrite(self.r.release(), lambda e: e.pop("alpha/manifest_v1.json"))
        code, _ = self.deploy(snap, "--yes")
        self.assertEqual(code, 1)
        self.assertIn("not manifest_v1.json up with no gap", self.stderr)
        self.assertFalse(self.dest.exists())

    def test_snapshot_with_a_changed_file_is_refused(self):
        snap = self.r.release()
        render = next(k for k in self.released(snap) if k.startswith("flow/render."))
        code, _ = self.deploy(rewrite(snap, lambda e: e.update({f"alpha/{render}": b"{}"})), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("whose content doesn't match its name", self.stderr)

    def test_snapshot_with_an_unlisted_file_is_refused(self):
        snap = rewrite(self.r.release(), lambda e: e.update({"alpha/extra.json": b"{}"}))
        code, _ = self.deploy(snap, "--yes")
        self.assertEqual(code, 1)
        self.assertIn("files no manifest lists: extra.json", self.stderr)

    def test_snapshot_with_an_escaping_entry_is_refused(self):
        snap = rewrite(self.r.release(), lambda e: e.update({"alpha/../evil.json": b"{}"}))
        code, _ = self.deploy(snap, "--yes")
        self.assertEqual(code, 1)
        self.assertIn("unexpected entry 'alpha/../evil.json'", self.stderr)

    def test_missing_bundle_is_refused(self):
        code, _ = self.deploy(self.r.release(), "--yes", bundle="beta")
        self.assertEqual(code, 1)
        self.assertIn("holds no bundle 'beta'", self.stderr)

    def test_s3_region(self):
        self.assertEqual(dr.s3_region("eu-west-1", "https://r2", None), "eu-west-1")
        self.assertEqual(dr.s3_region(None, "https://r2", "us-east-1"), "us-east-1")
        self.assertEqual(dr.s3_region(None, "https://r2", None), "auto")


try:
    import boto3
    from moto import mock_aws
except ImportError:
    mock_aws = None


@unittest.skipIf(mock_aws is None, "needs boto3 and moto")
class S3DeployTest(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name="us-east-1")
        self.client.create_bucket(Bucket="artifacts")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.r = Releases(Path(tmp.name))
        self.r.add("flow", "flow/workflow.json", {"nodes": [1]})
        self.store = dr.S3Store("artifacts", "tnsw/", client=self.client)

    def keys(self):
        out = self.client.list_objects_v2(Bucket="artifacts")
        return sorted(o["Key"] for o in out.get("Contents", []))

    def test_deploy_writes_under_the_prefix(self):
        release = dr.read_release(self.r.release(), "alpha")
        dr.apply(dr.plan_deploy(release, self.store), release, self.store)
        keys = self.keys()
        self.assertIn("tnsw/manifest_v1.json", keys)
        self.assertTrue(any(k.startswith("tnsw/flow/workflow.") for k in keys))
        head = self.client.head_object(Bucket="artifacts", Key="tnsw/manifest_v1.json")
        self.assertEqual(head["ContentType"], "application/json")

    def test_put_new_refuses_an_existing_key(self):
        self.store.put_new("manifest_v1.json", b"{}", "application/json")
        with self.assertRaises(dr.ConflictError):
            self.store.put_new("manifest_v1.json", b"{}", "application/json")


if __name__ == "__main__":
    unittest.main()
