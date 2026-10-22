"""Safe, static filesystem snapshots."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_FILE_SIZE = 1024 * 1024
SKIP_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "__pycache__",
                    "dist", "build", "cache", ".cache", ".tox", ".mypy_cache",
                     ".pytest_cache", ".ruff_cache", "coverage", "target",
                     ".gradle", ".next", ".parcel-cache", "vendor", "_vendor",
                     ".vercel", ".opencode"}
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
    safe_example = name in {".env.example", ".env.template", "example.env"}
    return ((name == ".env" or (name.startswith(".env.") and not safe_example)) or
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
        target_info = os.lstat(raw)
    except FileNotFoundError as exc:
        raise InvalidTargetError(f"scan target does not exist: {raw}") from exc
    except OSError as exc:
        raise InvalidTargetError(f"cannot access scan target: {raw}") from exc

    if stat.S_ISLNK(target_info.st_mode):
        raise InvalidTargetError(f"scan target is a symlink: {raw}")
    if not stat.S_ISDIR(target_info.st_mode):
        raise InvalidTargetError(f"scan target is not a directory: {raw}")

    # Do not resolve the target: resolving it would follow a symlink in the
    # target path and could make a scan escape the requested tree.
    root = raw.absolute()

    files: list[FileEntry] = []
    skipped: list[Path] = []
    errors: list[str] = []
    # Keep one result per path, but avoid reading the same inode once for each
    # hardlink.  Sensitive aliases are deliberately never satisfied from this
    # cache, since their content must not be loaded.
    read_cache: dict[tuple[int, int], tuple[bool, str | None, str | None]] = {}
    sensitive_identities: set[tuple[int, int]] = set()
    sensitive_paths: list[Path] = []

    def reference(relative: Path) -> str:
        """Return a safe error reference without exposing sensitive names."""
        if _sensitive(relative):
            return "<sensitive path>"
        return relative.as_posix()

    def record_error(relative: Path, operation: str, exc: BaseException) -> None:
        # Never include OSError.__str__(): it can contain file contents or
        # other sensitive data supplied by a filesystem provider.
        errors.append(f"{reference(relative)}: {operation}: {type(exc).__name__}")

    def record_error_type(relative: Path, operation: str, exception_type: str) -> None:
        errors.append(f"{reference(relative)}: {operation}: {exception_type}")

    def directory_entries(directory: Path) -> tuple[os.DirEntry[str], ...] | None:
        try:
            with os.scandir(directory) as iterator:
                return tuple(sorted(iterator,
                                   key=lambda item: (item.name.casefold(), item.name)))
        except OSError as exc:
            relative = directory.relative_to(root) if directory != root else Path(".")
            record_error(relative, "scandir failed", exc)
            return None

    initial = directory_entries(root)
    if initial is None:
        return ProjectSnapshot(root, (), (), tuple(errors))

    # Each frame is (directory, its sorted entries, next entry index).  Frames
    # replace recursive calls, so arbitrarily deep trees cannot exhaust the
    # Python call stack.
    frames: list[tuple[Path, tuple[os.DirEntry[str], ...], int]] = [(root, initial, 0)]
    while frames:
        directory, children, index = frames[-1]
        if index >= len(children):
            frames.pop()
            continue
        frames[-1] = (directory, children, index + 1)
        item = children[index]
        path = directory / item.name
        relative = path.relative_to(root)
        try:
            if item.is_symlink():
                skipped.append(relative)
            elif item.is_dir(follow_symlinks=False):
                if item.name.casefold() in _SKIP_DIRECTORY_NAMES:
                    skipped.append(relative)
                else:
                    nested = directory_entries(path)
                    if nested is not None:
                        frames.append((path, nested, 0))
            elif item.is_file(follow_symlinks=False):
                sensitive = _sensitive(relative)
                try:
                    info = item.stat(follow_symlinks=False)
                    size = info.st_size
                except OSError as exc:
                    files.append(FileEntry(path, relative, None, False,
                                           error=f"stat failed: {type(exc).__name__}"))
                    record_error(relative, "stat failed", exc)
                    continue

                if sensitive:
                    identity = (getattr(info, "st_dev", 0), getattr(info, "st_ino", 0))
                    if identity[1]:
                        sensitive_identities.add(identity)
                    sensitive_paths.append(path)
                    files.append(FileEntry(path, relative, size, False,
                                           error="sensitive file not loaded"))
                elif size > max_file_size:
                    files.append(FileEntry(path, relative, size, False,
                                           error="file exceeds configured size limit"))
                else:
                    device = getattr(info, "st_dev", 0)
                    inode = getattr(info, "st_ino", 0)
                    # On Windows, DirEntry.stat() can report zero identity
                    # fields even though os.stat() can provide them.
                    if not inode:
                        try:
                            identity = os.stat(path, follow_symlinks=False)
                        except OSError:
                            identity = None
                        if identity is not None:
                            device = getattr(identity, "st_dev", 0)
                            inode = getattr(identity, "st_ino", 0)
                    key = (device, inode)
                    same_as_sensitive = key[1] and key in sensitive_identities
                    if not same_as_sensitive:
                        for sensitive_path in sensitive_paths:
                            try:
                                if os.path.samefile(path, sensitive_path):
                                    same_as_sensitive = True
                                    break
                            except OSError:
                                continue
                    if same_as_sensitive:
                        files.append(FileEntry(
                            path,
                            relative,
                            size,
                            False,
                            error="content not loaded for a sensitive hardlink",
                        ))
                        continue
                    cached = read_cache.get(key) if key[1] else None
                    if cached is None:
                        try:
                            text = path.read_bytes().decode("utf-8", errors="replace")
                        except OSError as exc:
                            cached = (False, None, type(exc).__name__)
                        else:
                            cached = (True, text, None)
                        if key[1]:
                            read_cache[key] = cached

                    is_text, text, read_error = cached
                    if read_error is not None:
                        files.append(FileEntry(path, relative, size, False,
                                               error=f"read failed: {read_error}"))
                        record_error_type(relative, "read failed", read_error)
                    else:
                        files.append(FileEntry(path, relative, size, is_text,
                                               text=text if is_text else None))
        except OSError as exc:
            # DirEntry inspection can fail independently of scandir/stat/read.
            record_error(relative, "entry inspection failed", exc)

    return ProjectSnapshot(root, tuple(files), tuple(skipped), tuple(errors))


scan = scan_project
