import os
import pathlib, shutil, subprocess, sys, tempfile, unittest
from unittest.mock import patch
SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path: sys.path.insert(0, str(SOURCE_ROOT))
from repoready.scanner import scan_project
from repoready.checks import Status, run_checks

class ChecksTests(unittest.TestCase):
    def _results(self, root):
        return {result.name: result for result in run_checks(scan_project(root))}

    def test_all_eight_and_good_project(self):
        with tempfile.TemporaryDirectory() as d:
            r=pathlib.Path(d); (r/'.git').mkdir(); (r/'uv.lock').write_text('x'); (r/'.python-version').write_text('3.12.7')
            (r/'Dockerfile').write_text('FROM python@sha256:'+'a'*64+'\nCMD ["x"]\n')
            (r/'tests').mkdir(); (r/'tests/test_x.py').write_text('')
            (r/'README.md').write_text('Setup install. Run usage. Test with python -m unittest.')
            (r/'.env.example').write_text('TOKEN=') ; (r/'app.py').write_text('os.environ["TOKEN"]')
            results={x.name:x for x in run_checks(scan_project(r))}; self.assertEqual(len(results),8)
            self.assertTrue(all(isinstance(x.status, Status) for x in results.values()))
            self.assertEqual(results['dependency_lockfile'].status, Status.PASS)
            self.assertEqual(results['dockerfile'].status, Status.PASS)

    def test_missing_docs_and_env_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r=pathlib.Path(d); (r/'app.py').write_text('import os; os.environ["X"]')
            results={x.name:x for x in run_checks(scan_project(r))}
            self.assertEqual(results['environment_example'].status, Status.FAIL)
            self.assertEqual(results['readme'].status, Status.FAIL)
            self.assertEqual(results['dependency_lockfile'].status, Status.NOT_APPLICABLE)

    def test_git_states_distinguish_missing_empty_and_committed_repositories(self):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")

        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            self.assertEqual(self._results(root)["git_repository"].status, Status.FAIL)

            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            empty = self._results(root)["git_repository"]
            self.assertEqual(empty.status, Status.WARNING)
            self.assertIn("no commit", empty.explanation)

            (root / "README.md").write_text("content", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=RepoReady Test", "-c",
                 "user.email=repoready-test@example.invalid", "commit", "-qm", "initial"],
                cwd=root,
                check=True,
            )
            committed = self._results(root)["git_repository"]
            self.assertEqual(committed.status, Status.PASS)

    def test_vercel_deployment_without_git_metadata_is_not_applicable(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            with patch.dict(os.environ, {"VERCEL": "1"}):
                result = self._results(root)["git_repository"]

            self.assertEqual(result.status, Status.NOT_APPLICABLE)
            self.assertFalse(result.applicable)

    def test_lockfile_and_runtime_distinctions(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
            requirements = self._results(root)
            self.assertEqual(requirements["dependency_lockfile"].status, Status.WARNING)

            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            no_lock = self._results(root)
            self.assertEqual(no_lock["dependency_lockfile"].status, Status.FAIL)

            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            locked = self._results(root)
            self.assertEqual(locked["dependency_lockfile"].status, Status.PASS)

            (root / ".python-version").write_text("3.12\n", encoding="utf-8")
            self.assertEqual(locked["pinned_runtime"].status, Status.WARNING)
            (root / ".python-version").write_text("3.12.7\n", encoding="utf-8")
            self.assertEqual(self._results(root)["pinned_runtime"].status, Status.PASS)

    def test_dockerfile_static_quality(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            self.assertEqual(self._results(root)["dockerfile"].status, Status.FAIL)

            dockerfile = root / "Dockerfile"
            dockerfile.write_text("RUN echo unsafe\n", encoding="utf-8")
            self.assertEqual(self._results(root)["dockerfile"].status, Status.FAIL)

            dockerfile.write_text("FROM python:3.12.7-slim\n", encoding="utf-8")
            self.assertEqual(self._results(root)["dockerfile"].status, Status.WARNING)

            dockerfile.write_text("FROM python:3.12.7-slim\nCMD [\"python\"]\n", encoding="utf-8")
            self.assertEqual(self._results(root)["dockerfile"].status, Status.PASS)

    def test_test_detection_distinguishes_files_and_documentation(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_text("", encoding="utf-8")
            files_only = self._results(root)["automated_tests"]
            self.assertEqual(files_only.status, Status.WARNING)

            (root / "README.md").write_text("Run `python -m unittest`.\n", encoding="utf-8")
            both = self._results(root)["automated_tests"]
            self.assertEqual(both.status, Status.PASS)

            (root / "tests" / "test_app.py").unlink()
            command_only = self._results(root)["automated_tests"]
            self.assertEqual(command_only.status, Status.WARNING)

    def test_readme_environment_and_absolute_path_checks_are_safe(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            absolute_path = chr(47) + "home/alice/project"
            (root / "app.py").write_text(
                f"HOME = {absolute_path!r}\nTEMP = {(chr(47) + 'tmp/build')!r}\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "Setup and install. Run the application. Test with unittest.\n",
                encoding="utf-8",
            )
            results = self._results(root)
            self.assertEqual(results["readme"].status, Status.PASS)
            self.assertEqual(results["environment_example"].status, Status.NOT_APPLICABLE)
            self.assertEqual(results["absolute_paths"].status, Status.WARNING)
            self.assertTrue(results["absolute_paths"].evidence[0].endswith(":1"))
            self.assertNotIn("alice", results["absolute_paths"].evidence[0])

            (root / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
            (root / "app.py").write_text("import os\nos.getenv('TOKEN')\n", encoding="utf-8")
            self.assertEqual(self._results(root)["environment_example"].status, Status.PASS)

    def test_patch_pinned_docker_runtime_overrides_vague_runtime_line(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (root / ".python-version").write_text("3.12\n", encoding="utf-8")
            (root / "Dockerfile").write_text(
                "FROM python:3.12.7-slim\nCMD [\"python\"]\n",
                encoding="utf-8",
            )

            result = self._results(root)["pinned_runtime"]

            self.assertEqual(result.status, Status.PASS)

    def test_unreadable_dockerfile_is_unknown_not_a_false_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (root / "Dockerfile").write_text("FROM python:3.12.7\n", encoding="utf-8")
            snapshot = scan_project(root, max_file_size=1)
            result = run_checks(snapshot)[3]
            self.assertEqual(result.name, "dockerfile")
            self.assertEqual(result.status, Status.UNKNOWN)

if __name__ == '__main__': unittest.main()
