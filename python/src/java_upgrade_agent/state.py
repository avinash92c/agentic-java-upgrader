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
    hints_file: str  # migration_hints.yaml: fixes verified by a build, reused in later runs
    pending_hint: dict | None  # the last round's fix, recorded if the next build no longer shows its error

    # preflight and baseline
    preflight_ok: bool
    is_git_repo: bool
    java_update: dict | None  # {from, to, files} when the Java version was updated at preflight
    baseline_ok: bool
    baseline_log: str
    baseline_skipped: bool  # True after a Java version update (old code may need repair first)
    baseline_sources: dict[str, str]  # {"group:artifact": version} of dependency sources before the upgrades
    current_sources: dict[str, str]  # the same after the latest build that got past the POM stage

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
    attempts: int  # repair rounds so far (one issue per round)
    issue_tries: dict[str, int]  # {issue key: tries}; an issue is set aside after max_retries tries
    last_issue_keys: list[str]  # issues at the previous repair round, to log what was fixed or appeared
    repair_history: list[dict]  # [{attempt, errors_before, summary, edits, restored}]

    # outcome
    # i dont know if this is sufficient to track errors. 
    # but for skeleton lets hold and update later
    defer_reason: str | None
    exit_reason: str | None
    report: str  # markdown
