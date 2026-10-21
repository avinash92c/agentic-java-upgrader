"""Graph state and the models it carries."""

from typing import Literal, TypedDict

from pydantic import BaseModel


class Dependency(BaseModel):
    """One versioned entry from a POM, as produced by dependency extraction."""

    group: str
    artifact: str
    version: str


# {<project-name/module-name>: [Dependency, ...]}
DependencyMap = dict[str, list[Dependency]]

FailureKind = Literal["code", "environment"]


class QueueItem(BaseModel):
    """A dependency waiting in the upgrade loop, with the module it was declared in."""

    module: str
    dependency: Dependency


class AppliedUpgrade(BaseModel):
    module: str
    group: str
    artifact: str
    old_version: str
    new_version: str


class AgentState(TypedDict, total=False):
    # inputs
    project_dir: str
    max_retries: int

    # preflight and baseline
    preflight_ok: bool
    is_git_repo: bool
    baseline_ok: bool
    baseline_log: str

    # dependency extraction and the upgrade loop
    extraction_ok: bool
    poms: dict[str, str]  # {relative path of each pom.xml: content}
    dependencies: DependencyMap
    queue: list[QueueItem]
    current: QueueItem | None
    latest_version: str | None
    version_cache: dict[str, str | None]  # {"group:artifact": chosen version, None if not upgradable}
    could_not_check: list[dict]  # [{module, group, artifact, version, reason}] for the report
    applied: list[AppliedUpgrade]
    edited_locations: dict[str, str]  # {POM location key: "module/artifact" that changed it}
    apply_notes: list[dict]  # [{module, group, artifact, version, reason}]: applied via / could not apply

    # build and repair
    build_ok: bool
    build_log: str  # output of the latest build
    failure_kind: FailureKind | None
    attempts: int
    repair_history: list[dict]  # [{attempt, errors_before, summary, edits, restored}]

    # outcome
    # i dont know if this is sufficient to track errors. 
    # but for skeleton lets hold and update later
    defer_reason: str | None
    exit_reason: str | None
    report: str  # markdown
