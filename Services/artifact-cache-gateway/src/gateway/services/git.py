from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import urljoin, urlsplit


class GitMirrorError(RuntimeError):
    """Raised when a Git mirror plan or repository operation is unsafe."""


_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _canonical_github_repository(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GitMirrorError("only credential-free GitHub HTTP(S) repositories are supported")
    components = [part for part in parsed.path.split("/") if part]
    if len(components) != 2:
        raise GitMirrorError("GitHub repository URL must contain exactly owner/repository")
    owner, repository = components
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not repository or any(
        part in {".", ".."} or not _COMPONENT.fullmatch(part)
        for part in (owner, repository)
    ):
        raise GitMirrorError("GitHub repository contains an unsafe path component")
    return f"{owner}/{repository}"


def compile_git_mirror_plan(report: dict[str, object]) -> dict[str, object]:
    if report.get("kind") != "artifact-cache-external-build-input-report":
        raise GitMirrorError("input is not an external build input report")
    rows = report.get("cache_candidates")
    if not isinstance(rows, list):
        raise GitMirrorError("external report has no cache_candidates array")

    repositories: dict[str, dict[str, object]] = {}
    rejected: list[dict[str, str]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        if raw_row.get("kind") != "git-repository" or raw_row.get("action") != "mirror":
            continue
        raw_url = raw_row.get("url")
        if not isinstance(raw_url, str):
            rejected.append({"url": str(raw_url), "reason": "missing URL"})
            continue
        try:
            repository = _canonical_github_repository(raw_url)
        except GitMirrorError as exc:
            rejected.append({"url": raw_url, "reason": str(exc)})
            continue
        row = repositories.setdefault(
            repository.lower(),
            {
                "repository": repository,
                "upstream_url": f"https://github.com/{repository}.git",
                "occurrences": 0,
                "tasks": 0,
                "references": [],
                "examples": [],
            },
        )
        row["occurrences"] = int(row["occurrences"]) + int(raw_row.get("occurrences", 0))
        row["tasks"] = max(int(row["tasks"]), int(raw_row.get("tasks", 0)))
        reference = raw_row.get("reference")
        if isinstance(reference, str) and reference and reference not in row["references"]:
            row["references"].append(reference)
        for reference in raw_row.get("references", []):
            if (
                isinstance(reference, str)
                and reference
                and reference not in row["references"]
            ):
                row["references"].append(reference)
        for example in raw_row.get("examples", []):
            if isinstance(example, dict) and example not in row["examples"] and len(row["examples"]) < 5:
                row["examples"].append(example)

    entries = sorted(repositories.values(), key=lambda row: str(row["repository"]).lower())
    for row in entries:
        row["references"] = sorted(row["references"]) or ["HEAD"]
    return {
        "schema_version": 1,
        "kind": "artifact-cache-git-mirror-plan",
        "dataset": report.get("dataset"),
        "route_prefix": "/v1/git/github/",
        "repositories": entries,
        "rejected": rejected,
        "summary": {
            "repositories": len(entries),
            "rejected": len(rejected),
        },
    }


def load_git_mirror_plan(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitMirrorError(f"cannot read Git mirror plan: {exc}") from exc
    if not isinstance(document, dict) or document.get("kind") != "artifact-cache-git-mirror-plan":
        raise GitMirrorError("invalid Git mirror plan")
    repositories = document.get("repositories")
    if not isinstance(repositories, list):
        raise GitMirrorError("Git mirror plan has no repositories array")
    return document


@dataclass(frozen=True)
class GitMirrorResult:
    repository: str
    state: str
    duration_seconds: float
    error: str | None = None


class GitMirrorStore:
    """On-demand public GitHub mirror backed by a persistent filesystem."""

    def __init__(
        self,
        *,
        plan: dict[str, object] | None,
        root: Path,
        proxy_url: str | None = None,
        timeout: float = 1800.0,
        max_concurrent_fills: int = 4,
        metrics_callback: Callable[[str], None] | None = None,
    ):
        if not root.is_absolute():
            raise GitMirrorError("Git mirror root must be an absolute persistent path")
        if timeout <= 0:
            raise GitMirrorError("Git mirror timeout must be positive")
        if not 1 <= max_concurrent_fills <= 32:
            raise GitMirrorError("Git mirror fill concurrency must be between 1 and 32")
        raw_repositories = (plan or {}).get("repositories", [])
        if not isinstance(raw_repositories, list):
            raise GitMirrorError("Git mirror plan has no repositories array")
        repositories: dict[str, str] = {}
        references: dict[str, set[str]] = {}
        for raw_row in raw_repositories:
            if not isinstance(raw_row, dict):
                raise GitMirrorError("Git mirror plan repository row must be an object")
            raw_url = raw_row.get("upstream_url")
            raw_repository = raw_row.get("repository")
            if not isinstance(raw_url, str) or not isinstance(raw_repository, str):
                raise GitMirrorError("Git mirror plan repository row is incomplete")
            repository = _canonical_github_repository(raw_url)
            if repository != raw_repository:
                raise GitMirrorError("Git mirror plan repository identity does not match URL")
            repositories[repository.lower()] = repository
            raw_references = raw_row.get("references", [])
            if not isinstance(raw_references, list):
                raise GitMirrorError("Git mirror plan references must be an array")
            repository_references = references.setdefault(repository.lower(), set())
            repository_references.update(
                reference
                for reference in raw_references
                if isinstance(reference, str) and reference
            )
            if not repository_references:
                repository_references.add("HEAD")
        self.root = root
        self.proxy_url = proxy_url
        self.timeout = timeout
        self._fill_slots = threading.BoundedSemaphore(max_concurrent_fills)
        self._repositories = repositories
        self._references = references
        self._planned_repositories = set(repositories)
        self._derived_repositories: set[str] = set()
        self._on_demand_repositories: set[str] = set()
        self._repositories_guard = threading.Lock()
        self._scanned_references: set[str] = set()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._metrics = {"hit": 0, "fill": 0, "refresh": 0, "error": 0}
        self._metrics_guard = threading.Lock()
        self._metrics_callback = metrics_callback
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".locks").mkdir(exist_ok=True)
        (self.root / ".tmp").mkdir(exist_ok=True)
        self._load_derived_allowlist()
        self._load_existing_repositories()

    @property
    def configured_count(self) -> int:
        """Return the number of repositories known from plans, discovery, or requests."""
        with self._repositories_guard:
            return len(self._repositories)

    def canonical_repository(self, raw_repository: str) -> str:
        repository = _canonical_github_repository(
            f"https://github.com/{raw_repository}"
        )
        key = repository.lower()
        with self._repositories_guard:
            canonical = self._repositories.get(key)
            if canonical is None:
                self._repositories[key] = repository
                canonical = repository
            if (
                key not in self._planned_repositories
                and key not in self._derived_repositories
            ):
                self._on_demand_repositories.add(key)
        return canonical

    def repository_path(self, repository: str) -> Path:
        canonical = self.canonical_repository(repository)
        owner, name = canonical.split("/", 1)
        return self.root / owner / f"{name}.git"

    def _thread_lock(self, repository: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(repository.lower(), threading.Lock())

    def _git_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
        ):
            environment.pop(key, None)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if self.proxy_url:
            environment["HTTP_PROXY"] = self.proxy_url
            environment["HTTPS_PROXY"] = self.proxy_url
        return environment

    def _valid_bare_repository(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        result = subprocess.run(
            ["git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
            env=self._git_environment(),
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def ensure(self, raw_repository: str, *, refresh: bool = False) -> GitMirrorResult:
        repository = self.canonical_repository(raw_repository)
        started = time.monotonic()
        lock_name = hashlib.sha256(repository.lower().encode()).hexdigest() + ".lock"
        try:
            with self._thread_lock(repository):
                with (self.root / ".locks" / lock_name).open("a+b") as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    result = self._ensure_locked(repository, refresh=refresh)
                    self._discover_submodules(repository)
        except (OSError, subprocess.SubprocessError, GitMirrorError) as exc:
            self._count("error")
            return GitMirrorResult(repository, "ERROR", time.monotonic() - started, str(exc))
        self._count(result)
        return GitMirrorResult(repository, result.upper(), time.monotonic() - started)

    def _ensure_locked(self, repository: str, *, refresh: bool) -> str:
        path = self.repository_path(repository)
        temporary: Path | None = None
        if self._valid_bare_repository(path):
            if not refresh:
                command = None
                state = "hit"
            else:
                command = [
                    "git", "--git-dir", str(path), "fetch", "--all", "--prune", "--quiet"
                ]
                state = "refresh"
        else:
            if path.exists():
                raise GitMirrorError("mirror path exists but is not a valid bare repository")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix="mirror-", dir=self.root / ".tmp"))
            checkout = temporary / "repository.git"
            command = [
                "git", "clone", "--mirror", "--quiet",
                f"https://github.com/{repository}.git",
                str(checkout),
            ]
            state = "fill"
        if command is not None:
            try:
                with self._fill_slots:
                    completed = subprocess.run(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=self.timeout,
                        check=False,
                        env=self._git_environment(),
                    )
            except Exception:
                if temporary is not None:
                    shutil.rmtree(temporary, ignore_errors=True)
                raise
            if completed.returncode:
                if temporary is not None:
                    shutil.rmtree(temporary, ignore_errors=True)
                detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "git exited unsuccessfully"
                if self.proxy_url:
                    detail = detail.replace(self.proxy_url, "[configured proxy]")
                detail = re.sub(
                    r"(?i)(https?://)[^/@\s]+:[^/@\s]+@",
                    r"\1[credentials]@",
                    detail,
                )
                raise GitMirrorError(f"Git {state} failed: {detail[:500]}")
        if state == "fill":
            assert temporary is not None
            try:
                if not self._valid_bare_repository(checkout):
                    raise GitMirrorError("Git clone did not produce a valid bare repository")
                os.replace(checkout, path)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        self._ensure_pinned_commits(repository, path)
        return state

    def _ensure_pinned_commits(self, repository: str, path: Path) -> None:
        """Fetch recorded submodule commits that a normal mirror may not advertise."""
        with self._repositories_guard:
            commits = sorted(
                reference
                for reference in self._references.get(repository.lower(), set())
                if re.fullmatch(r"[0-9a-fA-F]{40}", reference)
            )
        for commit in commits:
            exists = subprocess.run(
                ["git", "--git-dir", str(path), "cat-file", "-e", f"{commit}^{{commit}}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                env=self._git_environment(),
            )
            if exists.returncode == 0:
                continue
            destination = f"refs/artifact-cache/pins/{commit.lower()}"
            with self._fill_slots:
                fetched = subprocess.run(
                    [
                        "git", "--git-dir", str(path), "fetch", "--quiet", "--no-tags",
                        "origin", f"{commit}:{destination}",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                    env=self._git_environment(),
                )
            if fetched.returncode:
                detail = fetched.stderr.strip().splitlines()[-1] if fetched.stderr.strip() else "git exited unsuccessfully"
                if self.proxy_url:
                    detail = detail.replace(self.proxy_url, "[configured proxy]")
                detail = re.sub(
                    r"(?i)(https?://)[^/@\s]+:[^/@\s]+@",
                    r"\1[credentials]@",
                    detail,
                )
                raise GitMirrorError(
                    f"Git pin fetch failed for {repository}@{commit}: {detail[:500]}"
                )

    @property
    def _derived_path(self) -> Path:
        return self.root / ".derived-allowlist.json"

    def _load_existing_repositories(self) -> None:
        """Index already-published mirrors so casing and status survive restarts."""
        for owner_path in self.root.iterdir():
            if not owner_path.is_dir() or not _COMPONENT.fullmatch(owner_path.name):
                continue
            for repository_path in owner_path.iterdir():
                if not repository_path.is_dir() or not repository_path.name.endswith(".git"):
                    continue
                repository_name = repository_path.name[:-4]
                if not _COMPONENT.fullmatch(repository_name):
                    continue
                repository = f"{owner_path.name}/{repository_name}"
                with self._repositories_guard:
                    self._repositories.setdefault(repository.lower(), repository)

    def _load_derived_allowlist(self) -> None:
        if not self._derived_path.is_file():
            return
        try:
            document = json.loads(self._derived_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitMirrorError(f"cannot read derived Git allowlist: {exc}") from exc
        if not isinstance(document, dict):
            raise GitMirrorError("derived Git allowlist must be an object")
        rows = document.get("repositories", [])
        scanned = document.get("scanned_references", [])
        if not isinstance(rows, list) or not isinstance(scanned, list):
            raise GitMirrorError("derived Git allowlist has an invalid schema")
        for row in rows:
            if not isinstance(row, dict):
                raise GitMirrorError("derived Git repository row must be an object")
            repository = row.get("repository")
            raw_references = row.get("references", [])
            if not isinstance(repository, str) or not isinstance(raw_references, list):
                raise GitMirrorError("derived Git repository row is incomplete")
            canonical = _canonical_github_repository(
                f"https://github.com/{repository}.git"
            )
            key = canonical.lower()
            self._repositories[key] = canonical
            if key not in self._planned_repositories:
                self._derived_repositories.add(key)
            self._references.setdefault(canonical.lower(), set()).update(
                reference
                for reference in raw_references
                if isinstance(reference, str) and reference
            )
        self._scanned_references.update(
            value for value in scanned if isinstance(value, str)
        )

    def _persist_derived_allowlist(self) -> None:
        lock_path = self.root / ".locks" / "derived-allowlist.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if self._derived_path.is_file():
                try:
                    existing = json.loads(
                        self._derived_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GitMirrorError(
                        f"cannot merge derived Git allowlist: {exc}"
                    ) from exc
                if not isinstance(existing, dict):
                    raise GitMirrorError("derived Git allowlist must be an object")
                with self._repositories_guard:
                    for row in existing.get("repositories", []):
                        if not isinstance(row, dict):
                            continue
                        repository = row.get("repository")
                        raw_references = row.get("references", [])
                        if not isinstance(repository, str) or not isinstance(raw_references, list):
                            continue
                        canonical = _canonical_github_repository(
                            f"https://github.com/{repository}.git"
                        )
                        key = canonical.lower()
                        self._repositories[key] = canonical
                        if key not in self._planned_repositories:
                            self._derived_repositories.add(key)
                        self._on_demand_repositories.discard(key)
                        self._references.setdefault(key, set()).update(
                            reference
                            for reference in raw_references
                            if isinstance(reference, str) and reference
                        )
                    self._scanned_references.update(
                        value
                        for value in existing.get("scanned_references", [])
                        if isinstance(value, str)
                    )
            rows = []
            with self._repositories_guard:
                for key in sorted(self._derived_repositories):
                    if key in self._planned_repositories:
                        continue
                    repository = self._repositories[key]
                    rows.append(
                        {
                            "repository": repository,
                            "references": sorted(self._references.get(key, set())),
                        }
                    )
                document = {
                    "schema_version": 1,
                    "kind": "artifact-cache-derived-git-allowlist",
                    "repositories": rows,
                    "scanned_references": sorted(self._scanned_references),
                }
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="derived-", suffix=".json", dir=self.root / ".tmp"
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                temporary.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self._derived_path)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _submodule_repository(parent: str, raw_url: str) -> str | None:
        value = raw_url.strip()
        if value.startswith("git@github.com:"):
            value = "https://github.com/" + value.split(":", 1)[1]
        elif value.startswith(("../", "./")):
            value = urljoin(f"https://github.com/{parent}.git/", value)
        try:
            return _canonical_github_repository(value)
        except GitMirrorError:
            return None

    def _discover_submodules(self, repository: str) -> None:
        key = repository.lower()
        with self._repositories_guard:
            references = sorted(self._references.get(key, set()))
            pending = [
                reference
                for reference in references
                if f"{key}@{reference}" not in self._scanned_references
            ]
        if not pending:
            return
        path = self.repository_path(repository)
        changed = False
        for reference in pending:
            command = [
                "git", "--git-dir", str(path), "config",
                "--blob", f"{reference}:.gitmodules",
                "--get-regexp", r"^submodule\..*\.(path|url)$",
            ]
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
                check=False,
                env=self._git_environment(),
            )
            modules: dict[str, dict[str, str]] = {}
            if completed.returncode == 0:
                for line in completed.stdout.splitlines():
                    field, separator, value = line.partition(" ")
                    match = re.fullmatch(r"submodule\.(.*)\.(path|url)", field)
                    if separator and match:
                        modules.setdefault(match.group(1), {})[match.group(2)] = value
            elif completed.returncode not in {1, 128}:
                raise GitMirrorError("cannot inspect reviewed .gitmodules")
            for module in modules.values():
                module_path = module.get("path")
                module_url = module.get("url")
                if not module_path or not module_url:
                    continue
                child = self._submodule_repository(repository, module_url)
                if child is None:
                    continue
                tree = subprocess.run(
                    ["git", "--git-dir", str(path), "ls-tree", reference, "--", module_path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=30,
                    check=False,
                    env=self._git_environment(),
                )
                match = re.match(r"160000 commit ([0-9a-f]{40})\t", tree.stdout)
                if tree.returncode or match is None:
                    continue
                with self._repositories_guard:
                    child_key = child.lower()
                    if child_key not in self._repositories:
                        self._repositories[child_key] = child
                        changed = True
                    if child_key not in self._derived_repositories:
                        self._derived_repositories.add(child_key)
                        self._on_demand_repositories.discard(child_key)
                        changed = True
                    if match.group(1) not in self._references.setdefault(child_key, set()):
                        self._references[child_key].add(match.group(1))
                        changed = True
            with self._repositories_guard:
                self._scanned_references.add(f"{key}@{reference}")
                changed = True
        if changed:
            self._persist_derived_allowlist()

    def _count(self, state: str) -> None:
        with self._metrics_guard:
            self._metrics[state] += 1
        if self._metrics_callback is not None:
            self._metrics_callback(state)

    def status(self) -> dict[str, object]:
        with self._repositories_guard:
            repositories = dict(self._repositories)
            planned = set(self._planned_repositories)
            derived = set(self._derived_repositories) - planned
            on_demand = set(self._on_demand_repositories) - planned - derived
        ready_keys = {
            key
            for key, repository in repositories.items()
            if (
                self.root
                / repository.split("/", 1)[0]
                / f"{repository.split('/', 1)[1]}.git"
            ).is_dir()
        }
        planned_ready = len(ready_keys & planned)
        with self._metrics_guard:
            metrics = dict(self._metrics)
        return {
            "enabled": True,
            "route_prefix": "/v1/git/github/",
            "known_repositories": len(repositories),
            # Kept for status API compatibility; repositories are no longer an allowlist.
            "configured_repositories": len(repositories),
            "planned_repositories": len(planned),
            "planned_ready_repositories": planned_ready,
            "planned_missing_repositories": len(planned) - planned_ready,
            "derived_submodule_repositories": len(derived),
            "on_demand_repositories": len(on_demand),
            "ready_repositories": len(ready_keys),
            "storage": "persistent-filesystem",
            "root": str(self.root),
            "requests": metrics,
            "upstream_proxy": "configured" if self.proxy_url else "disabled",
        }


def warm_git_mirrors(
    store: GitMirrorStore,
    *,
    concurrency: int = 4,
    refresh: bool = False,
    repositories: list[str] | None = None,
) -> dict[str, object]:
    if not 1 <= concurrency <= 32:
        raise GitMirrorError("Git mirror concurrency must be between 1 and 32")
    results: list[GitMirrorResult] = []
    completed_repositories: set[str] = set()
    selected = (
        [store.canonical_repository(repository) for repository in repositories]
        if repositories
        else None
    )
    while True:
        with store._repositories_guard:
            repositories = [
                repository
                for repository in (
                    selected
                    if selected is not None
                    else (
                        store._repositories[key]
                        for key in (
                            store._planned_repositories
                            | store._derived_repositories
                        )
                    )
                )
                if repository.lower() not in completed_repositories
            ]
        if not repositories:
            break
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(store.ensure, repository, refresh=refresh): repository
                for repository in repositories
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                completed_repositories.add(result.repository.lower())
        # A targeted warm prepares only the requested top-level repositories.
        # Derived submodules remain recorded and are filled on demand.
        if selected is not None:
            break
    results.sort(key=lambda row: row.repository.lower())
    return {
        "schema_version": 1,
        "kind": "artifact-cache-git-mirror-result",
        "summary": {
            "repositories": len(results),
            "hit": sum(row.state == "HIT" for row in results),
            "fill": sum(row.state == "FILL" for row in results),
            "refresh": sum(row.state == "REFRESH" for row in results),
            "error": sum(row.state == "ERROR" for row in results),
        },
        "results": [row.__dict__ for row in results],
    }


def read_cgi_headers(stream: BinaryIO, *, limit: int = 65536) -> tuple[int, list[tuple[str, str]]]:
    raw = bytearray()
    while len(raw) < limit:
        byte = stream.read(1)
        if not byte:
            break
        raw.extend(byte)
        if raw.endswith(b"\r\n\r\n") or raw.endswith(b"\n\n"):
            break
    else:
        raise GitMirrorError("git http-backend returned oversized headers")
    separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    if separator not in raw:
        raise GitMirrorError("git http-backend returned incomplete headers")
    head = bytes(raw).split(separator, 1)[0].decode("iso-8859-1")
    status = 200
    headers: list[tuple[str, str]] = []
    for line in head.splitlines():
        name, marker, value = line.partition(":")
        if not marker:
            raise GitMirrorError("git http-backend returned an invalid header")
        if name.lower() == "status":
            try:
                status = int(value.strip().split(" ", 1)[0])
            except ValueError as exc:
                raise GitMirrorError("git http-backend returned an invalid status") from exc
        elif name.lower() not in {"connection", "transfer-encoding", "content-length"}:
            headers.append((name.strip(), value.strip()))
    return status, headers
