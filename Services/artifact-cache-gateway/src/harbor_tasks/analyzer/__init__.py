"""Harbor dataset analyzer: the pipeline entry point `analyze()` and public-name re-exports.

Static analysis only: it never builds images or runs install commands. The pipeline
is layered into the models/shell/dockerfile/apt/external/packages/report/cli
submodules, and this module owns the pipeline orchestration.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence

from .apt import (
    apt_dependencies,
    apt_repository_declarations,
)
from .dockerfile import (
    discover_tasks,
    docker_instructions,
    docker_scan_sources,
    from_image,
    from_stage,
    read_text,
    shell_contexts,
)
from .external import (
    _git_checkout_reference,
    _git_clone_destination,
    _shell_path,
    external_build_input_issues,
    external_build_inputs,
    sanitize_public_url,
)
from .models import (
    NODE_REGISTRY_MANAGERS,
    Aggregate,
    AptDependency,
    BuildContext,
    ExternalBuildInput,
    ExternalBuildInputIssue,
    Install,
    ScanSource,
    _resolution_environment_id,
)
from .packages import (
    argument_specs,
    generated_manifests,
    identify_install,
    normalize_package,
    npm_resolution_options,
    package_resolution_options,
    parse_requirements,
    pip_python_version,
    pip_resolution_options,
    requirement_candidates,
    select_generated_manifest,
    shell_files,
)
from .report import markdown, write_outputs
from .shell import (
    command_start,
    logical_shell_lines,
    shell_commands,
    strip_heredocs,
    unquoted_heredoc_matches,
)


def analyze(dataset: Path, shell_scope: str) -> dict:
    images, packages, apt_artifacts = Aggregate(), Aggregate(), Aggregate()
    external_artifacts = Aggregate()
    external_issues = Aggregate()
    apt_examples: dict[AptDependency, list[dict[str, str]]] = defaultdict(list)
    external_examples: dict[ExternalBuildInput, list[dict[str, str]]] = defaultdict(
        list
    )
    external_issue_examples: dict[
        ExternalBuildInputIssue, list[dict[str, str]]
    ] = defaultdict(list)
    git_references: dict[str, set[str]] = defaultdict(set)
    apt_related_packages: dict[AptDependency, set[str]] = defaultdict(set)
    package_images: dict[tuple[str, str], set[str]] = defaultdict(set)
    package_contexts: dict[
        tuple[str, str], dict[tuple[str, str | None, str | None], dict[str, object]]
    ] = defaultdict(dict)
    package_repositories: dict[
        tuple[str, str], dict[tuple[object, ...], dict[str, object]]
    ] = defaultdict(dict)
    build_context_rows: dict[str, dict[str, object]] = {}
    environment_rows: dict[str, dict[str, object]] = {}
    managers, unresolved = Counter(), []
    task_count = dockerfile_count = shell_count = apt_source_file_count = 0
    for task_root in discover_tasks(dataset):
        task_count += 1
        task = task_root.relative_to(dataset).as_posix()
        environment = task_root / "environment"
        dockerfiles = sorted(p for p in environment.rglob("*") if p.is_file() and p.name.lower().startswith("dockerfile"))
        docker_texts = [read_text(path) for path in dockerfiles]
        scripts = shell_files(environment, docker_texts, shell_scope)
        apt_source_files = sorted(
            path
            for path in environment.rglob("*")
            if path.is_file() and path.suffix.lower() in {".list", ".sources"}
        )
        dockerfile_count += len(dockerfiles)
        shell_count += len(scripts)
        apt_source_file_count += len(apt_source_files)
        sources: list[ScanSource] = []
        apt_scan_sources: list[tuple[Path, str]] = []
        task_contexts: list[BuildContext] = []
        references: list[tuple[str, BuildContext]] = []
        for dockerfile, text in zip(dockerfiles, docker_texts):
            contexts, run_sources, docker_references = docker_scan_sources(
                task, task_root, dockerfile, text
            )
            task_contexts.extend(contexts)
            sources.extend(run_sources)
            references.extend(docker_references)
            for context in contexts:
                build_context_rows[context.id] = context.to_dict()
                images.add(context.base_image, task, context.base_image)
            apt_scan_sources.extend((item.path, item.text) for item in run_sources)
            for instruction, body in docker_instructions(text):
                if instruction in {"ADD", "COPY"}:
                    apt_scan_sources.append((dockerfile, body))
        script_owners = shell_contexts(
            environment, scripts, references, task_contexts
        )
        sources.extend(
            ScanSource(path, read_text(path), script_owners[path])
            for path in scripts
        )
        apt_scan_sources.extend((item.path, item.text) for item in sources if item.path in scripts)
        apt_scan_sources.extend((path, read_text(path)) for path in apt_source_files)
        task_apt_packages: set[str] = set()
        task_apt_artifacts: list[tuple[AptDependency, str]] = []
        for source, text in apt_scan_sources:
            relative_source = str(source.relative_to(task_root))
            for dependency in apt_dependencies(text):
                task_apt_artifacts.append((dependency, relative_source))
        repositories_by_context: dict[str, list[dict[str, object]]] = defaultdict(list)

        def record_package(
            manager: str,
            package: str,
            spec: str,
            python_version: str | None,
            context: BuildContext | None,
            active_repositories: Sequence[dict[str, object]],
            relative_source: str,
            resolution_options: dict[str, object] | None = None,
        ) -> None:
            key = manager, package
            packages.add(key, task, spec)
            image_values = [context.base_image] if context else []
            package_images[key].update(image_values)
            environment_id = (
                _resolution_environment_id(
                    manager,
                    context,
                    python_version=python_version,
                    repositories=active_repositories if manager == "apt" else (),
                    resolution_options=resolution_options,
                )
                if context
                else None
            )
            context_identity = (spec, python_version, environment_id)
            stored = package_contexts[key].setdefault(
                context_identity,
                {
                    "requirement": spec,
                    "images": image_values,
                    **({"python_version": python_version} if python_version else {}),
                    **({"environment_id": environment_id} if environment_id else {}),
                    **(
                        {"resolution_options": resolution_options}
                        if resolution_options
                        else {}
                    ),
                    "build_contexts": [],
                },
            )
            if context and context.id not in stored["build_contexts"]:
                stored["build_contexts"].append(context.id)
            if environment_id and context:
                environment = environment_rows.setdefault(
                    environment_id,
                    {
                        "id": environment_id,
                        "manager": manager,
                        "base_image": (
                            None
                            if manager in NODE_REGISTRY_MANAGERS
                            else context.base_image
                        ),
                        "base_images": [],
                        "platform": (
                            None
                            if manager in NODE_REGISTRY_MANAGERS
                            else context.platform
                        ),
                        **({"python_version": python_version} if python_version else {}),
                        "repositories": list(active_repositories)
                        if manager == "apt"
                        else [],
                        "resolution_options": resolution_options or {},
                        "build_contexts": [],
                        "packages": {},
                    },
                )
                if context.base_image not in environment["base_images"]:
                    environment["base_images"].append(context.base_image)
                if context.id not in environment["build_contexts"]:
                    environment["build_contexts"].append(context.id)
                package_entry = environment["packages"].setdefault(
                    package, {"package": package, "requirements": [], "build_contexts": []}
                )
                if spec not in package_entry["requirements"]:
                    package_entry["requirements"].append(spec)
                if context.id not in package_entry["build_contexts"]:
                    package_entry["build_contexts"].append(context.id)
            if manager == "apt" and context:
                task_apt_packages.add(package)
                for repository in active_repositories:
                    repository_context = {
                        **repository,
                        "task": task,
                        "source": relative_source,
                        "images": image_values,
                        "environment_id": environment_id,
                        "build_contexts": [context.id],
                    }
                    raw_components = repository_context.get("components", [])
                    identity = (
                        repository_context["upstream_url"],
                        repository_context["suite"],
                        tuple(raw_components) if isinstance(raw_components, list) else (),
                        task,
                        relative_source,
                        environment_id,
                    )
                    package_repositories[key][identity] = repository_context

        for scan_source in sources:
            source, text = scan_source.path, scan_source.text
            generated_requirements, generated_package_json = generated_manifests(text)
            relative_source = str(source.relative_to(task_root))
            scan_contexts: tuple[BuildContext | None, ...] = (
                scan_source.contexts if scan_source.contexts else (None,)
            )
            for scan_context in scan_contexts:
                current_directory: str | None = None
                git_clones_by_directory: dict[str, str] = {}
                active_repositories = (
                    repositories_by_context[scan_context.id]
                    if scan_context
                    else []
                )
                for line in logical_shell_lines(text):
                    for tokens in shell_commands(line):
                        command_text = " ".join(tokens)
                        discovered_inputs = list(external_build_inputs(tokens))
                        for dependency in discovered_inputs:
                            external_artifacts.add(dependency, task, dependency.url)
                            example = {
                                "task": task,
                                "source": relative_source,
                                **(
                                    {"build_context": scan_context.id}
                                    if scan_context
                                    else {}
                                ),
                            }
                            if (
                                example not in external_examples[dependency]
                                and len(external_examples[dependency]) < 5
                            ):
                                external_examples[dependency].append(example)
                            if dependency.kind == "git-repository":
                                destination = _git_clone_destination(tokens)
                                if destination:
                                    git_clones_by_directory[
                                        _shell_path(current_directory, destination)
                                    ] = dependency.url
                        for issue in external_build_input_issues(
                            tokens, discovered_inputs
                        ):
                            external_issues.add(issue, task, issue.reason)
                            example = {
                                "task": task,
                                "source": relative_source,
                                **(
                                    {"build_context": scan_context.id}
                                    if scan_context
                                    else {}
                                ),
                            }
                            if (
                                example not in external_issue_examples[issue]
                                and len(external_issue_examples[issue]) < 5
                            ):
                                external_issue_examples[issue].append(example)
                        for repository in apt_repository_declarations(command_text):
                            if repository not in active_repositories:
                                active_repositories.append(repository)
                        start = command_start(tokens)
                        if start < len(tokens) and tokens[start] == "cd" and start + 1 < len(tokens):
                            current_directory = _shell_path(
                                current_directory, tokens[start + 1]
                            )
                            continue
                        checkout_reference = _git_checkout_reference(tokens)
                        if checkout_reference and current_directory:
                            clone_url = git_clones_by_directory.get(current_directory)
                            if clone_url:
                                git_references[clone_url].add(checkout_reference)
                        install = identify_install(tokens)
                        if install is None:
                            continue
                        managers[install.manager] += 1
                        specs, requirement_files = argument_specs(install)
                        python_version = pip_python_version(install)
                        resolution_options = package_resolution_options(install)
                        if install.manager == "npm" and not specs:
                            manifest_specs = select_generated_manifest(
                                generated_package_json, cwd=current_directory
                            )
                            if manifest_specs:
                                specs.extend(manifest_specs)
                        for spec in specs:
                            package = normalize_package(install.manager, spec)
                            if package:
                                record_package(
                                    install.manager,
                                    package,
                                    spec,
                                    python_version,
                                    scan_context,
                                    active_repositories,
                                    relative_source,
                                    resolution_options,
                                )
                        for requested in requirement_files:
                            generated_specs = select_generated_manifest(
                                generated_requirements, requested=requested
                            )
                            if generated_specs is None:
                                candidates = requirement_candidates(environment, source, requested)
                                if not candidates:
                                    unresolved.append(
                                        {
                                            "task": task,
                                            "source": relative_source,
                                            "requirement": requested,
                                        }
                                    )
                                    continue
                                generated_specs = list(parse_requirements(candidates[0]))
                            for spec in generated_specs:
                                package = normalize_package("pip", spec)
                                if package:
                                    record_package(
                                        "pip",
                                        package,
                                        spec,
                                        python_version,
                                        scan_context,
                                        (),
                                        relative_source,
                                        resolution_options,
                                    )
        for dependency, source_name in task_apt_artifacts:
            apt_artifacts.add(dependency, task, dependency.url)
            apt_related_packages[dependency].update(task_apt_packages)
            example = {"task": task, "source": source_name}
            if example not in apt_examples[dependency] and len(apt_examples[dependency]) < 5:
                apt_examples[dependency].append(example)
    image_rows = [{"image": key, "occurrences": images.occurrences[key], "tasks": len(images.tasks[key])} for key in images.occurrences]
    image_rows.sort(key=lambda row: (-row["occurrences"], row["image"]))
    package_rows = []
    for manager, package in packages.occurrences:
        key = manager, package
        package_rows.append({"manager": manager, "package": package, "occurrences": packages.occurrences[key],
                             "tasks": len(packages.tasks[key]), "specs": dict(packages.specs[key].most_common()),
                             "images": sorted(package_images[key]),
                             "contexts": list(package_contexts[key].values()),
                             "repository_contexts": list(package_repositories[key].values())})
    package_rows.sort(key=lambda row: (-row["occurrences"], row["manager"], row["package"]))
    apt_rows = []
    action_order = {"cache": 0, "validate": 1, "rewrite": 2, "none": 3}
    for dependency in apt_artifacts.occurrences:
        apt_rows.append(
            {
                "url": dependency.url,
                "host": dependency.host,
                "kind": dependency.kind,
                "coverage": dependency.coverage,
                "action": dependency.action,
                "cache_mode": (
                    "apt-repository-proxy"
                    if dependency.action == "cache" and dependency.kind == "repository"
                    else "http-object"
                    if dependency.action == "cache"
                    else None
                ),
                "reason": dependency.reason,
                "query_present": dependency.query_present,
                "occurrences": apt_artifacts.occurrences[dependency],
                "tasks": len(apt_artifacts.tasks[dependency]),
                "packages_in_same_tasks": sorted(apt_related_packages[dependency]),
                "examples": apt_examples[dependency],
            }
        )
    apt_rows.sort(
        key=lambda row: (
            action_order.get(str(row["action"]), 99),
            -int(row["occurrences"]),
            str(row["url"]),
        )
    )
    apt_cache_candidates = [row for row in apt_rows if row["action"] == "cache"]
    apt_needs_validation = [row for row in apt_rows if row["action"] == "validate"]
    external_rows = []
    for dependency in external_artifacts.occurrences:
        external_rows.append(
            {
                "url": dependency.url,
                "host": dependency.host,
                "kind": dependency.kind,
                "tool": dependency.tool,
                "action": dependency.action,
                "cache_mode": dependency.cache_mode,
                "refresh_policy": dependency.refresh_policy,
                "reason": dependency.reason,
                "query_present": dependency.query_present,
                "credentials_present": dependency.credentials_present,
                "reference": dependency.reference,
                "references": sorted(
                    git_references.get(dependency.url, set())
                ),
                "occurrences": external_artifacts.occurrences[dependency],
                "tasks": len(external_artifacts.tasks[dependency]),
                "examples": external_examples[dependency],
            }
        )
    external_rows.sort(
        key=lambda row: (
            str(row["kind"]),
            -int(row["occurrences"]),
            str(row["url"]),
            str(row.get("reference") or ""),
        )
    )
    external_cache_candidates = [
        row for row in external_rows if row["action"] in {"cache", "mirror"}
    ]
    external_needs_review = [
        row for row in external_rows if row["action"] == "review"
    ]
    external_unresolved = [
        {
            "kind": issue.kind,
            "tool": issue.tool,
            "reason": issue.reason,
            "occurrences": external_issues.occurrences[issue],
            "tasks": len(external_issues.tasks[issue]),
            "examples": external_issue_examples[issue],
        }
        for issue in sorted(
            external_issues.occurrences,
            key=lambda item: (item.kind, item.tool, item.reason),
        )
    ]
    apt_package_rows = [row for row in package_rows if row["manager"] == "apt"]
    environments = []
    for environment in environment_rows.values():
        row = dict(environment)
        raw_packages = row.pop("packages")
        row["packages"] = sorted(raw_packages.values(), key=lambda item: item["package"])
        row["build_contexts"] = sorted(row["build_contexts"])
        environments.append(row)
    environments.sort(key=lambda row: str(row["id"]))
    return {"schema_version": 2, "dataset": str(dataset), "summary": {"tasks": task_count, "dockerfiles": dockerfile_count,
            "scanned_shell_files": shell_count, "image_occurrences": sum(images.occurrences.values()),
            "unique_images": len(image_rows), "package_occurrences": sum(packages.occurrences.values()),
            "unique_manager_packages": len(package_rows), "install_invocations_by_manager": dict(managers.most_common()),
            "build_contexts": len(build_context_rows),
            "resolution_environments": len(environments),
            "unresolved_requirement_files": len(unresolved), "shell_scope": shell_scope,
            "scanned_apt_source_files": apt_source_file_count,
            "apt_dependency_urls": len(apt_rows),
            "apt_cache_candidate_urls": len(apt_cache_candidates),
            "apt_needs_validation_urls": len(apt_needs_validation),
            "external_build_input_urls": len(
                {
                    (row["kind"], row["url"], row.get("reference"))
                    for row in external_rows
                }
            ),
            "external_cache_candidate_urls": len(
                {
                    (row["kind"], row["url"], row.get("reference"))
                    for row in external_cache_candidates
                }
            ),
            "external_download_cache_candidate_urls": len(
                {
                    row["url"]
                    for row in external_rows
                    if row["kind"] == "http-download" and row["action"] == "cache"
                }
            ),
            "external_git_mirror_candidate_urls": len(
                {
                    (row["url"], row.get("reference"))
                    for row in external_rows
                    if row["kind"] == "git-repository" and row["action"] == "mirror"
                }
            ),
            "external_needs_review_urls": len(
                {
                    (row["kind"], row["url"], row.get("reference"))
                    for row in external_needs_review
                }
            ),
            "external_unresolved_commands": sum(
                int(row["occurrences"]) for row in external_unresolved
            ),
            "apt_packages_needing_resolution": len(apt_package_rows)},
            "build_contexts": sorted(build_context_rows.values(), key=lambda row: str(row["id"])),
            "environments": environments,
            "images": image_rows, "packages": package_rows, "unresolved_requirements": unresolved,
            "apt": {
                "classification": {
                    "scope": "static-build-inputs",
                    "domestic_distribution_mirrors_cover": "Ubuntu/Debian distribution repositories only",
                    "cache_required": "third-party repositories, signing keys, and direct .deb artifacts",
                    "package_resolution": "requires an image/codename-aware runtime validation phase",
                },
                "packages": apt_package_rows,
                "dependencies": apt_rows,
                "cache_candidates": apt_cache_candidates,
                "needs_validation": apt_needs_validation,
            },
            "external_inputs": {
                "classification": {
                    "scope": "explicit-build-commands",
                    "http_download": "path-allowlisted static object source with manual refresh",
                    "git_repository": "Git-aware mirror; ordinary file-object caching is insufficient",
                    "security": "credentials and query strings are excluded from report URLs",
                },
                "dependencies": external_rows,
                "cache_candidates": external_cache_candidates,
                "needs_review": external_needs_review,
                "unresolved": external_unresolved,
            }}


from .cli import main, parse_args

__all__ = [
    "Aggregate",
    "AptDependency",
    "BuildContext",
    "ExternalBuildInput",
    "ExternalBuildInputIssue",
    "Install",
    "ScanSource",
    "analyze",
    "apt_dependencies",
    "apt_repository_declarations",
    "argument_specs",
    "command_start",
    "discover_tasks",
    "docker_instructions",
    "docker_scan_sources",
    "external_build_input_issues",
    "external_build_inputs",
    "from_image",
    "from_stage",
    "generated_manifests",
    "identify_install",
    "logical_shell_lines",
    "main",
    "markdown",
    "normalize_package",
    "npm_resolution_options",
    "package_resolution_options",
    "parse_args",
    "parse_requirements",
    "pip_python_version",
    "pip_resolution_options",
    "read_text",
    "requirement_candidates",
    "sanitize_public_url",
    "select_generated_manifest",
    "shell_commands",
    "shell_contexts",
    "shell_files",
    "strip_heredocs",
    "unquoted_heredoc_matches",
    "write_outputs",
]
