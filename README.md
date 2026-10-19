# RepoReady

RepoReady is a local, offline rebuildability checker for software projects. It
performs bounded, static filesystem inspection and reports configuration gaps
that can make a project difficult to share, reproduce, or rebuild. RepoReady
does not import or execute the inspected project, run its tests, build its
containers, install its dependencies, or make network calls.

The project is intended for developers, maintainers, reviewers, and teams who
want a quick, explainable signal before handing a repository to another person
or automation system. Its score is a quality signal based on observable files;
it is not proof that a project builds or that its tests pass.

## Goals and users

RepoReady helps users:

- identify missing lockfiles, runtime pins, Docker entrypoints, tests, or
  workflow documentation;
- see actionable recommendations and safe file references;
- produce a sanitized JSON report for review or another local tool; and
- inspect a project without running untrusted project code or contacting a
  service.

It is useful during repository handoff, reproducibility reviews, local
development, and documentation or release preparation. It is deliberately
small and conservative rather than a build system, package-manager wrapper, or
complete security scanner.

## Features

- Eight deterministic rebuildability checks with weighted scoring.
- Python-primary project recognition, plus static recognition of common Node
  project manifests and lockfiles.
- A bounded scanner with a default 1 MiB per-file text-read limit.
- Case-insensitive skipping of generated/dependency directories and all
  symlinks.
- No loading of `.env` files or private-key-like files.
- Terminal reports containing the score, coverage, every check result,
  recommendations, and limitations.
- Optional `--verbose` scanner summaries and skipped-path details.
- Versioned, UTF-8, sanitized JSON export written atomically.
- A standard-library runtime with a committed `uv.lock` file.

## Supported ecosystems and scope

RepoReady itself is a Python package and its runtime is Python 3.12.7 or a
later Python 3.12 patch release.

### Python-primary recognition

The checks recognize these Python project markers:

- manifests or dependency declarations: `pyproject.toml`, `setup.py`,
  `requirements.txt`, and `Pipfile`;
- dependency lockfiles: `uv.lock`, `poetry.lock`, and `Pipfile.lock`; and
- runtime evidence: `.python-version`, `.tool-versions`, a bounded
  `requires-python` declaration, or a pinned Docker base image.

`requirements.txt` is treated as a dependency list, not as a complete lockfile.
RepoReady does not parse Python dependency graphs, resolve versions, install
packages, import modules, or run Python build/test commands in the scanned
project.

### Recognized Node ecosystems

The static checks recognize a Node project when `package.json` is present and
recognize these lockfile names: `package-lock.json`, `pnpm-lock.yaml`, and
`yarn.lock`. `.nvmrc` and `.tool-versions` can provide Node runtime evidence.
JavaScript and TypeScript source suffixes also contribute to the generic
project-recognition check.

Recognition is intentionally limited: RepoReady does not run npm, pnpm, or
Yarn; parse manifests; verify that a lockfile matches `package.json`; resolve
semver ranges; inspect package-manager configuration; or build/test Node code.
The presence of a filename is static evidence, not proof that the ecosystem is
complete or usable.

Other projects may still be scanned. Generic project recognition also accepts
a `Dockerfile` or common source suffixes, but the eight checks do not provide a
full implementation-specific analysis for every language or package manager.

## Architecture and source layout

```text
src/repoready/
  __init__.py   Package version (0.1.0)
  scanner.py    Bounded, symlink-free static filesystem snapshot
  checks.py     Eight independent checks and status vocabulary
  scoring.py    Weights, score/coverage calculation, recommendations
  report.py     Sanitized versioned report and atomic JSON writer
  cli.py        argparse CLI, terminal rendering, and exit codes
tests/          Offline unittest suite for scanner, checks, scoring, reports, CLI
Dockerfile      Reproducible container image and repoready entrypoint
pyproject.toml  Package metadata and console-script declaration
uv.lock         Committed uv lockfile
.python-version Python runtime pin: 3.12.7
.env.example    Documented empty environment template
LICENSE         MIT License
```

The installed console script is `repoready = repoready.cli:main`.

## Prerequisites

- Python `3.12.7` or a later `3.12.x` release. The project requires
  `>=3.12.7,<3.13`.
- [uv](https://docs.astral.sh/uv/) for the virtual environment, editable
  install, and lockfile workflow. The repository Dockerfile and CI workflow
  use uv `0.13.1`.
- Git for normal repository work. The `git_repository` check uses
  `git rev-parse` when Git is available.
- Docker only for the optional container workflow.

RepoReady has no runtime dependencies outside Python's standard library. The
packaging build dependency is pinned in `pyproject.toml`, and `uv.lock` records
the locked project resolution used by `uv sync --locked`.

## Setup with uv

From the repository root:

```bash
uv sync --locked
```

`--locked` requires `pyproject.toml` and `uv.lock` to agree and does not update
the lockfile. The synchronized environment contains the `repoready` command.

RepoReady itself does not require environment variables. The tracked
`.env.example` exists to make that fact explicit; it contains comments only.

## Command-line usage

The complete command shape is:

```text
repoready [--json PATH] [--verbose] [--version] [PATH]
```

After setup, invoke it through uv:

```bash
uv run repoready --help
uv run repoready --version
uv run repoready PATH
uv run repoready PATH --json report.json
uv run repoready PATH --verbose
uv run repoready PATH --verbose --json report.json
```

Options and arguments:

- `PATH` is an existing project directory to inspect. A missing, non-directory,
  or symlink target is invalid.
- `--json PATH` writes the sanitized report to the specified file. Its parent
  directory must already exist; RepoReady does not create parent directories.
- `--verbose` adds sanitized scanner-error summaries and skipped-path details
  to the limitations section. It does not expose file contents or raw
  exception messages.
- `--help` (or `-h`) prints usage and the exit-code summary.
- `--version` prints `repoready 0.1.0` and exits.

If no `PATH` is supplied, RepoReady prints help and exits successfully. Using
`--json` without a project `PATH` is invalid.

Exit codes are:

| Code | Meaning |
| ---: | --- |
| `0` | The scan and report succeeded, even when checks have findings. |
| `1` | An operational failure occurred, including a JSON export failure. |
| `2` | Command-line input or the scan target is invalid. |

## Terminal reports

A successful scan prints:

1. the project path;
2. the normalized score out of 100 and coverage percentage;
3. counts of applicable and evaluable checks;
4. all eight check names, statuses, and explanations;
5. recommendations for failed and warning checks; and
6. scanner, score, and check limitations.

When `--json` succeeds, a final `JSON report written to ...` line identifies
the output path. If report writing fails, the command prints an error to
standard error and does not claim that a report was written.

RepoReady may invoke the host `git` executable for the Git repository check,
but it never executes code, setup scripts, tests, or commands from the scanned
project. It also makes no network calls.

## JSON report export

For example:

```bash
mkdir -p reports
uv run repoready ./some-project --json reports/repoready.json
```

The JSON document uses schema version `1.0` and includes tool metadata,
timestamps, the scanned project path, score fields, status counts, each check,
recommendations, and scanner limitations. It contains references and findings,
not scanned source-file contents. Obvious secret assignments, common token
prefixes, bearer tokens, and sensitive scanner details are redacted on a
best-effort basis. The writer flushes and atomically replaces the destination;
temporary files are cleaned up on failure.

## Score, weights, statuses, and coverage

RepoReady scores the eight checks with these exact weights:

| Check | Weight |
| --- | ---: |
| `git_repository` | 10 |
| `dependency_lockfile` | 15 |
| `pinned_runtime` | 15 |
| `dockerfile` | 15 |
| `automated_tests` | 15 |
| `readme` | 10 |
| `environment_example` | 10 |
| `absolute_paths` | 10 |
| **Total** | **100** |

For applicable checks:

```text
earned_weight = sum(weight × status_factor)
overall_score = 100 × earned_weight / applicable_weight
```

The factors are `PASS = 1.0`, `WARNING = 0.5`, `FAIL = 0.0`, and
`UNKNOWN = 0.0`. `NOT_APPLICABLE` checks are excluded from the denominator.
Scores are normalized to a maximum of 100 and rounded to two decimal places.
If there are no applicable checks, the score and coverage are both `0.0`.

Every check uses one of these statuses:

- `PASS`: the static evidence satisfies the check.
- `WARNING`: evidence is incomplete, vague, or only partially satisfactory;
  it earns half the check weight.
- `FAIL`: the expected evidence is absent or a clear issue was found; it earns
  no weight.
- `UNKNOWN`: the check could not be safely or reliably evaluated, for example
  because a file was unreadable or a required tool was unavailable; it earns no
  weight and remains in the applicable denominator.
- `NOT_APPLICABLE`: the check does not apply to the project; it is excluded
  from the applicable denominator and has `applicable: false` in reports.

Coverage is separate from score:

```text
coverage = 100 × evaluable_weight / applicable_weight
```

`PASS`, `WARNING`, and `FAIL` are evaluable. `UNKNOWN` is applicable but not
evaluable, so it lowers coverage. `NOT_APPLICABLE` affects neither the
applicable weight nor coverage. The report exposes both applicable/evaluable
weights and counts so a score can be interpreted with its evidence coverage.

## Offline tests

After `uv sync --locked`, run the exact offline test command from the repository
root:

```bash
uv run python -m unittest discover -s tests -v
```

The tests use Python's standard-library `unittest` framework, do not make
network calls, and use fixtures without executing inspected project code. The
CI workflow also checks the lockfile and compiles `src` and `tests`; no Docker
or CI result is claimed here unless that workflow or a Docker command has
actually been run.

## Docker

The supplied `Dockerfile` uses `python:3.12.7-slim`, copies uv `0.13.1`,
installs the locked project into `/workspace`, runs as the non-root
`repoready` user, and declares `/target` and `/report` as volumes. Its
entrypoint is the actual `repoready` CLI and its default command is equivalent
to:

```text
repoready /target --json /report/report.json
```

Build the image from the repository root:

```bash
docker build -t repoready .
```

On Linux or macOS, place the project to inspect in `target/` and create a
writable `report/` directory. The `--user` override makes the bind-mounted
report directory writable by the invoking user:

```bash
mkdir -p target report
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/target,dst=/target,readonly" \
  --mount "type=bind,src=$PWD/report,dst=/report" \
  repoready
```

The default command writes `report/report.json` on the host. To pass explicit
CLI options, remember that `repoready` is the image entrypoint, so arguments
follow the image name:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/target,dst=/target,readonly" \
  --mount "type=bind,src=$PWD/report,dst=/report" \
  repoready /target --verbose --json /report/verbose.json
```

In Windows PowerShell, the equivalent bind mounts are:

```powershell
New-Item -ItemType Directory -Force target, report | Out-Null
docker run --rm `
  -v "${PWD}\target:/target:ro" `
  -v "${PWD}\report:/report" `
  repoready /target --json /report/report.json
```

The report mount must be writable and the target mount is read-only in these
examples. The image does not install Git, so its `git_repository` check can be
`UNKNOWN` even when the target contains Git metadata. No Docker build or run
result is claimed unless you run those commands.

## Optional Vercel deployment

RepoReady is primarily a local CLI tool. The repository also includes a small
read-only Vercel adapter at `api/index.py` so Vercel's Python runtime has a
supported HTTP entrypoint. A deployment serves the scan report for the
repository bundled into the deployment at `/api`; it does not accept arbitrary
filesystem paths or execute uploaded project code.

The Vercel dashboard can import this GitHub repository without additional
runtime dependencies. After deployment, open the root URL to see the
dashboard:

```text
https://<your-deployment>.vercel.app/
```

The dashboard loads the live report from `/api`. To inspect the machine-readable
response directly, request:

```text
https://<your-deployment>.vercel.app/api
```

This endpoint is supplemental. For scanning a local project directory, use the
CLI or Docker workflow above.

## Troubleshooting

- **`uv` is not found:** install uv using its official installation
  instructions, then verify it is available with `uv --version`.
- **Python version rejected:** use Python `3.12.7` or a later `3.12.x` release;
  the project intentionally excludes Python 3.13 and later.
- **`uv sync --locked` reports a lock mismatch:** restore or regenerate the
  lockfile with the project's pinned dependency changes, then rerun the locked
  sync. Do not silently use an unlocked environment for reproducibility work.
- **Invalid scan target:** `PATH` must exist, be a directory, and not be a
  symlink. Check permissions and pass the intended project directory.
- **JSON export fails:** create the parent directory first; `--json` does not
  create it. Ensure the destination is writable and is not a directory.
- **Docker cannot write the report:** create the host `report/` directory and
  use the Linux/macOS `--user` form, or adjust the bind-mounted directory's
  permissions. Keep `/target` read-only.
- **Many `UNKNOWN` results:** inspect the limitations section and retry with
  `--verbose`. Unreadable files, the scan size limit, and unavailable tools
  intentionally prevent false passes.
- **The score is lower than expected:** review each check's status and weight;
  findings do not make the command fail, and a score does not validate a real
  build.

## Security considerations and known limitations

RepoReady is designed for cautious inspection:

- it reads metadata and bounded text only, with a default 1 MiB file limit;
- it does not follow symlinks and rejects a symlink as the scan root;
- it skips generated/dependency directories such as `.git`, virtual
  environments, `node_modules`, build output, caches, and `vendor`;
- it does not load `.env`, `.env.*`, or private-key-like filenames, including
  common SSH key names; and
- reports avoid source text and redact common secret/token assignment forms.

Redaction and filename matching are best-effort protections, not a guarantee
that arbitrary secrets or unusual formats are safe to publish. Review reports
before sharing them. A scan can still expose project paths, check names,
sanitized evidence references, and file-related limitations.

The checks are static heuristics. RepoReady does not prove dependency
resolution, reproducible builds, Docker success, test success, runtime
compatibility, lockfile correctness, or absence of vulnerabilities. It also
does not inspect files in skipped directories, follow symlinked content, or
load oversized/unreadable files. The absolute-path check is heuristic and the
README/test/environment checks look for simple documented markers rather than
understanding every project convention.

## Contributing

1. Create a branch from the repository's current branch.
2. Keep changes focused and preserve the offline, static-inspection boundary.
3. Synchronize with `uv sync --locked`.
4. Add or update meaningful `unittest` coverage for behavior changes.
5. Run the exact test command:

   ```bash
   uv run python -m unittest discover -s tests -v
   ```

6. For source changes, also run the compile check used by CI:

   ```bash
   uv run python -m compileall -q src tests
   ```

7. Submit a pull request describing the behavior changed and the commands
   actually run. Do not include secrets or generated virtual-environment,
   cache, or report files.

## License

RepoReady is released under the [MIT License](LICENSE).
