import json
import math
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from repoready.checks import CheckResult, Status, run_checks
from repoready.report import (
    REPORT_SCHEMA_VERSION,
    ReportWriteError,
    build_report,
    serialize_report,
    write_json_report,
)
from repoready.scanner import FileEntry, ProjectSnapshot, scan_project
from repoready.scoring import calculate_score


class ReportTests(unittest.TestCase):
    def test_real_scan_check_score_integration_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "pyproject.toml").write_text(
                '[project]\nrequires-python = ">=3.12.7,<3.13"\n', encoding="utf-8"
            )
            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (root / ".python-version").write_text("3.12.7\n", encoding="utf-8")
            (root / "README.md").write_text("setup, run, test\n", encoding="utf-8")
            snapshot = scan_project(root)
            results = run_checks(snapshot)
            score = calculate_score(results)

            report = build_report(
                snapshot,
                results,
                score,
                generated_at=datetime(2026, 10, 17, 12, 30, tzinfo=timezone.utc),
            )
            loaded = json.loads(serialize_report(report))

            self.assertEqual(loaded["schema_version"], REPORT_SCHEMA_VERSION)
            self.assertEqual(loaded["tool"]["name"], "RepoReady")
            self.assertEqual(loaded["project"]["path"], str(root.resolve()))
            self.assertEqual(loaded["generated_at"], "2026-10-17T12:30:00Z")
            self.assertEqual(loaded["scan_timestamp"], "2026-10-17T12:30:00Z")
            self.assertIn("overall_score", loaded["score"])
            self.assertIn("maximum", loaded["score"])
            self.assertIn("applicable_weight", loaded["score"])
            self.assertIn("evaluable_weight", loaded["score"])
            self.assertIn("applicable_count", loaded["score"])
            self.assertIn("evaluable_count", loaded["score"])
            self.assertIn("coverage", loaded["score"])
            self.assertIn("formula", loaded["score"])
            self.assertIn("limitations", loaded["score"])
            self.assertEqual(len(loaded["checks"]), len(results))
            self.assertEqual(
                [item["name"] for item in loaded["checks"]],
                [item.name for item in results],
            )
            self.assertEqual(
                loaded["summary"]["status_counts"]["PASS"],
                sum(result.status is Status.PASS for result in results),
            )
            self.assertEqual(
                set(loaded["summary"]["status_counts"]),
                {"PASS", "FAIL", "WARNING", "NOT_APPLICABLE", "UNKNOWN"},
            )

    def test_unicode_redaction_and_check_fields(self):
        snapshot = ProjectSnapshot(pathlib.Path("/tmp/プロジェクト"), ())
        result = CheckResult(
            "readme",
            Status.FAIL,
            "説明: café API_KEY='unsafe-value'",
            evidence=(".env contains PASSWORD=hunter2", "unicode ✓"),
            recommendation="Set token: super-secret",
            path=pathlib.Path("docs/é.md"),
            line_number=4,
            limitations=("secret = should-not-leak",),
        )
        report = build_report(snapshot, (result,), generated_at=datetime(2026, 1, 1))
        text = serialize_report(report)
        loaded = json.loads(text)
        check = loaded["checks"][0]

        self.assertIn("café", text)
        self.assertIn("unicode ✓", text)
        self.assertNotIn("unsafe-value", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("super-secret", text)
        self.assertNotIn("should-not-leak", text)
        self.assertEqual(check["path"], "docs/é.md")
        self.assertEqual(check["line_number"], 4)
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["priority"], "high")
        self.assertEqual(len(loaded["recommendations"]), 1)

    def test_scanner_errors_and_limitations_are_reported_without_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            secret_file = root / ".env"
            secret_file.write_text("API_KEY=do-not-report", encoding="utf-8")
            unreadable = root / "unreadable.txt"
            unreadable.write_text("content", encoding="utf-8")
            with patch.object(pathlib.Path, "read_bytes", side_effect=PermissionError("denied")):
                snapshot = scan_project(root)
            report = build_report(snapshot, ())
            scanner = report["scanner"]
            self.assertTrue(scanner["scan_errors"])
            self.assertTrue(scanner["limitations"])
            rendered = serialize_report(report)
            self.assertNotIn("do-not-report", rendered)
            self.assertIn("Sensitive files", rendered)

    def test_malformed_scanner_errors_are_reduced_to_safe_summaries(self):
        snapshot = ProjectSnapshot(
            pathlib.Path("/tmp/プロジェクト"),
            (FileEntry(
                pathlib.Path("/tmp/プロジェクト/bad.txt"),
                pathlib.Path("bad.txt"),
                10,
                False,
                error="API_KEY=synthetic-scanner-secret",
            ),),
            scan_errors=(
                "bad.txt: read failed: PermissionError",
                "API_KEY=synthetic-scanner-secret",
            ),
        )
        rendered = serialize_report(build_report(snapshot, ()))
        self.assertNotIn("synthetic-scanner-secret", rendered)
        self.assertIn("Scanner error: read failed (PermissionError)", rendered)
        self.assertIn("bad.txt: file could not be safely inspected.", rendered)

    def test_prefixed_scanner_error_is_not_trusted_or_echoed(self):
        snapshot = ProjectSnapshot(
            pathlib.Path("/tmp/project"), (),
            scan_errors=("Scanner error: API_KEY=synthetic-prefixed-secret",),
        )

        rendered = serialize_report(build_report(snapshot, ()))

        self.assertNotIn("synthetic-prefixed-secret", rendered)
        self.assertIn("Scanner error: details unavailable", rendered)

    def test_secret_like_project_path_is_redacted_in_report_metadata(self):
        secret_name = "project-API_KEY=synthetic-path-secret"
        snapshot = ProjectSnapshot(pathlib.Path("/tmp") / secret_name, ())

        rendered = serialize_report(build_report(snapshot, ()))

        self.assertNotIn("synthetic-path-secret", rendered)

    def test_supported_objects_are_json_safe(self):
        rendered = serialize_report(
            {
                "status": Status.WARNING,
                "path": pathlib.Path("src/app.py"),
                "when": datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3))),
            }
        )
        value = json.loads(rendered)
        self.assertEqual(value, {"path": "src/app.py", "status": "WARNING", "when": "2026-01-01T00:00:00Z"})

    def test_malformed_and_nonfinite_values_serialize_as_safe_json(self):
        class BrokenValue:
            def __str__(self):
                raise AssertionError("this value must never be stringified")

        rendered = serialize_report({
            "unicode": "café ✓",
            "secret": "PASSWORD=synthetic-report-secret",
            "nan": math.nan,
            "positive_infinity": math.inf,
            "negative_infinity": -math.inf,
            "malformed": BrokenValue(),
        })
        value = json.loads(rendered)
        self.assertEqual(value["unicode"], "café ✓")
        self.assertIsNone(value["nan"])
        self.assertIsNone(value["positive_infinity"])
        self.assertIsNone(value["negative_infinity"])
        self.assertEqual(value["malformed"], "<BrokenValue>")
        self.assertNotIn("synthetic-report-secret", rendered)

    def test_empty_report_input_is_safe(self):
        snapshot = ProjectSnapshot(pathlib.Path("project with spaces"), ())
        report = build_report(snapshot, None)

        self.assertEqual(report["checks"], [])
        self.assertEqual(report["recommendations"], [])
        self.assertEqual(report["score"]["overall_score"], 0.0)
        json.loads(serialize_report(report))

    def test_atomic_write_replaces_existing_file_and_rejects_missing_parent(self):
        report = {"unicode": "café", "status": Status.PASS}
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "report.json"
            output.write_text("old", encoding="utf-8")
            returned = write_json_report(report, output)
            self.assertEqual(returned, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["unicode"], "café")
            self.assertFalse(any(path.name.startswith(".report.json.") for path in pathlib.Path(directory).iterdir()))
            with self.assertRaises(ReportWriteError):
                write_json_report(report, pathlib.Path(directory) / "missing" / "report.json")

    def test_atomic_write_failure_preserves_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "report.json"
            output.write_text("original", encoding="utf-8")
            with patch("repoready.report.os.replace", side_effect=PermissionError("denied")):
                with self.assertRaises(ReportWriteError) as error:
                    write_json_report({"status": Status.PASS}, output)
            self.assertIn("atomically write", str(error.exception))
            self.assertNotIn("denied", str(error.exception))
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
            self.assertFalse(any(path.name.startswith(".report.json.") for path in pathlib.Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
