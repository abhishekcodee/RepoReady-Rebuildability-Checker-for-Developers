"""Stage 1 command-line entry point for RepoReady."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser used by the ``repoready`` command."""
    parser = argparse.ArgumentParser(
        prog="repoready",
        description=(
            "Inspect a software project for rebuildability problems. "
            "Project scanning will be added in Stage 2."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="project directory to inspect (available starting in Stage 2)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Stage 1 CLI and return a process exit code.

    The command intentionally does not inspect project directories yet. This
    keeps Stage 1 limited to architecture and environment setup rather than
    introducing a placeholder scanner.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.path is not None:
        print(
            "RepoReady is configured, but project scanning is not available "
            "until Stage 2.",
            file=sys.stderr,
        )
        return 2

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
