"""Graph nodes. LLM steps get their chat model from the run config:
graph.invoke(state, config={"configurable": {"llm": model}}).
"""

import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from . import repair, version_select
from .state import AgentState, AppliedUpgrade, QueueItem
from .tools.pom import ProjectPoms
from .tools.repo import VersionLookupError
from .tracing import log

BUILD_COMMAND = ["-B", "clean", "install", "-D", "skipTests"]

# Maven output that means the build failed for reasons outside the code: dependency or plugin
# resolution, repository auth, network and TLS problems.
ENVIRONMENT_ERRORS = re.compile(
    "|".join([
        r"Could not resolve dependencies",
        r"Could not transfer artifact",
        r"Could not find artifact",
        r"Non-resolvable (parent POM|import POM)",
        r"Plugin \S+ or one of its dependencies could not be resolved",
        r"status code: 40[137]",
        r"\b40[137] (Unauthorized|Forbidden|Proxy Authentication Required)\b",
        r"UnknownHostException",
        r"Connection (refused|timed out|reset)",
        r"connect timed out",
        r"PKIX path building failed",
        r"SSLHandshakeException",
    ]),
    re.IGNORECASE,
)


def run_maven(project_dir: str, label: str = "build") -> tuple[bool, str]:
    """Run `mvn clean install` in the project and return (succeeded, combined output)."""
    mvn = shutil.which("mvn") or "mvn"  # resolves mvn.cmd on Windows
    log.info("%s: mvn %s ...", label, " ".join(BUILD_COMMAND))
    started = time.monotonic()
    result = subprocess.run(
        [mvn, *BUILD_COMMAND],
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ok, output = result.returncode == 0, result.stdout + result.stderr
    log.info("%s: %s in %.0fs (exit %d)", label, "passed" if ok else "FAILED", time.monotonic() - started,
             result.returncode)
    if not ok:
        log.debug("%s: last lines of the log:\n%s", label, "\n".join(output.splitlines()[-40:]))
    return ok, output


def _git(project_dir: str, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run([git, "-C", project_dir, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@tool("ping")
def _ping() -> str:
    """Connectivity check. Call this tool when asked to."""
    return "pong"


def _llm(config: RunnableConfig):
    return (config.get("configurable") or {}).get("llm")


def preflight(state: AgentState, config: RunnableConfig) -> AgentState:
    """The project is a Git repo with a clean tree, and the model can make tool calls."""
    project_dir = state["project_dir"]

    def stop(reason: str) -> AgentState:
        log.error("preflight: %s", reason)
        return {"preflight_ok": False, "exit_reason": reason}

    log.info("preflight: project %s", project_dir)

    try:
        inside = _git(project_dir, "rev-parse", "--is-inside-work-tree")
        is_git = inside.returncode == 0 and inside.stdout.strip() == "true"
    except OSError:  # git not installed
        is_git = False
    if not is_git:
        log.warning("preflight: not a Git repository; changes are made in place and the agent cannot undo them")
    elif _git(project_dir, "status", "--porcelain").stdout.strip():
        return stop("the Git working tree has uncommitted changes; commit or stash them first")

    llm = _llm(config)
    if llm is None:
        return stop("no LLM configured")
    try:
        reply = llm.bind_tools([_ping]).invoke("Call the ping tool now.")
    except Exception as e:
        return stop(f"the LLM check failed: {e}")
    if not getattr(reply, "tool_calls", None):
        return stop("the model did not make a tool call; use a model that supports tool calling")
    log.info("preflight: %s, %s can call tools", "git repo clean" if is_git else "no git", type(llm).__name__)
    return {"preflight_ok": True, "is_git_repo": is_git}


def baseline_build(state: AgentState) -> AgentState:
    ok, output = run_maven(state["project_dir"], "baseline build")
    update: AgentState = {"baseline_ok": ok, "baseline_log": output}
    if not ok:
        update["exit_reason"] = "baseline build failed before any upgrade"
    return update


def extract_dependencies(state: AgentState) -> AgentState:
    try:
        poms = ProjectPoms.load(state["project_dir"])
        dependencies = poms.collect()
    except (FileNotFoundError, ET.ParseError) as e:
        log.error("extract: could not read the POMs: %s", e)
        return {"extraction_ok": False, "exit_reason": f"could not read the POMs: {e}"}

    log.info("extract: %d POM(s): %s", len(poms.texts), ", ".join(poms.texts))
    for module, deps in dependencies.items():
        log.info("extract: %s: %d versioned dependencies", module, len(deps))
        for d in deps:
            log.debug("extract:   %s:%s %s", d.group, d.artifact, d.version)
    queue = [QueueItem(module=module, dependency=dep) for module, deps in dependencies.items() for dep in deps]
    return {"extraction_ok": True, "poms": poms.texts, "dependencies": dependencies, "queue": queue, "applied": []}


def next_dependency(state: AgentState) -> AgentState:
    queue = state.get("queue", [])
    if not queue:
        return {"current": None}
    return {"current": queue[0], "queue": queue[1:]}


def find_versions(state: AgentState, config: RunnableConfig) -> AgentState:
    """Fetch the published versions from Maven Central, then one LLM call picks the newest stable one.

    Results are cached per group:artifact, so an artifact listed in several modules is looked up once.
    """
    current = state["current"]
    dep = current.dependency
    key = f"{dep.group}:{dep.artifact}"
    cache = state.get("version_cache", {})
    if key in cache:
        log.info("versions: %s (%s) %s -> %s (cached)", key, current.module, dep.version, cache[key] or "no change")
        return {"latest_version": cache[key]}

    failure = None
    try:
        selection = version_select.select_version(_llm(config), dep)
    except VersionLookupError as e:  # Maven Central: not found, timeout, network
        selection, failure = None, f"version lookup failed: {e.reason}"
    except Exception as e:  # provider error, unusable answer: never stops the run
        selection, failure = None, f"version selection failed: {e}"

    chosen = selection.new_version if selection and selection.status == "upgrade" else None
    if selection:
        log.info("versions: %s (%s) %s -> %s [%s] %s", key, current.module, dep.version, chosen or "no change",
                 selection.status, selection.reason)
    else:
        log.warning("versions: %s (%s): %s", key, current.module, failure)
    update: AgentState = {"latest_version": chosen, "version_cache": {**cache, key: chosen}}
    if failure:
        update["could_not_check"] = [*state.get("could_not_check", []), _note(current, failure)]
    return update


def _note(item: QueueItem, reason: str) -> dict:
    dep = item.dependency
    return {"module": item.module, "group": dep.group, "artifact": dep.artifact, "version": dep.version,
            "reason": reason}


def apply_version(state: AgentState) -> AgentState:
    """Write the new version where the module's version is defined (literal <version> or ${property})."""
    current, new_version = state["current"], state["latest_version"]
    dep = current.dependency
    edited = state.get("edited_locations", {})
    notes = state.get("apply_notes", [])

    poms = ProjectPoms.load(state["project_dir"])
    location = poms.locate(current.module, dep.group, dep.artifact)
    if location is None:
        log.warning("apply: %s:%s (%s): version definition not found, not applied",
                    dep.group, dep.artifact, current.module)
        return {"apply_notes": [*notes, _note(current, "could not apply: version definition not found")]}

    key = location.key(dep.group, dep.artifact)
    if key in edited:  # shared or inherited property already changed by an earlier entry
        log.info("apply: %s:%s (%s): already set via %s", dep.group, dep.artifact, current.module, edited[key])
        return {"apply_notes": [*notes, _note(current, f"applied via {edited[key]}")]}

    poms.write(location, new_version)
    where = f"${{{location.property}}}" if location.property else "<version>"
    log.info("apply: %s:%s %s -> %s in %s %s", dep.group, dep.artifact, dep.version, new_version,
             location.path, where)
    upgrade = AppliedUpgrade(module=current.module, group=dep.group, artifact=dep.artifact,
                             old_version=dep.version, new_version=new_version)
    return {
        "applied": [*state.get("applied", []), upgrade],
        "edited_locations": {**edited, key: f"{current.module}/{dep.artifact}"},
    }


def build(state: AgentState) -> AgentState:
    """Run the Maven build after the upgrades, and again after each repair attempt."""
    ok, output = run_maven(state["project_dir"], "build")
    return {"build_ok": ok, "build_log": output}


def classify_failure(state: AgentState) -> AgentState:
    """Decide whether a failed build is something the LLM can fix (code) or not (environment).

    Environment failures (artifacts that can't be downloaded, auth, network, TLS) go straight
    to the user. Anything else, compiler errors included, goes to the repair agent. Exception:
    if the previous repair attempt edited a pom.xml, a resolution error is likely its own doing
    and goes back to the agent.
    """
    match = ENVIRONMENT_ERRORS.search(state.get("build_log", ""))
    if not match:
        log.info("classify: code (no environment error found)")
        return {"failure_kind": "code"}
    history = state.get("repair_history", [])
    if history and any(path.endswith("pom.xml") for path in history[-1]["edits"]):
        log.info("classify: code (\"%s\", but the last repair edited a pom.xml)", match.group(0))
        return {"failure_kind": "code"}
    log.info("classify: environment (\"%s\")", match.group(0))
    return {"failure_kind": "environment"}


def _restore_reverted(state: AgentState) -> list[str]:
    """Guard: put back any upgraded version the repair agent reverted or changed."""
    try:
        poms = ProjectPoms.load(state["project_dir"])
        current = poms.collect()
    except (FileNotFoundError, ET.ParseError):
        return []  # a broken POM shows up in the next build
    restored = []
    for u in state.get("applied", []):
        versions = {(d.group, d.artifact): d.version for d in current.get(u.module, [])}
        found = versions.get((u.group, u.artifact))
        if found is not None and found != u.new_version:
            location = poms.locate(u.module, u.group, u.artifact)
            if location is not None:
                poms.write(location, u.new_version)
                restored.append(f"{u.module}: {u.artifact} {found} -> {u.new_version}")
                log.warning("guard: %s: %s was changed to %s, put back to %s", u.module, u.artifact, found,
                            u.new_version)
    return restored


def repair_agent(state: AgentState, config: RunnableConfig) -> AgentState:
    """One repair attempt by the LLM, then the guard against reverted upgrades.

    An attempt that reduced the error count does not use up one of the allowed attempts.
    """
    build_log = state.get("build_log", "")
    history = state.get("repair_history", [])
    errors_now = repair.error_count(build_log)
    improved = bool(history) and errors_now < history[-1]["errors_before"]
    attempts = state.get("attempts", 0) + (0 if improved else 1)
    number = len(history) + 1

    log.info("repair: attempt %d, %d error(s), counted attempts %d/%d%s", number, errors_now, attempts,
             state.get("max_retries", 3), " (free: the last attempt reduced the errors)" if improved else "")
    edits: list[str] = []
    task = repair.build_task(number, state.get("applied", []), build_log, history)
    try:
        summary = repair.run_repair(_llm(config), state["project_dir"], task, edits)
    except Exception as e:  # provider error or tool-call limit: the build still runs
        summary = f"repair agent stopped: {e}"
        log.warning("repair: %s", summary)

    entry = {"attempt": number, "errors_before": errors_now, "summary": summary,
             "edits": sorted(set(edits)), "restored": _restore_reverted(state)}
    log.info("repair: attempt %d edited %s", number, ", ".join(entry["edits"]) or "nothing")
    log.info("repair: summary: %s", " ".join(summary.split())[:300])
    return {"attempts": attempts, "repair_history": [*history, entry]}


def defer_to_user(state: AgentState) -> AgentState:
    if state.get("failure_kind") == "environment":
        reason = ("the build fails for a reason outside the code (dependency download, repository "
                  "access, network or TLS); check repository credentials, network/proxy, or settings.xml")
    else:
        reason = f"the build still fails after {state.get('attempts', 0)} repair attempts"
    log.warning("defer: %s", reason)
    return {"defer_reason": reason}


def _major(version: str) -> str:
    return re.match(r"\d*", version).group()


def report(state: AgentState, config: RunnableConfig) -> AgentState:
    """Markdown report of the run, returned in state["report"]."""
    applied = state.get("applied", [])
    llm = _llm(config)
    model = getattr(llm, "model", None) or getattr(llm, "model_name", None) or "-"

    if state.get("exit_reason"):
        outcome = f"Stopped: {state['exit_reason']}."
    elif state.get("defer_reason"):
        outcome = f"Deferred to you: {state['defer_reason']}."
    elif not applied:
        outcome = "Nothing was upgraded: every dependency is up to date or could not be checked."
    else:
        outcome = "All upgrades applied and the build passes."

    lines = ["# Java upgrade report", "", f"- Project: `{state.get('project_dir')}`",
             f"- Model: {type(llm).__name__ if llm else '-'} `{model}`",
             f"- Build command: `mvn {' '.join(BUILD_COMMAND)}`", "", f"**{outcome}**", ""]

    if applied:
        lines += ["## Upgrades applied", "", "| Module | Artifact | From | To | Major |", "|---|---|---|---|---|"]
        for u in applied:
            major = "yes" if _major(u.old_version) != _major(u.new_version) else ""
            lines.append(f"| {u.module} | {u.group}:{u.artifact} | {u.old_version} | {u.new_version} | {major} |")
        lines.append("")

    for title, entries in [("Applied through a shared property / not applied", state.get("apply_notes", [])),
                           ("Could not check", state.get("could_not_check", []))]:
        if entries:
            lines += [f"## {title}", ""]
            lines += [f"- {e['module']}: {e['group']}:{e['artifact']} {e['version']}: {e['reason']}" for e in entries]
            lines.append("")

    history = state.get("repair_history", [])
    if history:
        lines += ["## Repair attempts", ""]
        for h in history:
            lines.append(f"### Attempt {h['attempt']} ({h['errors_before']} errors before)")
            lines += ["", h["summary"] or "(no summary)", "", f"Files changed: {', '.join(h['edits']) or 'none'}"]
            if h["restored"]:
                lines.append(f"Reverted upgrades put back: {'; '.join(h['restored'])}")
            lines.append("")

    failed_log = state.get("build_log") if state.get("build_ok") is False else (
        state.get("baseline_log") if state.get("baseline_ok") is False else None)
    if failed_log:
        lines += ["## Remaining build errors", "", "```", repair.error_excerpt(failed_log), "```", ""]

    if (applied or history) and state.get("is_git_repo"):
        lines += ["## Undo", "", "To return to the starting state: `git restore .` and, for files the agent "
                  "created, `git clean -fd`.", ""]
    elif applied or history:
        lines += ["## Undo", "", "The project is not a Git repository: changes were made in place. "
                  "Restore from your own backup if needed.", ""]
    log.info("report: %s", outcome)
    return {"report": "\n".join(lines)}
