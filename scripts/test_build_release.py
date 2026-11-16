"""Tests for build_release.py. Run with: python3 -m unittest discover -s scripts -p 'test_*.py'"""

import contextlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_release as br  # noqa: E402


class Bundle:
    """A bundle folder written into a repository folder."""

    def __init__(self, repo: Path, name: str) -> None:
        self.name = name
        self.dir = repo / name
        self.dir.mkdir(parents=True)
        self.rows: list[dict] = []
        self.save()

    def add(self, ident, kind, path, content, version=""):
        self.rows.append({"id": ident, "kind": kind, "version": version, "path": path})
        self.write(path, content)

    def remove(self, ident, version=""):
        self.rows = [r for r in self.rows if (r["id"], r["version"]) != (ident, version)]
        self.save()

    def write(self, path, content):
        p = self.dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
        self.save()

    def save(self):
        (self.dir / "manifest.json").write_text(json.dumps({"artifacts": self.rows}))


class BuildTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.a = Bundle(self.repo, "alpha")
        self.a.add("flow", "workflow", "flow/workflow.json", {"nodes": [1]})
        self.a.add("flow:render", "generic_template", "flow/render.json", {"title": "A"})
        self.a.add("cert", "html_template", "flow/cert.gohtml", "<p>{{.x}}</p>")
        self.b = Bundle(self.repo, "beta")
        self.b.add("task", "workflow", "task/workflow.json", {"nodes": ["b"]})
        self.builds = 0
        self.baseline = None  # the previous build's output folder

    def build(self, *extra, baseline="previous"):
        """Run a build against the previous one's output; returns (exit code, summary)."""
        self.builds += 1
        out = self.root / f"out{self.builds}"
        base = self.baseline if baseline == "previous" else baseline
        argv = ["--repo", str(self.repo), "--out", str(out), "--commit", f"c{self.builds}",
                "--released-at", "2026-11-04T00:00:00Z", "--bundles", "alpha,beta", *extra]
        if base is not None:
            argv += ["--baseline", str(base)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = br.main(argv)
        self.out, self.stdout, self.stderr = out, stdout.getvalue(), stderr.getvalue()
        if code != 0:
            return code, None
        self.baseline = out
        return code, json.loads((out / "release.json").read_text())

    def archive(self, name):
        with tarfile.open(self.out / name if name.endswith(".tar.gz") else self.out / f"{name}.tar.gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}

    def rows(self, name, summary):
        return {r["id"]: r for r in summary["bundles"][name]["manifest"]["artifacts"]}

    def assertRefused(self, code, *messages):
        self.assertEqual(code, 1, self.stdout)
        for m in messages:
            self.assertIn(m, self.stderr)
        self.assertFalse(self.out.exists(), "a refused build wrote output")

    # --- releases --------------------------------------------------------------

    def test_first_release_is_v1_with_hashed_files(self):
        code, s = self.build(baseline=None)
        self.assertEqual(code, 0, self.stderr)
        self.assertTrue(s["changed"])
        self.assertEqual(s["bundles"]["alpha"]["version"], 1)
        self.assertEqual(s["bundles"]["alpha"]["previous"], 0)
        self.assertEqual(s["bundles"]["beta"]["archive"], "beta.tar.gz")

        files = self.archive("alpha")
        m = json.loads(files["manifest_v1.json"])
        self.assertEqual((m["version"], m["commit"], m["released_at"]), (1, "c1", "2026-11-04T00:00:00Z"))
        render = {r["id"]: r for r in m["artifacts"]}["flow:render"]
        self.assertRegex(render["path"], r"^flow/render\.[0-9a-f]{16}\.json$")
        self.assertEqual((render["source"], render["since"]), ("flow/render.json", 1))
        self.assertEqual(json.loads(files[render["path"]]), {"title": "A"})
        self.assertEqual(sorted(files), sorted([r["path"] for r in m["artifacts"]] + ["manifest_v1.json"]))
        self.assertIn("### alpha — v1 (first release)", (self.out / "release-notes.md").read_text())

    def test_rebuild_against_its_own_output_changes_nothing(self):
        self.build(baseline=None)
        code, s = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertFalse(s["changed"])
        self.assertEqual(s["bundles"]["alpha"]["version"], 1)
        self.assertEqual(s["bundles"]["alpha"]["manifest"]["commit"], "c1")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["release-notes.md", "release.json"])
        self.assertIn("nothing changed", self.stdout)
        # ... and again, against a summary that recorded no change.
        code, s = self.build()
        self.assertFalse(s["changed"])

    def test_changed_file_releases_only_its_bundle(self):
        _, s1 = self.build(baseline=None)
        self.a.write("flow/render.json", {"title": "B"})

        code, s2 = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((s2["bundles"]["alpha"]["version"], s2["bundles"]["alpha"]["previous"]), (2, 1))
        self.assertFalse(s2["bundles"]["beta"]["released"])
        self.assertIsNone(s2["bundles"]["beta"]["archive"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["alpha.tar.gz", "release-notes.md", "release.json", "snapshot.tar.gz"])
        [c] = s2["bundles"]["alpha"]["changed"]
        self.assertEqual(c["source"], "flow/render.json")

        v1, v2 = self.rows("alpha", s1), self.rows("alpha", s2)
        self.assertNotEqual(v2["flow:render"]["path"], v1["flow:render"]["path"])
        self.assertEqual(v2["flow:render"]["since"], 2)
        self.assertEqual((v2["flow"]["path"], v2["flow"]["since"]), (v1["flow"]["path"], 1))
        self.assertIn("manifest_v2.json", self.archive("alpha"))
        notes = (self.out / "release-notes.md").read_text()
        self.assertIn("### alpha — v2 (was v1)", notes)
        self.assertIn("- `flow:render` (generic_template) — `flow/render.json`", notes)
        self.assertIn("### beta — unchanged (v1)", notes)

    def test_snapshot_holds_every_bundle_at_its_current_version(self):
        _, s1 = self.build(baseline=None)
        self.a.write("flow/render.json", {"title": "B"})
        code, s2 = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(s2["snapshot"], "snapshot.tar.gz")

        snap = self.archive("snapshot.tar.gz")
        self.assertEqual(snap["alpha/manifest_v2.json"], self.archive("alpha")["manifest_v2.json"])
        # beta didn't change: it's there at v1, with the files v1 listed.
        beta = json.loads(snap["beta/manifest_v1.json"])
        self.assertEqual(beta, s1["bundles"]["beta"]["manifest"])
        self.assertEqual(json.loads(snap[f"beta/{beta['artifacts'][0]['path']}"]), {"nodes": ["b"]})

    def test_snapshot_holds_the_whole_history(self):
        _, s1 = self.build(baseline=None)
        self.a.write("flow/render.json", {"title": "B"})
        _, s2 = self.build()
        self.b.write("task/workflow.json", {"nodes": ["c"]})
        code, s3 = self.build()
        self.assertEqual(code, 0, self.stderr)

        snap = self.archive("snapshot.tar.gz")
        manifests = {
            "alpha/manifest_v1.json": s1["bundles"]["alpha"]["manifest"],
            "alpha/manifest_v2.json": s2["bundles"]["alpha"]["manifest"],
            "beta/manifest_v1.json": s1["bundles"]["beta"]["manifest"],
            "beta/manifest_v2.json": s3["bundles"]["beta"]["manifest"],
        }
        for path, manifest in manifests.items():
            self.assertEqual(json.loads(snap[path]), manifest, path)
        # Exactly every manifest ever released and every file any of them lists.
        expected = set(manifests) | {
            f"{path.split('/')[0]}/{r['path']}" for path, m in manifests.items() for r in m["artifacts"]
        }
        self.assertEqual(set(snap), expected)
        # alpha v1's render, replaced in v2, is still there for v1.
        old = self.rows("alpha", s1)["flow:render"]["path"]
        self.assertEqual(json.loads(snap[f"alpha/{old}"]), {"title": "A"})
        # The per-bundle archive holds that bundle's whole history: its slice of the snapshot.
        beta = self.archive("beta")
        self.assertEqual(sorted(k for k in beta if k.startswith("manifest_")), ["manifest_v1.json", "manifest_v2.json"])
        self.assertEqual(beta, {p[len("beta/"):]: d for p, d in snap.items() if p.startswith("beta/")})
        self.assertNotIn("alpha.tar.gz", [p.name for p in self.out.iterdir()])  # alpha didn't change

    def test_baseline_without_a_snapshot_is_refused_when_releasing(self):
        self.build(baseline=None)
        (self.baseline / "snapshot.tar.gz").unlink()
        code, s = self.build()  # nothing changed: the snapshot isn't needed
        self.assertEqual(code, 0, self.stderr)
        self.assertFalse(s["changed"])
        self.a.write("flow/render.json", {"title": "B"})
        code, _ = self.build()
        self.assertRefused(code, "snapshot.tar.gz does not exist")

    def rewrite_baseline_snapshot(self, change):
        """Rewrite the baseline's snapshot.tar.gz through change(entries)."""
        path = self.baseline / "snapshot.tar.gz"
        with tarfile.open(path) as tar:
            entries = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
        change(entries)
        path.write_bytes(br.archive_bytes(entries, 0))

    def test_baseline_snapshot_missing_an_earlier_manifest_is_refused(self):
        self.build(baseline=None)
        self.a.write("flow/render.json", {"title": "B"})
        self.build()
        self.rewrite_baseline_snapshot(lambda e: e.pop("alpha/manifest_v1.json"))
        self.b.write("task/workflow.json", {"nodes": ["c"]})
        code, _ = self.build()
        self.assertRefused(code, "alpha/manifest_v1.json is missing")

    def test_baseline_snapshot_with_a_changed_file_is_refused(self):
        _, s1 = self.build(baseline=None)
        render = "alpha/" + self.rows("alpha", s1)["flow:render"]["path"]
        self.rewrite_baseline_snapshot(lambda e: e.update({render: b'{"title": "X"}'}))
        self.b.write("task/workflow.json", {"nodes": ["c"]})
        code, _ = self.build()
        self.assertRefused(code, "whose content doesn't match its name")

    def test_snapshot_never_replaces_a_file(self):
        self.build(baseline=None)
        self.rewrite_baseline_snapshot(lambda e: e.update({"alpha/manifest_v2.json": b"{}"}))
        self.a.write("flow/render.json", {"title": "B"})
        code, _ = self.build()
        self.assertRefused(code, "already holds other content at:\n  alpha/manifest_v2.json")

    def test_baseline_snapshot_with_an_unexpected_entry_is_refused(self):
        self.build(baseline=None)
        self.rewrite_baseline_snapshot(lambda e: e.update({"../evil.json": b"{}"}))
        self.b.write("task/workflow.json", {"nodes": ["c"]})
        code, _ = self.build()
        self.assertRefused(code, "unexpected entry '../evil.json'")

    def test_skipped_bundle_is_still_known_to_the_next_build(self):
        self.build(baseline=None)
        self.a.write("flow/render.json", {"title": "B"})
        self.build()  # beta skipped
        self.b.write("task/workflow.json", {"nodes": ["c"]})
        code, s = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(s["bundles"]["beta"]["version"], 2)
        self.assertEqual(s["bundles"]["alpha"]["version"], 2)
        self.assertFalse(s["bundles"]["alpha"]["released"])

    def test_revert_to_earlier_content_reuses_its_name(self):
        _, s1 = self.build(baseline=None)
        self.a.write("flow/render.json", {"title": "B"})
        self.build()
        self.a.write("flow/render.json", {"title": "A"})

        code, s3 = self.build()
        self.assertEqual(code, 0, self.stderr)
        render = self.rows("alpha", s3)["flow:render"]
        self.assertEqual(s3["bundles"]["alpha"]["version"], 3)
        self.assertEqual(render["path"], self.rows("alpha", s1)["flow:render"]["path"])
        self.assertEqual(render["since"], 3)

    def test_removed_row_is_a_release(self):
        self.build(baseline=None)
        self.a.remove("cert")

        code, s = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(s["bundles"]["alpha"]["version"], 2)
        self.assertEqual(s["bundles"]["alpha"]["removed"], [{"id": "cert", "kind": "html_template", "version": ""}])
        self.assertEqual(s["bundles"]["alpha"]["changed"], [])
        self.assertNotIn("cert", self.rows("alpha", s))
        self.assertIn("Removed (1):\n\n- `cert` (html_template)", (self.out / "release-notes.md").read_text())

    def test_new_bundle_in_a_later_release_starts_at_v1(self):
        self.build(baseline=None)
        Bundle(self.repo, "gamma").add("x", "workflow", "x.json", {})
        code, s = self.build("--bundles", "alpha,beta,gamma")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(s["bundles"]["gamma"]["version"], 1)
        self.assertFalse(s["bundles"]["alpha"]["released"])

    def test_archive_is_the_same_for_the_same_input(self):
        self.build("--commit", "same", baseline=None)
        first = (self.out / "alpha.tar.gz").read_bytes()
        self.build("--commit", "same", baseline=None)
        self.assertEqual((self.out / "alpha.tar.gz").read_bytes(), first)

    # --- versioned (static data) rows ----------------------------------------------

    def test_static_data_version_cannot_change_content(self):
        self.a.add("countries", "static_data", "data/countries.json", ["LK"], version="1")
        self.build(baseline=None)
        self.a.write("data/countries.json", ["LK", "IN"])

        code, _ = self.build()
        self.assertRefused(code, "alpha: countries (static_data, version 1)", "manifest_v1.json")

    def test_static_data_moved_with_the_same_content_is_allowed(self):
        self.a.add("countries", "static_data", "data/countries.json", ["LK"], version="1")
        self.build(baseline=None)
        self.a.remove("countries", "1")
        self.a.add("countries", "static_data", "static/countries.json", ["LK"], version="1")

        code, s = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual([c["source"] for c in s["bundles"]["alpha"]["changed"]], ["static/countries.json"])

    def test_static_data_new_version_is_allowed(self):
        self.a.add("countries", "static_data", "data/countries.json", ["LK"], version="1")
        self.build(baseline=None)
        self.a.add("countries", "static_data", "data/countries_2.json", ["LK", "IN"], version="2")

        code, s = self.build()
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual([c["source"] for c in s["bundles"]["alpha"]["changed"]], ["data/countries_2.json"])
        self.assertEqual([(v["version"], v["since"]) for v in s["bundles"]["alpha"]["versioned"]], [("1", 1), ("2", 2)])

    def test_static_data_removed_then_changed_is_refused(self):
        self.a.add("countries", "static_data", "data/countries.json", ["LK"], version="1")
        self.build(baseline=None)
        self.a.remove("countries", "1")
        self.build()
        self.a.add("countries", "static_data", "data/countries.json", ["IN"], version="1")

        code, _ = self.build()
        self.assertRefused(code, "countries (static_data, version 1)")

    # --- refusals --------------------------------------------------------------------

    def test_every_problem_is_listed(self):
        self.a.rows.append({"id": "gone", "kind": "workflow", "version": "", "path": "nope.json"})
        self.a.rows.append(dict(self.a.rows[0]))
        self.a.save()
        (self.root / "secret.json").write_text("{}")
        self.b.rows.append({"id": "x", "kind": "workflow", "version": "", "path": "../../secret.json"})
        self.b.write("task/workflow.json", "{not json")

        code, _ = self.build(baseline=None)
        self.assertRefused(
            code,
            "alpha/manifest.json artifacts[3]: nope.json does not exist",
            "alpha/manifest.json artifacts[4]: flow (workflow) is also artifacts[0]",
            "beta/manifest.json artifacts[0]: task/workflow.json is not valid JSON",
            "beta/manifest.json artifacts[1]: path '../../secret.json' leaves the bundle",
        )

    def test_duplicate_versioned_row_is_refused(self):
        self.a.add("countries", "static_data", "data/a.json", ["LK"], version="1")
        self.a.add("countries", "static_data", "data/b.json", ["IN"], version="1")
        code, _ = self.build(baseline=None)
        self.assertRefused(code, "countries (static_data, version 1) is also artifacts[3]")

    def test_symlink_out_of_the_bundle_is_refused(self):
        (self.root / "secret.json").write_text("{}")
        (self.a.dir / "link.json").symlink_to(self.root / "secret.json")
        self.a.rows.append({"id": "x", "kind": "workflow", "version": "", "path": "link.json"})
        self.a.save()
        code, _ = self.build(baseline=None)
        self.assertRefused(code, "leaves the bundle")

    def test_missing_bundle_is_refused(self):
        code, _ = self.build("--bundles", "alpha,beta,gamma", baseline=None)
        self.assertRefused(code, "gamma/manifest.json does not exist")

    def test_baseline_without_a_summary_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        code, _ = self.build(baseline=empty)
        self.assertRefused(code, "release.json does not exist")

    def test_baseline_bundle_left_out_is_refused(self):
        self.build(baseline=None)
        code, _ = self.build("--bundles", "alpha")
        self.assertRefused(code, "the baseline has bundles this build doesn't: beta")

    def test_output_folder_must_be_empty(self):
        self.root.joinpath("out1").mkdir()
        self.root.joinpath("out1", "stale").write_text("")
        code, _ = self.build(baseline=None)
        self.assertEqual(code, 1)
        self.assertIn("is not an empty folder", self.stderr)

    # --- the nsw-srilanka release it works with -------------------------------

    def test_nsw_srilanka_release_opens_the_notes_and_is_recorded(self):
        code, s = self.build("--nsw-srilanka", "v1.1.0", baseline=None)
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(s["nsw_srilanka"], "v1.1.0")
        self.assertTrue((self.out / "release-notes.md").read_text().startswith(
            "Works with nsw-srilanka [v1.1.0](https://github.com/OpenNSW/nsw-srilanka/releases/tag/v1.1.0)"
            " and later (image tag `1.1.0` or newer).\n\n## Bundles\n"
        ))

    def test_nsw_srilanka_pre_release_is_accepted(self):
        code, s = self.build("--nsw-srilanka", "v1.2.0-rc.1", baseline=None)
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(s["nsw_srilanka"], "v1.2.0-rc.1")
        self.assertIn("(image tag `1.2.0-rc.1` or newer)", (self.out / "release-notes.md").read_text())

    def test_without_nsw_srilanka_the_notes_start_at_the_bundles(self):
        code, s = self.build(baseline=None)
        self.assertEqual(code, 0, self.stderr)
        self.assertIsNone(s["nsw_srilanka"])
        self.assertTrue((self.out / "release-notes.md").read_text().startswith("## Bundles\n"))

    def test_nsw_srilanka_that_is_not_a_release_tag_is_refused(self):
        for value in ("1.1.0", "v1.1", "latest", "v1.1.0 ", ""):
            with self.subTest(value=value):
                code, _ = self.build("--nsw-srilanka", value, baseline=None)
                self.assertRefused(code, "is not an nsw-srilanka release tag like v1.1.0")

    def test_baseline_without_nsw_srilanka_is_read(self):
        # Releases published before release.json recorded it.
        self.build(baseline=None)
        summary = json.loads((self.baseline / "release.json").read_text())
        del summary["nsw_srilanka"]
        (self.baseline / "release.json").write_text(json.dumps(summary))
        self.a.write("flow/render.json", {"title": "B"})
        code, s = self.build("--nsw-srilanka", "v1.1.0")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual((s["bundles"]["alpha"]["version"], s["nsw_srilanka"]), (2, "v1.1.0"))

    def test_hashed_path(self):
        self.assertRegex(br.hashed_path("a/b/render.json", b"x"), r"^a/b/render\.[0-9a-f]{16}\.json$")
        self.assertRegex(br.hashed_path("x.schema.json", b"x"), r"^x\.schema\.[0-9a-f]{16}\.json$")
        self.assertRegex(br.hashed_path("README", b"x"), r"^README\.[0-9a-f]{16}$")


class GitTest(unittest.TestCase):
    """The recorded commit is the repository's HEAD, and a dirty bundle is refused."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.bundle = Bundle(self.repo, "alpha")
        self.bundle.add("flow", "workflow", "flow/workflow.json", {"nodes": [1]})
        Bundle(self.repo, "beta")
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                                   capture_output=True, text=True).stdout.strip()

    def build(self, out, *extra):
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = br.main(["--repo", str(self.repo), "--out", str(self.root / out),
                            "--bundles", "alpha,beta", *extra])
        return code, stderr.getvalue()

    def test_clean_checkout_records_head(self):
        code, err = self.build("out")
        self.assertEqual(code, 0, err)
        s = json.loads((self.root / "out" / "release.json").read_text())
        self.assertEqual(s["commit"], self.head)
        self.assertEqual(s["bundles"]["alpha"]["manifest"]["commit"], self.head)

    def test_uncommitted_changes_are_refused(self):
        self.bundle.write("flow/workflow.json", {"nodes": [2]})
        code, err = self.build("out")
        self.assertEqual(code, 1)
        self.assertIn("alpha has uncommitted changes", err)
        self.assertNotIn("beta has", err)
        self.assertFalse((self.root / "out").exists())

        code, err = self.build("out2", "--allow-dirty")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
