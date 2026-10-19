"""Integration tests for the RepoReady command-line workflow."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from repoready import __version__  # noqa: E402
from repoready.cli import main  # noqa: E402
from repoready.scanner import ProjectSnapshot, scan_project  # noqa: E402
from repoready.checks import run_checks  # noqa: E402
from repoready.scoring import calculate_score  # noqa: E402


class CliTests(unittest.TestCase):
    """Verify user-visible CLI behavior without executing fixture projects."""

    def _project(self, root: pathlib.Path, *, token: str | None = None) -> None:
        (root / ".git").mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\nrequires-python = ">=3.12.7,<3.13"\n',
            encoding="utf-8",
        )
        (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (root / ".python-version").write_text("3.12.7\n", encoding="utf-8")
        (root / "Dockerfile").write_text(
            "FROM python@sha256:" + "a" * 64 + '\nCMD ["python"]\n',
            encoding="utf-8",
        )
        (root / "tests").mkdir()
        (root / "tests" / "test_fixture.py").write_text(
            "# This fixture is never imported or executed.\n", encoding="utf-8"
        )
        (root / "README.md").write_text(
            "Setup and install. Run the application. Test with unittest.\n",
            encoding="utf-8",
        )
        (root / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
        (root / "app.py").write_text(
            "import os\nvalue = os.getenv('TOKEN')\n",
            encoding="utf-8",
        )
        if token is not None:
            (root / ".env").write_text(f"API_KEY={token}\n", encoding="utf-8")

    def _run(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_successful_scan_runs_eight_checks_and_reports_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root)
            expected = calculate_score(run_checks(scan_project(root)))

            code, stdout, stderr = self._run([str(root)])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("RepoReady report", stdout)
            self.assertIn(f"Score: {expected.overall_score}/{expected.maximum}", stdout)
            self.assertIn(f"Coverage: {expected.coverage}%", stdout)
            self.assertIn(
                f"Checks: {expected.applicable_count} applicable, "
                f"{expected.evaluable_count} evaluable",
                stdout,
            )
            self.assertEqual(stdout.count("- ["), 8 + len(  # checks + recommendations
                [result for result in run_checks(scan_project(root))
                 if result.status.value in {"FAIL", "WARNING"}]
            ))
            for name in (
                "git_repository",
                "dependency_lockfile",
                "pinned_runtime",
                "dockerfile",
                "automated_tests",
                "readme",
                "environment_example",
                "absolute_paths",
            ):
                self.assertIn(name, stdout)
            self.assertIn("Limitations:", stdout)

    def test_invalid_target_returns_two_without_exception_details(self) -> None:
        code, stdout, stderr = self._run(["path-that-does-not-exist"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("scan target must be an existing directory", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_json_export_contains_actual_sanitized_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root)
            output = root / "reports" / "report.json"
            output.parent.mkdir()

            code, stdout, stderr = self._run([str(root), "--json", str(output)])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("JSON report written to", stdout)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["project"]["path"], str(root.resolve()))
            self.assertEqual(len(report["checks"]), 8)
            self.assertIn("overall_score", report["score"])
            self.assertIn("recommendations", report)

    def test_invalid_json_output_returns_one_without_success_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root)
            missing_parent = root / "does-not-exist" / "report.json"

            code, stdout, stderr = self._run([str(root), "--json", str(missing_parent)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("could not write the JSON report", stderr)
            self.assertNotIn("written to", stderr)

    def test_export_exception_is_generic_and_does_not_print_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root)
            secret = "PASSWORD=synthetic-export-secret"
            with patch(
                "repoready.cli.write_json_report",
                side_effect=OSError(secret),
            ):
                code, stdout, stderr = self._run([str(root), "--json", str(root / "report.json")])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("could not write the JSON report", stderr)
            self.assertNotIn(secret, stderr)
            self.assertNotIn("synthetic-export-secret", stderr)

    def test_verbose_scanner_errors_are_summaries_without_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root)
            secret = "API_KEY=synthetic-cli-scanner-secret"
            snapshot = ProjectSnapshot(root, (), scan_errors=(secret,))
            with patch("repoready.cli.scan_project", return_value=snapshot):
                code, stdout, stderr = self._run([str(root), "--verbose"])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("Scanner error:", stdout)
            self.assertNotIn("synthetic-cli-scanner-secret", stdout)

    def test_unsupported_argument_uses_argparse_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["--unsupported"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_unicode_project_path_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "proyecto-✓"
            root.mkdir()
            self._project(root)

            code, stdout, stderr = self._run([str(root)])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(str(root.resolve()), stdout)

    def test_scanner_limitations_are_visible_without_project_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root)
            marker = root / "executed.txt"
            (root / "project_code.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )

            with patch.object(pathlib.Path, "read_bytes", side_effect=PermissionError("denied")):
                code, stdout, stderr = self._run([str(root), "--verbose"])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("Limitations:", stdout)
            self.assertIn("Scanner error:", stdout)
            self.assertFalse(marker.exists(), "the scanned project must not execute")

    def test_help_and_version(self) -> None:
        help_stdout = io.StringIO()
        with contextlib.redirect_stdout(help_stdout):
            code = main([])
        self.assertEqual(code, 0)
        self.assertIn("usage: repoready", help_stdout.getvalue())
        self.assertIn("--json PATH", help_stdout.getvalue())
        self.assertIn("Exit codes", help_stdout.getvalue())

        version_stdout = io.StringIO()
        with contextlib.redirect_stdout(version_stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(version_stdout.getvalue().strip(), f"repoready {__version__}")

    def test_sensitive_fixture_value_is_not_in_terminal_or_json_report(self) -> None:
        token = "ghp_fixture_token_must_not_leak"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._project(root, token=token)
            output = root / "report.json"

            code, stdout, stderr = self._run([str(root), "--json", str(output)])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertNotIn(token, stdout)
            self.assertNotIn(token, output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
