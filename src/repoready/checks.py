"""Static rebuildability checks for a :class:`ProjectSnapshot`."""
from __future__ import annotations

import re
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .scanner import ProjectSnapshot


class Status(str, Enum):
    """The outcome vocabulary used by every check."""
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CheckResult:
    """Safe, explainable result from one independent check."""
    name: str
    status: Status
    explanation: str
    evidence: tuple[str, ...] = ()
    recommendation: str = ""
    path: Path | None = None
    line_number: int | None = None
    applicable: bool = True
    limitations: tuple[str, ...] = ()


def _text(snapshot: ProjectSnapshot, name: str) -> str | None:
    return snapshot.text(name)


def _result(name: str, status: Status, explanation: str,
            evidence: tuple[str, ...] = (), recommendation: str = "",
            **kwargs: object) -> CheckResult:
    return CheckResult(name, status, explanation, evidence, recommendation, **kwargs)


def _recognized_project(snapshot: ProjectSnapshot) -> bool:
    """Return whether static evidence suggests an actual software project."""
    markers = {
        "pyproject.toml", "setup.py", "requirements.txt", "Pipfile",
        "package.json", "Dockerfile",
    }
    if any(snapshot.get(marker) for marker in markers):
        return True
    source_suffixes = {".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".rs"}
    return any(entry.relative_path.suffix.lower() in source_suffixes
               for entry in snapshot.files)


def check_git_repository(snapshot: ProjectSnapshot) -> CheckResult:
    """Detect repository presence and whether HEAD names a commit."""
    metadata = snapshot.root / ".git"
    if os.environ.get("VERCEL") == "1" and not metadata.exists():
        return _result(
            "git_repository",
            Status.NOT_APPLICABLE,
            "Git metadata is not included in the Vercel deployment artifact",
            applicable=False,
            limitations=("Deployment artifacts do not include the source Git directory.",),
        )
    if shutil.which("git") is None:
        if metadata.exists():
            return _result("git_repository", Status.UNKNOWN,
                           "Git metadata exists but Git is unavailable",
                           ("static .git metadata detected",),
                           limitations=("Commit state and parent repositories could not be validated.",))
        return _result("git_repository", Status.UNKNOWN,
                       "Git availability is required to determine repository ancestry",
                       limitations=("Git executable unavailable.",))
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                cwd=snapshot.root, capture_output=True, text=True,
                                timeout=3)
        if inside.returncode != 0:
            return _result("git_repository", Status.FAIL,
                           "No Git repository detected",
                           recommendation="Initialize Git and create a commit.")
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                              cwd=snapshot.root, capture_output=True, text=True,
                              timeout=3)
        if head.returncode == 0:
            return _result("git_repository", Status.PASS,
                           "Git repository contains a commit", ("HEAD verified",))
        return _result("git_repository", Status.WARNING,
                       "Git repository is initialized but has no commit",
                       ("repository detected",), "Create the first commit.")
    except (OSError, subprocess.SubprocessError):
        return _result("git_repository", Status.UNKNOWN,
                       "Git metadata exists but validation failed",
                       limitations=("Git command could not be completed.",))


def check_dependency_lockfile(snapshot: ProjectSnapshot) -> CheckResult:
    """Assess recognized Python and Node dependency lockfiles."""
    python = any(_text(snapshot, n) is not None
                 for n in ("pyproject.toml", "Pipfile", "setup.py", "requirements.txt"))
    node = any(_text(snapshot, n) is not None for n in ("package.json",))
    lock_names = ("uv.lock", "poetry.lock", "Pipfile.lock",
                  "package-lock.json", "pnpm-lock.yaml", "yarn.lock")
    lock_entries = [snapshot.get(name) for name in lock_names]
    unreadable = [entry.relative_path.as_posix() for entry in lock_entries
                  if entry is not None and not entry.is_text]
    locks = [entry.relative_path.as_posix() for entry in lock_entries
             if entry is not None and entry.is_text and entry.text and entry.text.strip()]
    if not python and not node and not locks:
        return _result("dependency_lockfile", Status.NOT_APPLICABLE,
                       "No recognized Python or Node ecosystem found", applicable=False)
    if locks:
        return _result("dependency_lockfile", Status.PASS, "Dependency lockfile detected",
                       tuple(locks))
    if unreadable:
        return _result(
            "dependency_lockfile", Status.UNKNOWN,
            "A dependency lockfile exists but could not be safely evaluated",
            tuple(unreadable),
            limitations=("The lockfile was unreadable, sensitive, or over the scan size limit.",),
        )
    if any(entry is not None for entry in lock_entries):
        return _result(
            "dependency_lockfile", Status.WARNING,
            "A lockfile name was found but its contents were empty",
            tuple(entry.relative_path.as_posix() for entry in lock_entries if entry is not None),
            recommendation="Regenerate and commit a valid dependency lockfile.",
        )
    if (_text(snapshot, "requirements.txt") is not None and
            _text(snapshot, "pyproject.toml") is None and
            _text(snapshot, "Pipfile") is None):
        return _result("dependency_lockfile", Status.WARNING,
                       "requirements.txt is a dependency list, not a full lockfile",
                       ("requirements.txt",), "Add a committed ecosystem lockfile.")
    return _result("dependency_lockfile", Status.FAIL,
                   "Recognized ecosystem has no dependency lockfile",
                   recommendation="Add and commit the appropriate lockfile.")


def check_pinned_runtime(snapshot: ProjectSnapshot) -> CheckResult:
    """Check for exact-ish runtime or digest-pinned base evidence."""
    ecosystem = any(_text(snapshot, n) is not None for n in
                    ("pyproject.toml", "package.json", "requirements.txt", "Dockerfile",
                     ".python-version", ".nvmrc", ".tool-versions"))
    if not ecosystem:
        return _result("pinned_runtime", Status.NOT_APPLICABLE,
                       "No recognized runtime ecosystem found", applicable=False)
    evidence: list[str] = []
    vague = False
    for name in (".python-version", ".nvmrc", ".tool-versions"):
        value = _text(snapshot, name)
        if value:
            (evidence.append(name) if re.search(r"\b\d+\.\d+\.\d+(?:[-+._][\w.-]+)?\b", value)
             else None)
            vague |= not bool(re.search(r"\b\d+\.\d+\.\d+\b", value))
    pyproject = _text(snapshot, "pyproject.toml") or ""
    docker = _text(snapshot, "Dockerfile") or ""
    bounded = bool(re.search(
        r"requires-python\s*=\s*['\"][^'\"]*(?:==\s*\d+\.\d+\.\d+|"
        r">=\s*\d+\.\d+\.\d+[^'\"]*[,<])",
        pyproject,
    ))
    digest = bool(re.search(r"^FROM\s+\S+@sha256:[0-9a-fA-F]{64}\s*$", docker, re.M))
    tagged = bool(re.search(
        r"^FROM\s+(?:--platform=\S+\s+)?\S+:\d+\.\d+\.\d+(?:[-._][\w.-]+)?\s*$",
        docker,
        re.M,
    ))
    if bounded or digest or tagged:
        evidence.append(
            "bounded runtime declaration" if bounded
            else "digest-pinned Docker base" if digest
            else "patch-pinned Docker base"
        )
    if evidence and (not vague or bounded or digest or tagged):
        return _result("pinned_runtime", Status.PASS, "Pinned runtime evidence found", tuple(evidence))
    return _result("pinned_runtime", Status.WARNING, "Runtime evidence is missing or vague", tuple(evidence), "Pin an exact runtime version or Docker base digest.")


def check_dockerfile(snapshot: ProjectSnapshot) -> CheckResult:
    """Inspect Dockerfile structure without building it."""
    entry = snapshot.get("Dockerfile")
    ecosystem = _recognized_project(snapshot)
    if entry is None:
        return _result("dockerfile", Status.FAIL if ecosystem else Status.NOT_APPLICABLE,
                       "Dockerfile is missing" if ecosystem else "Empty or unrecognized project",
                       recommendation="Add a Dockerfile with a pinned base image." if ecosystem else "", applicable=ecosystem)
    if not entry.is_text:
        return _result("dockerfile", Status.UNKNOWN, "Dockerfile could not be safely read", limitations=(entry.error or "unreadable",))
    text = entry.text or ""
    if not re.search(r"^FROM\s+", text, re.M):
        return _result("dockerfile", Status.FAIL, "Dockerfile has no FROM instruction")
    has_run = bool(re.search(r"^(CMD|ENTRYPOINT)\b", text, re.M))
    pinned = bool(re.search(r"^FROM\s+\S+@sha256:[0-9a-fA-F]{64}\s*$", text, re.M))
    if not has_run: return _result("dockerfile", Status.WARNING, "Dockerfile has no CMD or ENTRYPOINT", recommendation="Add a runtime command.")
    if not pinned:
        tagged = bool(re.search(
            r"^FROM\s+(?:--platform=\S+\s+)?\S+:\d+\.\d+\.\d+(?:[-._][\w.-]+)?\s*$",
            text,
            re.M,
        ))
        if not tagged:
            return _result(
                "dockerfile", Status.WARNING,
                "Docker base image is not pinned to a patch tag or digest",
                recommendation="Pin FROM to an exact version tag or image digest.",
            )
    return _result("dockerfile", Status.PASS, "Dockerfile has static FROM and runtime command")


def check_automated_tests(snapshot: ProjectSnapshot) -> CheckResult:
    """Find project tests and commands only in README/config files."""
    test_files = tuple(f.relative_path.as_posix() for f in snapshot.files
                       if re.search(r"(^|/)(tests?/|test_|.*_test\.)", f.relative_path.as_posix(), re.I))
    docs = "\n".join(filter(None, (_text(snapshot, "README.md"), _text(snapshot, "pyproject.toml"), _text(snapshot, "package.json"), _text(snapshot, "Makefile"))))
    command = bool(re.search(r"\b(pytest|unittest|npm\s+test|pnpm\s+test|yarn\s+test|make\s+test|uv\s+run\s+pytest)\b", docs, re.I))
    if test_files and command: status, explanation = Status.PASS, "Test files and a documented test command found"
    elif test_files: status, explanation = Status.WARNING, "Test files found but no documented test command"
    elif command: status, explanation = Status.WARNING, "A test command is documented but no test files were found"
    else: status, explanation = Status.FAIL, "No project test files or documented test command found"
    return _result("automated_tests", status, explanation, test_files, "Add tests and document one offline test command.")


def check_readme(snapshot: ProjectSnapshot) -> CheckResult:
    """Check root README workflow documentation."""
    entry = next((f for f in snapshot.files if f.relative_path.name.lower() == "readme.md" and len(f.relative_path.parts) == 1), None)
    if entry is None: return _result("readme", Status.FAIL, "README.md is missing", recommendation="Document setup, run, and test steps.")
    if not entry.is_text: return _result("readme", Status.UNKNOWN, "README.md could not be read")
    text = entry.text or ""
    found = tuple(label for label, pattern in (("setup/install", r"setup|install"), ("run/usage", r"run|usage"), ("test", r"test")) if re.search(pattern, text, re.I))
    return _result("readme", Status.PASS if len(found) == 3 else Status.WARNING, "README documents the core workflow" if len(found) == 3 else "README documentation is incomplete", found, "Document setup/install, run/usage, and test commands.")


def check_environment_example(snapshot: ProjectSnapshot) -> CheckResult:
    """Require examples only when source/config statically references env vars."""
    example = next((n for n in (".env.example", ".env.template", "example.env") if snapshot.get(n)), None)
    source = tuple(f for f in snapshot.files if f.relative_path.name.lower() != "readme.md" and f.relative_path.name not in (".env", ".env.local"))
    references = any(re.search(r"os\.environ|getenv\s*\(|process\.env|\$\{[A-Za-z_][A-Za-z0-9_]*\}", f.text or "") for f in source if f.is_text)
    if example: return _result("environment_example", Status.PASS, "Environment example file found", (example,))
    if not references: return _result("environment_example", Status.NOT_APPLICABLE, "No static environment references or example found", applicable=False)
    return _result("environment_example", Status.FAIL, "Environment variables are referenced without an example", recommendation="Add a sanitized .env.example.")


def check_absolute_paths(snapshot: ProjectSnapshot) -> CheckResult:
    """Warn about machine-specific paths while returning no source content."""
    findings: list[tuple[Path, int]] = []
    relevant = {".py", ".js", ".ts", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".sh", ".md"}
    pattern = re.compile(
        r"(?:\b[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+"
        r"|/(?:Users|home)/[^/\s\"']+)"
    )
    for entry in snapshot.files:
        if not entry.is_text or entry.path.suffix.lower() not in relevant: continue
        for number, line in enumerate((entry.text or "").splitlines(), 1):
            if pattern.search(line) and "http://" not in line and "https://" not in line:
                findings.append((entry.relative_path, number))
    evidence = tuple(f"{path.as_posix()}:{line}" for path, line in findings[:20])
    return _result("absolute_paths", Status.WARNING if findings else Status.PASS, "Machine-specific absolute paths detected" if findings else "No obvious machine-specific absolute paths detected", evidence, "Replace machine-specific paths with relative or configurable paths.", limitations=("Findings are heuristic and expose no source-line content.",) if findings else ())


def run_checks(snapshot: ProjectSnapshot) -> tuple[CheckResult, ...]:
    """Run all eight independent checks in stable order."""
    return tuple(check(snapshot) for check in (check_git_repository, check_dependency_lockfile, check_pinned_runtime, check_dockerfile, check_automated_tests, check_readme, check_environment_example, check_absolute_paths))
