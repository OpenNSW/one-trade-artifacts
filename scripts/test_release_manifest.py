"""Tests for release_manifest.py. Run with: python3 -m unittest scripts/test_release_manifest.py

The S3 tests need boto3 and moto, and are skipped without them.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_manifest as rm  # noqa: E402


class Bundle:
    """A bundle folder written into a temporary directory."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "bundle"
        self.dir.mkdir()
        self.rows: list[dict] = []

    def add(self, ident, kind, path, content, version=""):
        self.rows.append({"id": ident, "kind": kind, "version": version, "path": path})
        self.write(path, content)

    def write(self, path, content):
        p = self.dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
        self.save()

    def save(self):
        (self.dir / "manifest.json").write_text(json.dumps({"artifacts": self.rows}))


class LocalReleaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = Bundle(self.root)
        self.bundle.add("flow", "workflow", "flow/workflow.json", {"nodes": [1]})
        self.bundle.add("flow:render", "generic_template", "flow/render.json", {"title": "A"})
        self.bundle.add("cert", "html_template", "flow/cert.html", "<p>{{x}}</p>")
        self.dest = self.root / "store"

    def release(self, *extra):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rm.main(
                [str(self.bundle.dir), "--dest", str(self.dest), "--commit", "abc123", "--json", *[e for e in extra if e != "--ask"]]
                + ([] if "--ask" in extra else ["--yes"])
            )
        self.last_err = err.getvalue()
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    def manifest(self, version):
        return json.loads((self.dest / f"manifest_v{version}.json").read_text())

    def rows(self, version):
        return {r["id"]: r for r in self.manifest(version)["artifacts"]}

    def test_first_release_is_v1_with_hashed_files(self):
        code, summary = self.release()
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(summary["version"], 1)
        self.assertTrue(summary["published"])
        self.assertEqual(summary["uploaded"], 3)

        m = self.manifest(1)
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["commit"], "abc123")
        self.assertIn("released_at", m)
        render = self.rows(1)["flow:render"]
        self.assertRegex(render["path"], r"^flow/render\.[0-9a-f]{16}\.json$")
        self.assertEqual(render["source"], "flow/render.json")
        self.assertEqual(render["since"], 1)
        self.assertEqual(render["version"], "")
        self.assertEqual(
            json.loads((self.dest / render["path"]).read_text()), {"title": "A"}
        )
        self.assertRegex(self.rows(1)["cert"]["path"], r"^flow/cert\.[0-9a-f]{16}\.html$")

    def test_unchanged_bundle_publishes_nothing(self):
        self.release()
        code, summary = self.release()
        self.assertEqual(code, 0)
        self.assertFalse(summary["published"])
        self.assertEqual(summary["version"], 1)
        self.assertFalse((self.dest / "manifest_v2.json").exists())

    def test_changed_file_gets_a_new_name_and_since(self):
        self.release()
        v1 = self.rows(1)
        self.bundle.write("flow/render.json", {"title": "B"})

        code, summary = self.release()
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(summary["version"], 2)
        [c] = summary["changed"]
        self.assertEqual(c["source"], "flow/render.json")
        self.assertRegex(c["path"], r"^flow/render\.[0-9a-f]{16}\.json$")
        self.assertTrue(c["upload"])
        self.assertRegex(self.last_err, r"changed  flow/render\.json -> flow/render\.[0-9a-f]{16}\.json \(upload\)")
        self.assertEqual(summary["uploaded"], 1)

        v2 = self.rows(2)
        self.assertNotEqual(v2["flow:render"]["path"], v1["flow:render"]["path"])
        self.assertEqual(v2["flow:render"]["since"], 2)
        self.assertEqual(v2["flow"]["path"], v1["flow"]["path"])
        self.assertEqual(v2["flow"]["since"], 1)
        # v1 still resolves its own file.
        self.assertEqual(
            json.loads((self.dest / v1["flow:render"]["path"]).read_text()), {"title": "A"}
        )
        self.assertEqual(self.rows(1), v1)

    def test_revert_reuses_the_stored_file(self):
        self.release()
        v1_path = self.rows(1)["flow:render"]["path"]
        self.bundle.write("flow/render.json", {"title": "B"})
        self.release()
        self.bundle.write("flow/render.json", {"title": "A"})

        code, summary = self.release()
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(summary["version"], 3)
        self.assertEqual(summary["uploaded"], 0)
        self.assertEqual(summary["changed"], [{"source": "flow/render.json", "path": v1_path, "upload": False}])
        self.assertIn(f"changed  flow/render.json -> {v1_path} (already stored)", self.last_err)
        self.assertEqual(self.rows(3)["flow:render"]["path"], v1_path)
        self.assertEqual(self.rows(3)["flow:render"]["since"], 3)

    def test_removed_row_is_a_release(self):
        self.release()
        self.bundle.rows = [r for r in self.bundle.rows if r["id"] != "cert"]
        self.bundle.save()

        code, summary = self.release()
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(summary["version"], 2)
        self.assertEqual(summary["removed"], ["cert"])
        self.assertNotIn("cert", self.rows(2))

    def test_dry_run_writes_nothing(self):
        code, summary = self.release("--dry-run")
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(summary["version"], 1)
        self.assertFalse(summary["published"])
        self.assertFalse(self.dest.exists())

    def test_declined_release_writes_nothing(self):
        with mock.patch.object(rm, "confirm", return_value=False) as asked:
            code, summary = self.release("--ask")
        asked.assert_called_once()
        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertIn("not published", self.last_err)
        self.assertFalse(self.dest.exists())

    def test_confirmed_release_publishes(self):
        with mock.patch.object(rm, "confirm", return_value=True):
            code, summary = self.release("--ask")
        self.assertEqual(code, 0, self.last_err)
        self.assertTrue(summary["published"])
        self.assertIn("version    manifest_v1.json (first release)", self.last_err)

    def test_release_without_a_terminal_needs_yes(self):
        with mock.patch.object(rm.sys.stdin, "isatty", return_value=False):
            code, _ = self.release("--ask")
        self.assertEqual(code, 1)
        self.assertIn("pass --yes", self.last_err)
        self.assertFalse(self.dest.exists())

    def test_dry_run_never_asks(self):
        with mock.patch.object(rm, "confirm") as asked:
            code, _ = self.release("--ask", "--dry-run")
        asked.assert_not_called()
        self.assertEqual(code, 0, self.last_err)

    def test_static_data_version_cannot_change_content(self):
        self.bundle.add("countries", "static_data", "data/countries.json", ["LK"], version="1")
        self.release()
        self.bundle.write("data/countries.json", ["LK", "IN"])

        code, _ = self.release()
        self.assertEqual(code, 1)
        self.assertIn("countries (static_data) version 1", self.last_err)
        self.assertFalse((self.dest / "manifest_v2.json").exists())

    def test_static_data_new_version_is_allowed(self):
        self.bundle.add("countries", "static_data", "data/countries.json", ["LK"], version="1")
        self.release()
        self.bundle.add("countries", "static_data", "data/countries_v2.json", ["LK", "IN"], version="2")

        code, summary = self.release()
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual([c["source"] for c in summary["changed"]], ["data/countries_v2.json"])

    def test_missing_file_and_duplicate_row_are_refused(self):
        self.bundle.rows.append({"id": "gone", "kind": "workflow", "version": "", "path": "nope.json"})
        self.bundle.rows.append(dict(self.bundle.rows[0]))
        self.bundle.save()

        code, _ = self.release()
        self.assertEqual(code, 1)
        self.assertIn("nope.json does not exist", self.last_err)
        self.assertIn("is also artifacts[0]", self.last_err)
        self.assertFalse(self.dest.exists())

    def test_path_outside_the_bundle_is_refused(self):
        (self.root / "secret.json").write_text("{}")
        self.bundle.rows.append({"id": "x", "kind": "workflow", "version": "", "path": "../secret.json"})
        self.bundle.save()

        code, _ = self.release()
        self.assertEqual(code, 1)
        self.assertIn("leaves the bundle", self.last_err)

    def test_invalid_json_is_refused(self):
        self.bundle.write("flow/render.json", "{not json")
        code, _ = self.release()
        self.assertEqual(code, 1)
        self.assertIn("flow/render.json is not valid JSON", self.last_err)

    def test_manifest_published_meanwhile_is_a_conflict(self):
        store = rm.LocalStore(str(self.dest))
        plan = rm.plan_release(self.bundle.dir, store, "abc", "now")
        store.put_new("manifest_v1.json", b"{}", "application/json")
        with self.assertRaises(rm.ConflictError):
            rm.apply(plan, store)

    def test_versions_stop_at_the_first_gap(self):
        self.release()
        (self.dest / "manifest_v3.json").write_text("{}")
        self.bundle.write("flow/render.json", {"title": "B"})
        code, summary = self.release()
        self.assertEqual(code, 0, self.last_err)
        self.assertEqual(summary["version"], 2)

    def test_hashed_path(self):
        self.assertRegex(rm.hashed_path("a/b/render.json", b"x"), r"^a/b/render\.[0-9a-f]{16}\.json$")
        self.assertRegex(rm.hashed_path("x.schema.json", b"x"), r"^x\.schema\.[0-9a-f]{16}\.json$")
        self.assertRegex(rm.hashed_path("README", b"x"), r"^README\.[0-9a-f]{16}$")


try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None


@unittest.skipIf(mock_aws is None, "needs boto3 and moto")
class S3ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name="us-east-1")
        self.client.create_bucket(Bucket="artifacts")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bundle = Bundle(Path(tmp.name))
        self.bundle.add("flow", "workflow", "flow/workflow.json", {"nodes": [1]})
        self.store = rm.S3Store("artifacts", "tnsw/", client=self.client)

    def keys(self):
        out = self.client.list_objects_v2(Bucket="artifacts")
        return sorted(o["Key"] for o in out.get("Contents", []))

    def test_release_writes_under_the_prefix(self):
        plan = rm.plan_release(self.bundle.dir, self.store, "abc", "now")
        rm.apply(plan, self.store)
        keys = self.keys()
        self.assertIn("tnsw/manifest_v1.json", keys)
        self.assertTrue(any(k.startswith("tnsw/flow/workflow.") for k in keys))
        head = self.client.head_object(Bucket="artifacts", Key="tnsw/manifest_v1.json")
        self.assertEqual(head["ContentType"], "application/json")

        self.bundle.write("flow/workflow.json", {"nodes": [2]})
        plan = rm.plan_release(self.bundle.dir, self.store, "def", "later")
        self.assertEqual(plan.version, 2)
        rm.apply(plan, self.store)
        self.assertIn("tnsw/manifest_v2.json", self.keys())

    def test_put_new_refuses_an_existing_key(self):
        self.store.put_new("manifest_v1.json", b"{}", "application/json")
        with self.assertRaises(rm.ConflictError):
            self.store.put_new("manifest_v1.json", b"{}", "application/json")


if __name__ == "__main__":
    unittest.main()
