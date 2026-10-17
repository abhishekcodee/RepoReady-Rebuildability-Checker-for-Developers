"""Safe, static filesystem snapshots."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_FILE_SIZE = 1024 * 1024
SKIP_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "__pycache__",
                    "dist", "build", "cache", ".cache", ".tox", ".mypy_cache",
                    ".pytest_cache", ".ruff_cache", "coverage", "target",
                    ".gradle", ".next", ".parcel-cache", "vendor"}
_SKIP_DIRECTORY_NAMES = frozenset(name.casefold() for name in SKIP_DIRECTORIES)


class InvalidTargetError(ValueError):
    """Raised when a scan target is missing or is not a directory."""


@dataclass(frozen=True)
class FileEntry:
    """A file discovered without executing or importing it."""

    path: Path
    relative_path: Path
    size: int | None
    is_text: bool
    text: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProjectSnapshot:
    """Immutable result of a bounded, symlink-free scan."""

    root: Path
    files: tuple[FileEntry, ...]
    skipped_paths: tuple[Path, ...] = ()
    scan_errors: tuple[str, ...] = ()

    @property
    def entries(self) -> tuple[FileEntry, ...]:
        """Return all discovered file entries."""
        return self.files

    def get(self, relative: str | os.PathLike[str]) -> FileEntry | None:
        """Find a file by its POSIX-style relative path."""
        wanted = Path(relative).as_posix()
        return next((entry for entry in self.files
                     if entry.relative_path.as_posix() == wanted), None)

    def text(self, relative: str | os.PathLike[str]) -> str | None:
        """Return safe text for a file, or ``None`` when unavailable."""
        entry = self.get(relative)
        return entry.text if entry and entry.is_text else None


def _sensitive(relative: Path) -> bool:
    name = relative.name.lower()
    return (name == ".env" or name.startswith(".env.") or
            any(token in name for token in ("private_key", "private-key",
                                             "privatekey", "id_rsa", "id_ed25519",
                                             "id_ecdsa", "id_dsa")))


def scan_project(target: str | os.PathLike[str],
                 max_file_size: int = DEFAULT_MAX_FILE_SIZE) -> ProjectSnapshot:
    """Scan *target* without following symlinks or reading unsafe files."""
    if max_file_size < 0:
        raise ValueError("max_file_size must be non-negative")
    raw = Path(target).expanduser()
    try:
        if not raw.exists():
            raise InvalidTargetError(f"scan target does not exist: {raw}")
        if not raw.is_dir():
            raise InvalidTargetError(f"scan target is not a directory: {raw}")
        root = raw.resolve()
    except OSError as exc:
        raise InvalidTargetError(f"cannot access scan target: {raw}") from exc

    files: list[FileEntry] = []
    skipped: list[Path] = []
    errors: list[str] = []

    def visit(directory: Path) -> None:
        try:
            iterator = os.scandir(directory)
            with iterator as entries:
                children = sorted(entries, key=lambda item: item.name.lower())
        except (OSError, PermissionError) as exc:
            rel = directory.relative_to(root) if directory != root else Path(".")
            errors.append(f"{rel}: {type(exc).__name__}")
            return
        for item in children:
            path = Path(item.path)
            relative = path.relative_to(root)
            try:
                if item.is_symlink():
                    skipped.append(relative)
                elif item.is_dir(follow_symlinks=False):
                    if item.name.casefold() in _SKIP_DIRECTORY_NAMES:
                        skipped.append(relative)
                    else:
                        visit(path)
                elif item.is_file(follow_symlinks=False):
                    try:
                        size = item.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError) as exc:
                        files.append(FileEntry(path, relative, None, False,
                                               error=f"stat failed: {type(exc).__name__}"))
                        errors.append(f"{relative}: stat failed")
                        continue
                    if _sensitive(relative):
                        files.append(FileEntry(path, relative, size, False,
                                               error="sensitive file not loaded"))
                    elif size > max_file_size:
                        files.append(FileEntry(path, relative, size, False,
                                               error="file exceeds configured size limit"))
                    else:
                        try:
                            text = path.read_bytes().decode("utf-8", errors="replace")
                            files.append(FileEntry(path, relative, size, True, text=text))
                        except (OSError, PermissionError) as exc:
                            files.append(FileEntry(path, relative, size, False,
                                                   error=f"read failed: {type(exc).__name__}"))
                            errors.append(f"{relative}: read failed")
            except (OSError, PermissionError) as exc:
                errors.append(f"{relative}: {type(exc).__name__}")

    visit(root)
    return ProjectSnapshot(root, tuple(files), tuple(skipped), tuple(errors))


scan = scan_project
