"""Graph nodes. LLM steps get their chat model from the run config:
graph.invoke(state, config={"configurable": {"llm": model}}).
"""

import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from . import compile_fix, consultant, learning, repair, version_select
from .prompts import load_prompt
from .state import AgentState, AppliedUpgrade, QueueItem
from .tools import java_version, migration
from .tools.pom import ProjectPoms
from .tools.repo import VersionLookupError, default_source
from .tracing import log

# Learning: a new error within this many lines of a fix means the fix wasn't right.
LEARNING_NEARBY_LINES = 3
# Java syntax errors: after an upgrade these come from the agent's own edits, so their fixes aren't learned.
SYNTAX_ERROR = re.compile(r"'[^']+'(?: or '[^']+')* expected|illegal start of|not a statement|unclosed |"
                          r"reached end of file while parsing|class, interface, enum, or record expected|"
                          r"orphaned |<identifier> expected")

BUILD_COMMAND =["-B", "clean", "install", "-D", "skipTests"]

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


def run_maven(project_dir: str, label: str = "build") -> tuple[bool, str, dict[str, str]]:
    """Run `mvn clean install` in the project, then download the dependency sources.
    Returns (succeeded, combined build output, {"group:artifact": version} of the sources jars)."""
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
    sources = download_sources(project_dir, label)
    return ok, output, sources


SOURCES_COMMAND = ["-B", "dependency:sources"]
RESOLVED_SOURCES = re.compile(r"\[INFO\]\s+([\w.\-]+):([\w.\-]+):jar:sources:([^:\s]+)")


def download_sources(project_dir: str, label: str) -> dict[str, str]:
    """Download the -sources.jar of every dependency into ~/.m2 (already present ones are skipped), so
    the repair step can read library source and javadoc locally. A separate Maven call: its result
    never affects the build's result or log, and a failure is only a warning.
    Returns {"group:artifact": version} for every sources jar Maven resolved."""
    mvn = shutil.which("mvn") or "mvn"
    started = time.monotonic()
    result = subprocess.run([mvn, *SOURCES_COMMAND], cwd=project_dir, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    sources: dict[str, str] = {}
    resolved_section = False
    for line in result.stdout.splitlines():
        if "have been resolved" in line:
            resolved_section = "NOT" not in line
        elif resolved_section and (m := RESOLVED_SOURCES.search(line)):
            sources[f"{m.group(1)}:{m.group(2)}"] = m.group(3)
    if result.returncode == 0:
        log.info("%s: %d dependency sources available (%.0fs)", label, len(sources), time.monotonic() - started)
    else:
        errors = [line for line in result.stdout.splitlines() if "[ERROR]" in line][:3]
        log.warning("%s: downloading dependency sources failed (exit %d): %s", label, result.returncode,
                    " | ".join(errors) or "see Maven output")
    return sources


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


def _repair_llm(config: RunnableConfig):
    """The model for repair rounds: [repair_llm] from the config if set, otherwise [llm]."""
    return (config.get("configurable") or {}).get("repair_llm") or _llm(config)


def _model_name(llm) -> str:
    if llm is None:
        return "-"
    return f"{type(llm).__name__} `{getattr(llm, 'model', None) or getattr(llm, 'model_name', None) or '-'}`"


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

    llm, repair_llm = _llm(config), _repair_llm(config)
    if llm is None:
        return stop("no LLM configured")
    for role, model in [("[llm]", llm)] + ([("[repair_llm]", repair_llm)] if repair_llm is not llm else []):
        try:
            reply = model.bind_tools([_ping]).invoke("Call the ping tool now.")
        except Exception as e:
            return stop(f"the LLM check failed for {role} {_model_name(model)}: {e}")
        if not getattr(reply, "tool_calls", None):
            return stop(f"{role} {_model_name(model)} did not make a tool call; use a model that supports tool calling")
        log.info("preflight: %s %s can call tools", role, _model_name(model))
    log.info("preflight: %s; repair uses %s", "git repo clean" if is_git else "no git", _model_name(repair_llm))

    java_update = None
    try:
        java_update = _check_java_version(project_dir)
    except JavaUpdateDeclined as e:
        return stop(str(e))
    return {"preflight_ok": True, "is_git_repo": is_git, "java_update": java_update}


class JavaUpdateDeclined(Exception):
    pass


def _check_java_version(project_dir: str) -> dict | None:
    """If the JDK Maven uses can't compile the project's Java version, offer to update the POMs to a
    newer version. Returns {from, to, files} when updated; raises JavaUpdateDeclined on "no"."""
    try:
        current = java_version.project_version(project_dir)
    except (FileNotFoundError, ET.ParseError):
        return None  # extraction reports broken POMs later
    jdk = java_version.jdk_version(project_dir)
    if current is None or jdk is None:
        log.info("preflight: Java version check skipped (project %s, JDK %s)", current or "not set", jdk or "unknown")
        return None
    minimum = java_version.min_release(jdk)
    if current >= minimum:
        log.info("preflight: project targets Java %d, JDK %d supports it", current, jdk)
        return None

    choices = [v for v in java_version.LTS_VERSIONS if v <= jdk] or [jdk]
    print(f"\nThe project targets Java {current}, which the installed JDK {jdk} can no longer compile "
          f"(oldest supported: {minimum}).", flush=True)
    try:
        answer = input(f"Update the project to a newer Java version? [{'/'.join(map(str, choices))}/n] "
                       f"(default {choices[0]}): ").strip().lower()
    except EOFError:  # not interactive: treat as no
        answer = "n"
    if answer in ("n", "no"):
        raise JavaUpdateDeclined(f"the project targets Java {current}, which JDK {jdk} cannot compile, "
                                 "and the Java version update was declined")
    target = int(answer) if answer.isdigit() and int(answer) in choices else choices[0]
    files = java_version.update_version(project_dir, target)
    log.info("preflight: Java version %d -> %d in %s", current, target, ", ".join(files) or "no POM (nothing to change)")
    return {"from": current, "to": target, "files": files}


def baseline_build(state: AgentState) -> AgentState:
    """Build before any change; a failure stops the run. Skipped after a Java version update: old code
    may need repair to compile on the new Java version, which the repair step does after the upgrades.
    The dependency sources are still downloaded, so the old library versions are known for API notes."""
    if java := state.get("java_update"):
        log.info("baseline build: skipped (Java %s -> %s; code that no longer compiles is repaired later)",
                 java["from"], java["to"])
        return {"baseline_ok": True, "baseline_log": "", "baseline_skipped": True,
                "baseline_sources": download_sources(state["project_dir"], "baseline")}
    ok, output, sources = run_maven(state["project_dir"], "baseline build")
    update: AgentState = {"baseline_ok": ok, "baseline_log": output, "baseline_sources": sources}
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
    ok, output, sources = run_maven(state["project_dir"], "build")
    # Learning: the last repair round's fix is recorded if its error is gone from this build.
    if pending := dict(state.get("pending_hint") or {}):
        issues = repair.build_issues(output) if not ok else []
        remaining = {learning.signature(i) for i in issues}
        locations = pending.pop("_locations", [])
        # javac often reports an error caused by an edit on a neighbouring line (e.g. the line before a
        # broken call chain), so "near" any fixed place counts, not only the same line.
        nearby = [i for i in issues if "error" in i and any(
            Path(i["error"]["path"]).name == file and abs(i["error"]["line"] - line) <= LEARNING_NEARBY_LINES
            for file, line in locations)]
        if pending["signature"] in remaining:
            log.info("learning: error still present after the fix, not recorded: %s", pending["signature"])
        elif nearby:
            log.info("learning: the fix removed the error but a new one appeared near a fixed place (%s), "
                     "not recorded: %s", nearby[0]["text"], pending["signature"])
        else:
            learning.record(Path(state.get("hints_file") or learning.DEFAULT_HINTS_FILE), pending)
    # A build that fails on the POM resolves nothing: keep the last known versions then.
    return {"build_ok": ok, "build_log": output, "current_sources": sources or state.get("current_sources", {}),
            "pending_hint": None}


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


MAX_REPAIR_ROUNDS = 50  # safety cap on repair rounds (one issue each) per run


def _rename_for_issue(state: AgentState, issue: dict) -> tuple[str | None, list[str]]:
    """POM issue about a missing dependency that an upgraded BOM clearly renamed: rename it in pom.xml.
    Returns (description, edited POM paths), or (None, []) when there is no clear rename."""
    for group, artifact in migration.missing_dependencies(issue["log"]):
        try:
            found = migration.clear_rename(default_source(), group, artifact, state.get("applied", []))
        except Exception as e:  # a shortcut: never let it break the repair step
            log.warning("repair: rename check for %s:%s failed: %s", group, artifact, e)
            continue
        if found is None:
            continue
        new_artifact, upgrade = found
        paths = ProjectPoms.load(state["project_dir"]).rename_artifact(group, artifact, new_artifact)
        if paths:
            return (f"renamed by code: {group}:{artifact} -> {new_artifact} in {', '.join(paths)} "
                    f"(renamed in {upgrade.artifact} {upgrade.new_version})"), paths
    return None, []


def _known_fixes(state: AgentState, issue: dict) -> str:
    """Fixes for the same error recorded in earlier runs (migration_hints.yaml), as prompt text."""
    hints = learning.find_hints(Path(state.get("hints_file") or learning.DEFAULT_HINTS_FILE), learning.signature(issue))
    if hints:
        log.info("learning: %d known fix(es) for this error from earlier runs", len(hints))
    return learning.format_hints(hints)


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _fix_structured(state: AgentState, config: RunnableConfig, issue: dict, advice: str = "(none)",
                    advice_source: str = "no consultant", extra: str = "",
                    group: list[dict] | None = None) -> tuple[str, list[str], str, list[str]]:
    """One issue (code, dependency or POM) fixed with one structured-output call: the model returns
    dependencies to add and text edits, and code applies them exactly (dependencies are inserted into the
    right pom.xml by code). `group` holds every issue with the same error (in other files or lines); all of
    them are fixed in this one call. For compile errors, if nothing could be applied, the model is asked
    again with API notes from the library sources. Returns (summary, edited files, notes, changes)."""
    project_dir = state["project_dir"]
    group = group or [issue]
    error = issue.get("error")
    errors = [i["error"] for i in group if "error" in i]
    upgrades = "\n".join(f"- {u.module}: {u.group}:{u.artifact} {u.old_version} -> {u.new_version}"
                         for u in state.get("applied", [])) or "- (none)"
    earlier = "\n".join(f"- try {h['try']}: {h['summary']}" for h in state.get("repair_history", [])
                        if h.get("issue_key") == issue["key"]) or "- none"
    if errors:  # each file whole if small; otherwise imports, setup code and ±25 lines around each error
        context = repair.group_fix_context(errors, project_dir)
        if len(errors) > 1:
            places = ", ".join(f"{Path(e['path']).name}:{e['line']}" for e in errors)
            context = (f"The same error occurs at {len(errors)} places: {places}. Fix ALL of them in this answer "
                       f"(one edit per place).\n\n{context}")
    else:
        context = f"```\n{issue['log']}\n```\n" + repair.pom_context(issue["log"], project_dir)
    known = _known_fixes(state, issue)
    allow_pom = issue["kind"] in ("dependency", "pom")
    error_files = [e["path"] for e in errors]

    def ask(notes: str) -> tuple[str, list[str], list[str]]:
        task = load_prompt("compile_fix_task.md", upgrades=upgrades, java_context=context, extra=extra,
                           notes=notes, known_fixes=known, advice=advice, advice_source=advice_source,
                           history=earlier)
        try:
            fix = compile_fix.ask(_repair_llm(config), task)
        except Exception as e:
            log.warning("fix: %s", e)
            return f"LLM failed: {e}", [], []
        log.info("fix: LLM answered %s with %d dependency(ies) and %d edit(s): %s", fix.status,
                 len(fix.add_dependencies), len(fix.edits), fix.reason)
        if fix.status != "fix" or not (fix.edits or fix.add_dependencies):
            return f"unsure: {fix.reason}", [], []

        edited, changes, problems = [], [], []
        if fix.add_dependencies:
            poms = ProjectPoms.load(project_dir)
            target = poms.pom_for(compile_fix.project_file(project_dir, error["path"])) if error and \
                compile_fix.project_file(project_dir, error["path"]) else "pom.xml"
            for dep in fix.add_dependencies:
                done = poms.add_dependency(target, dep.groupId.strip(), dep.artifactId.strip(),
                                           dep.version.strip(), dep.scope.strip())
                log.info("fix: %s", done)
                if done.startswith("added"):
                    edited.append(target)
                    changes.append(done)
                else:
                    problems.append(done)
        if fix.edits:
            edit_paths, edit_problems = compile_fix.apply_edits(project_dir, fix.edits, error_files, allow_pom)
            edited += edit_paths
            problems += edit_problems
            names = {Path(e).name for e in edit_paths}
            for e in fix.edits:
                name = Path(e.file.replace("\\", "/")).name
                if name not in names:
                    continue
                where = f"{name}:{e.line}" + (f"-{e.end_line}" if e.end_line and e.end_line != e.line else "") \
                    if e.line else name
                before = f"`{_short(e.old)}`" if e.old.strip() else "line(s)"
                changes.append(f"{where}: {before} -> " + (f"`{_short(e.new)}`" if e.new.strip() else "deleted"))
        for problem in problems:
            log.warning("fix: not applied: %s", problem)
        return (fix.reason + (f" (not applied: {'; '.join(problems)})" if problems else ""),
                sorted(set(edited)), changes)

    summary, edited, changes = ask("(none yet)")
    if edited or not error or issue["kind"] != "code":
        return f"[LLM] {summary}", edited, "", changes
    log.info("fix: nothing applied (%s); asking again with API notes from the library sources", summary)
    notes = compile_fix.api_notes([error], state.get("baseline_sources", {}), state.get("current_sources", {}))
    summary, edited, changes = ask(notes)
    return f"[LLM + API notes] {summary}", edited, notes, changes


def _library_of(state: AgentState, issue: dict) -> tuple[str, dict]:
    """The library an issue is about and its old -> new version, for the hints file."""
    old, new = state.get("baseline_sources", {}), state.get("current_sources", {})
    coordinate = None
    if issue["kind"] == "code":
        location = compile_fix.LOCATION_TYPE.search(" ".join(issue["error"]["details"]))
        split = compile_fix._split_fqcn(compile_fix._erase(location.group(1))) if location else None
        coordinate = compile_fix._find_artifact(split[1], new) if split else None
    elif deps := migration.missing_dependencies(issue["log"]):
        coordinate = f"{deps[0][0]}:{deps[0][1]}"
    if issue["kind"] == "dependency":  # the library is the one added by the fix; the package says what was missing
        return f"package {issue['package']}", {}
    if not coordinate:
        return "unknown", {}
    return coordinate, {"from": old.get(coordinate, "?"), "to": new.get(coordinate, "?")}


def _fix_with_agent(state: AgentState, config: RunnableConfig, issue: dict, mode: str, number: int,
                    candidates: str = "", advice: str = "(none)",
                    advice_source: str = "no consultant") -> tuple[str, list[str]]:
    """POM issue (pom mode), missing package (dependency mode) or other issue (full mode): the
    tool-calling agent, given only this issue."""
    edits: list[str] = []
    history = [h for h in state.get("repair_history", []) if h.get("issue_key") == issue["key"]]
    task = repair.build_task(number, state.get("applied", []), issue["log"], history, state["project_dir"], mode,
                             known_fixes=_known_fixes(state, issue), candidates=candidates,
                             advice=advice, advice_source=advice_source)
    try:
        summary = repair.run_repair(_repair_llm(config), state["project_dir"], task, edits, state.get("applied", []), mode)
    except Exception as e:  # provider error or tool-call limit: the build still runs
        summary = f"repair agent stopped: {e}"
        log.warning("repair: %s", summary)
    return summary, sorted(set(edits))


def _consultant(config: RunnableConfig):
    """The [consultant] CLI settings from the run config, or None."""
    return (config.get("configurable") or {}).get("consultant")


def _consultant_task(state: AgentState, issue: dict, candidates: str, group: list[dict] | None = None) -> str:
    """The question for the consultant CLI: the error (at every place it occurs), the code or POM lines,
    the upgrades, earlier tries."""
    project_dir = state["project_dir"]
    group = group or [issue]
    if "error" in issue:
        excerpts = [repair.java_excerpt(i["error"], project_dir) or f"{i['text']} (file not found)" for i in group]
        excerpt = "\n\n".join(excerpts)
        if len(group) > 1:
            excerpt = (f"The same error occurs at {len(group)} places; give a CHANGE block for each:\n\n" + excerpt)
    else:
        excerpt = repair.pom_context(issue["log"], project_dir)
    upgrades = "\n".join(f"- {u.group}:{u.artifact} {u.old_version} -> {u.new_version}"
                         for u in state.get("applied", [])) or "- (none)"
    if java := state.get("java_update"):
        upgrades += f"\n- Java {java['from']} -> {java['to']}"
    extra = f"Artifacts on Maven Central that provide the missing classes:\n{candidates}" if candidates else ""
    history = "\n".join(f"- try {h['try']}: {h['summary']}" for h in state.get("repair_history", [])
                        if h.get("issue_key") == issue["key"]) or "- nothing yet"
    error = "\n".join(i["log"] for i in group)
    return load_prompt("consultant_task.md", error=error, excerpt=excerpt, upgrades=upgrades,
                       extra=extra, history=history)


def repair_agent(state: AgentState, config: RunnableConfig) -> AgentState:
    """One repair round: take the first issue of the latest build that hasn't used up its tries
    (max_retries per issue), fix it, then the graph rebuilds. Issues come from repair.build_issues():
    POM issues first, then compile errors, then other errors. Then the guard against reverted upgrades."""
    build_log = state.get("build_log", "")
    history = state.get("repair_history", [])
    tries = dict(state.get("issue_tries", {}))
    max_tries = state.get("max_retries", 3)
    issues = repair.build_issues(build_log)
    issue = repair.next_issue(build_log, tries, max_tries)
    number = len(history) + 1
    if issue is None:  # after_classify routes here only when there is one; be safe anyway
        return {"attempts": state.get("attempts", 0) + 1}

    # The same error elsewhere (other files or lines): fixed together in this round.
    signature = learning.signature(issue)
    group = [i for i in issues if i["kind"] == issue["kind"] and learning.signature(i) == signature
             and (i is issue or tries.get(i["key"], 0) < max_tries)]
    for member in group:
        tries[member["key"]] = tries.get(member["key"], 0) + 1
    _log_issue_round(number, issues, issue, tries, max_tries, state, group)

    notes, changes, source = "", [], learning.source_of(_repair_llm(config))
    try:
        candidates = ""
        if issue["kind"] == "dependency":
            candidates = repair.dependency_candidates(issue, state["project_dir"])
            log.info("repair: artifacts providing package %s:\n%s", issue["package"],
                     "\n".join("    " + line for line in candidates.splitlines()))
        advice, advice_source = "(none)", "no consultant"
        consultant_settings = _consultant(config)
        if consultant.should_ask(consultant_settings, tries[issue["key"]]):
            answer = consultant.ask(consultant_settings, _consultant_task(state, issue, candidates, group),
                                    state["project_dir"])
            if answer:
                advice, advice_source = answer, consultant_settings.label
                source = f"{consultant_settings.label} advice, applied by {source}"

        # Code, dependency and POM issues: one structured-output call, applied by code.
        # Other issues: the tool-calling agent.
        if issue["kind"] == "code":
            summary, edits, notes, changes = _fix_structured(state, config, issue, advice, advice_source, group=group)
        elif issue["kind"] == "dependency":
            extra = f"Artifacts on Maven Central that provide the missing classes:\n{candidates}"
            summary, edits, notes, changes = _fix_structured(state, config, issue, advice, advice_source, extra,
                                                             group=group)
        elif issue["kind"] == "pom":
            summary, edits = (None, []) if advice != "(none)" else _rename_for_issue(state, issue)
            if summary is None:
                summary, edits, notes, changes = _fix_structured(state, config, issue, advice, advice_source)
            else:
                source = "code (BOM comparison)"
        else:
            summary, edits = _fix_with_agent(state, config, issue, "full", number, advice=advice,
                                             advice_source=advice_source)
    except Exception as e:  # one issue must never end the run: record it and let the next round continue
        log.exception("repair: fixing [%s] %s failed", issue["kind"], issue["text"])
        summary, edits = f"fix failed with an error: {type(e).__name__}: {e}", []

    # Remember the fix; the build node records it in the hints file if its error is gone afterwards.
    pending = None
    failed = re.match(r"(\[[^\]]*\]\s*)?(repair agent stopped|LLM failed|fix failed with an error|unsure)", summary)
    syntax = SYNTAX_ERROR.search(issue["text"])  # repairing a broken edit teaches nothing about upgrades
    if syntax and edits:
        log.info("learning: syntax error fix (%s), not recorded", syntax.group(0))
    if edits and not failed and not syntax:
        library, versions = _library_of(state, issue)
        pending = learning.new_entry(issue, library, versions, summary, changes or [f"edited {e}" for e in edits],
                                     source)
        # where the error was (every place in the group): a new error near one afterwards means the fix wasn't right
        pending["_locations"] = [[Path(i["error"]["path"]).name, i["error"]["line"]] for i in group if "error" in i]

    more = f" (+{len(group) - 1} more place(s) with the same error)" if len(group) > 1 else ""
    entry = {"attempt": number, "issue": f"[{issue['kind']}] {issue['text']}{more}", "issue_key": issue["key"],
             "try": tries[issue["key"]], "issues_total": len(issues), "errors_before": len(issues),
             "summary": summary, "edits": edits, "restored": _restore_reverted(state)}
    if notes:
        entry["notes"] = notes
    outcome = "edited " + ", ".join(edits) if edits else "no change"
    log.info("repair: round %d result for [%s] %s: %s", number, issue["kind"], issue["text"], outcome)
    log.info("repair: summary: %s", " ".join(summary.split())[:500])
    if entry["restored"]:
        log.info("repair: guard restored: %s", "; ".join(entry["restored"]))
    log.info("repair: ---------- end of round %d (the project is rebuilt next) ----------", number)
    return {"attempts": state.get("attempts", 0) + 1, "issue_tries": tries, "repair_history": [*history, entry],
            "last_issue_keys": [i["key"] for i in issues], "pending_hint": pending}


def _log_issue_round(number: int, issues: list[dict], issue: dict, tries: dict[str, int], max_tries: int,
                     state: AgentState, group: list[dict] | None = None) -> None:
    """INFO log of a repair round: what changed since the last round, the whole issue list with tries,
    and the details of the issue(s) being fixed now (`group`: the same error at several places)."""
    group = group or [issue]
    log.info("repair: ========== round %d: %d issue(s) in the latest build ==========", number, len(issues))
    previous = state.get("last_issue_keys")
    if previous is not None:
        keys = {i["key"] for i in issues}
        gone = [k.split("|", 2)[-1] for k in previous if k not in keys]
        new = [i["text"] for i in issues if i["key"] not in set(previous)]
        log.info("repair: since round %d: %d issue(s) gone, %d new", number - 1, len(gone), len(new))
        for text in gone:
            log.info("repair:   gone: %s", text)
        for text in new:
            log.info("repair:   new:  %s", text)
    in_group = {id(i) for i in group}
    for n, i in enumerate(issues, 1):
        current = id(i) in in_group
        used = tries.get(i["key"], 0) - (1 if current else 0)
        status = "set aside" if used >= max_tries and not current else f"tried {used}/{max_tries}"
        log.info("repair: %s #%d [%s] %s (%s)", "->" if current else "  ", n, i["kind"], i["text"], status)
    numbers = ", ".join(f"#{issues.index(i) + 1}" for i in group)
    log.info("repair: fixing %s [%s], try %d/%d%s", numbers, issue["kind"], tries[issue["key"]], max_tries,
             f" (the same error at {len(group)} places, fixed together)" if len(group) > 1 else "")
    for member in group:
        log.info("repair: issue details:\n%s", "\n".join("    " + line for line in member["log"].splitlines()))
        if "error" in member:  # code and dependency issues
            excerpt = repair.java_excerpt(member["error"], state["project_dir"])
            if excerpt:
                log.info("repair: code at the error:\n%s", "\n".join("    " + line for line in excerpt.splitlines()))


def defer_to_user(state: AgentState) -> AgentState:
    if state.get("failure_kind") == "environment":
        reason = ("the build fails for a reason outside the code (dependency download, repository "
                  "access, network or TLS); check repository credentials, network/proxy, or settings.xml")
    else:
        tries, max_tries = state.get("issue_tries", {}), state.get("max_retries", 3)
        left = [i["text"] for i in repair.build_issues(state.get("build_log", ""))]
        given_up = [i["text"] for i in repair.build_issues(state.get("build_log", ""))
                    if tries.get(i["key"], 0) >= max_tries]
        reason = (f"the build still fails after {state.get('attempts', 0)} repair round(s); "
                  f"{len(left)} issue(s) remain, {len(given_up)} of them tried {max_tries} times without success")
    log.warning("defer: %s", reason)
    return {"defer_reason": reason}


def _major(version: str) -> str:
    return re.match(r"\d*", version).group()


def report(state: AgentState, config: RunnableConfig) -> AgentState:
    """Markdown report of the run, returned in state["report"]."""
    applied = state.get("applied", [])
    llm, repair_llm = _llm(config), _repair_llm(config)

    if state.get("exit_reason"):
        outcome = f"Stopped: {state['exit_reason']}."
    elif state.get("defer_reason"):
        outcome = f"Deferred to you: {state['defer_reason']}."
    elif not applied:
        outcome = "Nothing was upgraded: every dependency is up to date or could not be checked."
    else:
        outcome = "All upgrades applied and the build passes."

    lines = ["# Java upgrade report", "", f"- Project: `{state.get('project_dir')}`",
             f"- Model: {_model_name(llm)}",
             f"- Repair model: {_model_name(repair_llm) if repair_llm is not llm else 'same as above'}",
             f"- Build command: `mvn {' '.join(BUILD_COMMAND)}`", "", f"**{outcome}**", ""]

    if java := state.get("java_update"):
        lines += ["## Java version", "", f"Updated from Java {java['from']} to Java {java['to']} (installed JDK could "
                  f"not compile the old version) in: {', '.join(java['files']) or 'no file'}. The baseline build was "
                  "skipped, so build errors after the upgrades can come from the Java update as well.", ""]

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
        lines += ["## Repair rounds (one issue each, build after each)", ""]
        for h in history:
            lines.append(f"### Round {h['attempt']}: {h.get('issue', '')} "
                         f"(try {h.get('try', 1)}; {h.get('issues_total', h['errors_before'])} issue(s) in the build)")
            lines += ["", h["summary"] or "(no summary)", "", f"Files changed: {', '.join(h['edits']) or 'none'}"]
            if h["restored"]:
                lines.append(f"Reverted upgrades put back: {'; '.join(h['restored'])}")
            if h.get("notes"):
                lines += ["", "API notes from the library sources:", "", "```", h["notes"], "```"]
            lines.append("")

    failed_log = state.get("build_log") if state.get("build_ok") is False else (
        state.get("baseline_log") if state.get("baseline_ok") is False else None)
    if failed_log:
        tries = state.get("issue_tries", {})
        remaining = repair.build_issues(failed_log)
        if remaining:
            lines += ["## Remaining issues", ""]
            lines += [f"- [{i['kind']}] {i['text']} (tried {tries.get(i['key'], 0)}x)" for i in remaining]
            lines.append("")
        lines += ["## Remaining build errors", "", "```", repair.error_excerpt(failed_log), "```", ""]

    if (applied or history) and state.get("is_git_repo"):
        lines += ["## Undo", "", "To return to the starting state: `git restore .` and, for files the agent "
                  "created, `git clean -fd`.", ""]
    elif applied or history:
        lines += ["## Undo", "", "The project is not a Git repository: changes were made in place. "
                  "Restore from your own backup if needed.", ""]
    log.info("report: %s", outcome)
    return {"report": "\n".join(lines)}
