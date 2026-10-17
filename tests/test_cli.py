"""Stage 1 tests for the RepoReady command-line entry point."""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest


SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from repoready import __version__  # noqa: E402
from repoready.cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    """Verify the configured Stage 1 CLI behavior."""

    def test_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"repoready {__version__}")

    def test_help_without_path(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage: repoready", output.getvalue())

    def test_path_is_rejected_until_stage_two(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            exit_code = main(["project with spaces"])

        self.assertEqual(exit_code, 2)
        self.assertIn("not available until Stage 2", output.getvalue())


if __name__ == "__main__":
    unittest.main()
