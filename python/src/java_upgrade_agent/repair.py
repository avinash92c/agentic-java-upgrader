"""Build repair: a tool-calling agent that edits the project until the errors are fixed."""

import json
import re
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool

from .compile_fix import project_file
from .prompts import load_prompt
from .state import AppliedUpgrade
from .tools.files import make_edit_tools, make_read_tools
from .tools.library import make_library_tools
from .tools.migration import make_migration_tools, migration_hints, missing_dependencies
from .tools.repo import default_source, make_find_versions_tool
from .tracing import AgentTraceLogger, log

RECURSION_LIMIT = 60  # roughly 30 tool calls per attempt
MAX_ERROR_LINES = 150
NOISE = re.compile(r"\[ERROR\]\s*(-> \[Help|To see the full|Re-run Maven|For more information|$)")
COMPILER_ERROR = re.compile(r"\[ERROR\] .+\.(java|kt):\[\d+")

# Maven POM (model) errors: "The project g:a:v (C:\...\pom.xml) has 1 error" followed by
# "... is missing. @ line 45, column 15"; or "..., C:\...\pom.xml, line 45, column 15".
PROJECT_POM = re.compile(r"The project \S+ \((.+?pom\.xml)\) has \d+ error")
LINE_REF = re.compile(r"@ line (\d+), column \d+")
PATH_LINE_REF = re.compile(r"([A-Za-z]:[\\/][^,]*?pom\.xml|/[^,]*?pom\.xml), line (\d+)")
CONTEXT_LINES = 10
MAX_POM_EXCERPTS = 3

# Compile errors: "[ERROR] /C:/path/Foo.java:[84,33] cannot find symbol" (newer compiler plugin),
# "[ERROR] C:\path\Foo.java:[84,33] ..." (older plugin), or "/home/x/Foo.java:[..]"; followed (in the
# second listing) by "[ERROR]   symbol: ..." and "[ERROR]   location: ..." lines.
COMPILE_ERROR = re.compile(r"\[ERROR\]\s+((?:/?[A-Za-z]:)?[\\/][^\[\]]+?\.(?:java|kt)):\[(\d+),(\d+)\]\s*(.*)")
SYMBOL_LINE = re.compile(r"\[ERROR\]\s+(symbol|location):\s*(.*)")
JAVA_CONTEXT_LINES = 8
MAX_JAVA_EXCERPTS = 3

# Lookups whose result can't change within one attempt: a repeated call is answered from memory.
NO_REPEAT_TOOLS = {"find_versions", "read_artifact_pom", "inspect_class", "read_library_docs",
                   "find_replacement_artifact"}
# Maven coordinates: after stripping, they must be a single token (they go into URLs and paths).
COORDINATE_ARGS = {"group", "artifact", "version"}
# Exact file text: never stripped, or edits would stop matching the file.
VERBATIM_ARGS = {"old", "new", "content"}


def pom_context(log: str, project_dir: str) -> str:
    """Excerpts of the pom.xml lines that Maven's POM errors point to, with line numbers."""
    refs: list[tuple[str, int]] = []
    current_pom = None
    for line in log.splitlines():
        if m := PROJECT_POM.search(line):
            current_pom = m.group(1)
        if (m := PATH_LINE_REF.search(line)) is not None:
            ref = (m.group(1), int(m.group(2)))
        elif (m := LINE_REF.search(line)) is not None and current_pom:
            ref = (current_pom, int(m.group(1)))
        else:
            continue
        if ref not in refs:
            refs.append(ref)

    root = Path(project_dir).resolve()
    excerpts = []
    for pom, number in refs[:MAX_POM_EXCERPTS]:
        path = Path(pom)
        if not path.is_file():
            continue
        try:
            shown = path.resolve().relative_to(root).as_posix()
        except ValueError:
            shown = str(path)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(1, number - CONTEXT_LINES), min(len(lines), number + CONTEXT_LINES)
        body = "\n".join(f"{'>>' if n == number else '  '} {n:4} | {lines[n - 1]}" for n in range(start, end + 1))
        excerpts.append(f"{shown}, around line {number}:\n```xml\n{body}\n```")
    return "\n\n".join(excerpts) or "(no pom.xml locations in the errors)"


def compile_errors(log: str) -> list[dict]:
    """Distinct compile errors: [{path, line, column, details}], where details holds the message and the
    "symbol: ..." / "location: ..." lines. Windows paths "/C:/x" are returned as "C:/x"."""
    errors: dict[tuple[str, int, int], list[str]] = {}
    current = None
    for line in log.splitlines():
        if m := COMPILE_ERROR.search(line):
            current = (m.group(1), int(m.group(2)), int(m.group(3)))
            details = errors.setdefault(current, [])
            if m.group(4).strip() and m.group(4).strip() not in details:
                details.append(m.group(4).strip())
        elif current and (m := SYMBOL_LINE.search(line)):
            detail = f"{m.group(1)}: {m.group(2).strip()}"
            if detail not in errors[current]:
                errors[current].append(detail)
        elif "[ERROR]" in line:
            current = None
    return [{"path": raw[1:] if re.match(r"/[A-Za-z]:", raw) else raw, "line": n, "column": c, "details": d}
            for (raw, n, c), d in errors.items()]


def java_excerpt(error: dict, project_dir: str) -> str | None:
    """The Java lines around one compile error (`>>` on the failing line), with its message, symbol and location."""
    path, number, column, details = project_file(project_dir, error["path"]), error["line"], error["column"], error["details"]
    if path is None:
        return None
    try:
        shown = path.resolve().relative_to(Path(project_dir).resolve()).as_posix()
    except ValueError:
        shown = str(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end = max(1, number - JAVA_CONTEXT_LINES), min(len(lines), number + JAVA_CONTEXT_LINES)
    body = "\n".join(f"{'>>' if n == number else '  '} {n:4} | {lines[n - 1]}" for n in range(start, end + 1))
    return f"{shown}, line {number}, column {column}: {'; '.join(details)}\n```java\n{body}\n```"


MAX_FULL_FILE_LINES = 250  # smaller files are shown whole to the model fixing an error
FIX_WINDOW_LINES = 25  # larger files: this many lines around the error, plus imports and constructors


SETUP_ANNOTATIONS = re.compile(r"@(PostConstruct|Bean|Autowired|Inject|BeforeEach|Before|Override)\b")


def _setup_ranges(lines: list[str], type_decl: int, name: str) -> list[tuple[int, int]]:
    """Line ranges of a class's fields and setup code (constructors, @PostConstruct/@Bean/@Autowired methods):
    where objects are configured, which the failing call often depends on."""
    ranges, depth = [], 0
    i = type_decl
    while i <= len(lines):
        line = lines[i - 1]
        if depth == 1 and line.strip():
            stripped = line.strip()
            annotated_setup = SETUP_ANNOTATIONS.search(stripped) and "Override" not in stripped
            is_constructor = re.match(rf"(public|protected|private)?\s*{name}\s*\(", stripped)
            if (annotated_setup or is_constructor) and not stripped.endswith(";"):
                # the member starts here (annotations first); take it up to its closing brace
                start, inner, opened = i, 0, False
                for j in range(i, len(lines) + 1):
                    inner += lines[j - 1].count("{") - lines[j - 1].count("}")
                    opened = opened or "{" in lines[j - 1]
                    if opened and inner <= 0:
                        ranges.append((start, j))
                        depth += 0  # the member's braces are balanced
                        i = j + 1
                        break
                else:
                    i += 1
                continue
            declaration = re.sub(r"@[\w.]+(\([^)]*\))?\s*", "", stripped)  # without its annotations
            if stripped.endswith(";") and "(" not in declaration.split("=")[0]:  # a field
                ranges.append((i, i))
        depth += line.count("{") - line.count("}")
        i += 1
    return ranges


def group_fix_context(errors: list[dict], project_dir: str) -> str:
    """The code for several errors with the same cause: each file once, every error line in it marked."""
    by_file: dict[str, list[dict]] = {}
    for error in errors:
        by_file.setdefault(error["path"], []).append(error)
    blocks = [java_fix_context(errs[0], project_dir, also=[e["line"] for e in errs[1:]]) for errs in by_file.values()]
    return "\n\n".join(b for b in blocks if b) or "(files not found)"


def java_fix_context(error: dict, project_dir: str, also: list[int] = ()) -> str | None:
    """What the fixing model sees of the failing Java file: the whole file if it's small, otherwise the
    package/imports, the class's fields and setup methods (constructors, @PostConstruct, @Bean, ...) and
    FIX_WINDOW_LINES lines around the error. Line numbers on every line, `>>` on the failing one, `...`
    where lines are left out."""
    path = project_file(project_dir, error["path"])
    if path is None:
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    number = error["line"]
    marked = {number, *also}  # every error line in this file
    if len(lines) <= MAX_FULL_FILE_LINES:
        ranges = [(1, len(lines))]
    else:
        ranges = [(max(1, n - FIX_WINDOW_LINES), min(len(lines), n + FIX_WINDOW_LINES)) for n in sorted(marked)]
        type_decl = next((i for i, line in enumerate(lines, 1)
                          if re.match(r"\s*(public\s+|abstract\s+|final\s+)*(class|interface|enum|record)\s+\w+", line)), None)
        if type_decl:
            ranges.append((1, type_decl))  # package, imports, class annotations and declaration
            name = re.search(r"(class|interface|enum|record)\s+(\w+)", lines[type_decl - 1]).group(2)
            ranges += _setup_ranges(lines, type_decl, name)
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 2:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    body = []
    for k, (start, end) in enumerate(merged):
        if k or start > 1:
            body.append("          ...")
        body += [f"{'>>' if n in marked else '  '} {n:4} | {lines[n - 1]}" for n in range(start, end + 1)]
    if merged and merged[-1][1] < len(lines):
        body.append("          ...")
    try:
        shown = path.resolve().relative_to(Path(project_dir).resolve()).as_posix()
    except ValueError:
        shown = str(path)
    scope = ("whole file" if merged == [[1, len(lines)]]
             else "imports, fields, setup methods (constructors, @PostConstruct, @Bean) and the lines around the error")
    where = f"line {number}, column {error['column']}" if not also else \
        "lines " + ", ".join(str(n) for n in sorted(marked))
    return (f"{shown} ({scope}), error at {where}: {'; '.join(error['details'])}\n"
            f"```java\n" + "\n".join(body) + "\n```")


def java_context(log: str, project_dir: str) -> str:
    """Excerpts of the Java lines that compile errors point to, with the error, symbol and location."""
    excerpts = [e for e in (java_excerpt(err, project_dir) for err in compile_errors(log)[:MAX_JAVA_EXCERPTS]) if e]
    return "\n\n".join(excerpts) or "(no compile error locations in the errors)"


# --- issue list ---------------------------------------------------------------------------------

# Maven's wrapper and help lines: they repeat or summarise real errors, so they are not issues.
WRAPPER = re.compile(r"COMPILATION ERROR|Failed to execute goal .*(Compilation failure|could not be resolved)|"
                     r"BUILD FAILURE|Some problems were encountered|The build could not read|"
                     r"The project \S+ \(.+\) has \d+ error|-> \[Help|\[Help \d+\]|To see the full|Re-run Maven|"
                     r"For more information|^\s*$")
PACKAGE_MISSING = re.compile(r"package ([\w.]+) does not exist")
IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;")
POM_ISSUE = re.compile(r"'dependencies\.dependency\.version' for|Could not find artifact|Non-resolvable|"
                       r"Could not resolve dependencies|'[\w.]+' for [\w.\-]+:[\w.\-]+ (is missing|must be)")


def _message(line: str) -> str:
    return " ".join(line.replace("[ERROR]", " ").split())


def build_issues(log: str) -> list[dict]:
    """The distinct problems in a failed build, in the order they should be fixed: POM issues first (they
    block compilation), then missing packages (a dependency is missing; every use of the package fails
    until it's back), then other compile errors (by file and line), then other errors. Each issue:
    {key, kind: "pom" | "dependency" | "code" | "other", text, log (the lines to show a fixer),
     error (compile errors only), package (dependency only)}."""
    pom, dependency, code, other, seen = [], [], [], [], set()
    project_line = next((line for line in log.splitlines() if PROJECT_POM.search(line)), "")

    for error in compile_errors(log):
        message = " ".join(error["details"])
        missing = PACKAGE_MISSING.search(message)
        kind = "dependency" if missing else "code"
        key = f"{kind}|{Path(error['path']).as_posix()}|{message}"
        if key not in seen:
            seen.add(key)
            text = f"{Path(error['path']).name}:{error['line']} {message}"
            entry = {"key": key, "kind": kind, "text": text, "log": f"[ERROR] {text}", "error": error}
            if missing:
                entry["package"] = missing.group(1)
            (dependency if missing else code).append(entry)

    for line in log.splitlines():
        if "[ERROR]" not in line or WRAPPER.search(line) or COMPILE_ERROR.search(line) or SYMBOL_LINE.search(line):
            continue
        message = _message(line)
        kind = "pom" if POM_ISSUE.search(line) else "other"
        key = f"{kind}|{message}"
        if not message or key in seen:
            continue
        seen.add(key)
        entry = {"key": key, "kind": kind, "text": message,
                 "log": "\n".join(x for x in (project_line, f"[ERROR] {message}") if x)}
        (pom if kind == "pom" else other).append(entry)

    if not (pom or dependency or code or other) and "BUILD FAILURE" in log:  # no recognisable error line
        excerpt = error_excerpt(log)
        other.append({"key": "other|build failure", "kind": "other", "text": "build failed (see log)", "log": excerpt})
    for group in (dependency, code):
        group.sort(key=lambda i: (i["error"]["path"], i["error"]["line"]))
    return pom + dependency + code + other


def next_issue(log: str, tries: dict[str, int], max_tries: int) -> dict | None:
    """The first issue in the build log that hasn't used up its tries, or None."""
    return next((issue for issue in build_issues(log) if tries.get(issue["key"], 0) < max_tries), None)


def _clean_args(args: dict) -> tuple[dict, str | None]:
    """Strip string arguments (except exact file text). Returns (cleaned args, error for the model or None)."""
    cleaned = {k: v.strip() if isinstance(v, str) and k not in VERBATIM_ARGS else v for k, v in args.items()}
    for key in sorted(COORDINATE_ARGS & cleaned.keys()):
        value = cleaned[key]
        if isinstance(value, str) and (not value or re.search(r"\s", value)):
            return cleaned, (f"error: {key}={value!r} is not valid. Pass one value with no spaces or line breaks, "
                             f"e.g. group=\"org.springframework.ai\", artifact=\"spring-ai-bom\", version=\"2.0.0\".")
    return cleaned, None


def _guarded(tool: BaseTool) -> BaseTool:
    """Wrap a tool: clean its arguments, answer repeated lookups from memory, and turn any error into
    a tool result, so one bad call can't end the whole repair attempt."""
    seen: set[str] = set()

    def run(**kwargs):
        args, error = _clean_args(kwargs)
        if error:
            return error
        if tool.name in NO_REPEAT_TOOLS:
            key = json.dumps(args, sort_keys=True, default=str)
            if key in seen:
                return (f"You already called {tool.name} with these arguments in this attempt. "
                        "Use that result; do not call it again. Fix the pom.xml or code with the information you have.")
            seen.add(key)
        try:
            return tool.invoke(args)
        except Exception as e:
            log.warning("repair: tool %s failed: %s", tool.name, e)
            return f"error: {tool.name} failed: {e}"

    return StructuredTool.from_function(func=run, name=tool.name, description=tool.description,
                                        args_schema=tool.args_schema)


def error_excerpt(log: str) -> str:
    """The build's [ERROR] lines without Maven's help text, or the end of the log if there are none."""
    lines = [line for line in log.splitlines() if "[ERROR]" in line and not NOISE.search(line)]
    if not lines:
        lines = log.splitlines()[-80:]
    return "\n".join(lines[:MAX_ERROR_LINES])


def error_count(log: str) -> int:
    """Compiler errors in the log, or all [ERROR] lines when there are no compiler errors."""
    compiler = len(COMPILER_ERROR.findall(log))
    return compiler or sum(1 for line in log.splitlines() if "[ERROR]" in line and not NOISE.search(line))


# Repair modes, chosen by code from the error type: system prompt, task prompt, and the tools offered.
# None = all tools.
FILE_TOOLS = {"list_files", "read_file", "search_code", "edit_file", "write_file"}
MODES = {
    "code": ("repair_code_system.md", "repair_code_task.md", FILE_TOOLS | {"inspect_class", "read_library_docs"}),
    "pom": ("repair_pom_system.md", "repair_task.md",
            FILE_TOOLS | {"find_versions", "read_artifact_pom", "find_replacement_artifact"}),
    "full": ("repair_system.md", "repair_task.md", None),
    # A missing package: add the dependency that provides it (pom.xml) and/or adjust the imports (Java).
    "dependency": ("repair_dependency_system.md", "repair_dependency_task.md",
                   FILE_TOOLS | {"find_versions", "read_artifact_pom"}),
}
MAX_CANDIDATES = 8


def missing_classes(issue: dict, project_dir: str) -> list[str]:
    """Fully qualified names imported from the missing package in the failing file (from its import lines)."""
    path = project_file(project_dir, issue["error"]["path"])
    if path is None:
        return []
    package = issue["package"]
    names = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if (m := IMPORT.match(line)) and m.group(1).startswith(package + ".") and m.group(1)[len(package) + 1:][:1].isupper():
            names.append(m.group(1))
    return names


def dependency_candidates(issue: dict, project_dir: str) -> str:
    """Artifacts on Maven Central that provide the classes the failing file imports from the missing package."""
    classes = missing_classes(issue, project_dir)
    if not classes:
        return f"(no class from package {issue['package']} imported by name in this file; search found nothing)"
    lines = []
    searched = []
    for fqcn in classes[:3]:
        searched.append(fqcn)
        if fqcn.startswith("javax."):  # the usual replacement after Java EE moved to Jakarta EE
            searched.append("jakarta." + fqcn[len("javax."):])
    for fqcn in searched:
        found = default_source().artifacts_with_class(fqcn)
        if not found:
            lines.append(f"- {fqcn}: no artifact found on Maven Central")
            continue
        lines.append(f"- {fqcn} is provided by (closest match first):")
        lines += [f"    {ga} (versions seen: {', '.join(versions[:3])})" for ga, versions in found[:MAX_CANDIDATES]]
    return "\n".join(lines)


def repair_mode(log: str) -> str:
    """"pom" if Maven can't read the POMs (that blocks compilation, so it comes first), "code" for
    compile errors, "full" for anything else."""
    if PROJECT_POM.search(log) or missing_dependencies(log) or "Non-resolvable" in log:
        return "pom"
    if COMPILE_ERROR.search(log):
        return "code"
    return "full"


def run_repair(llm: BaseChatModel, project_dir: str, task: str, edits: list[str],
               applied: list[AppliedUpgrade], mode: str = "full") -> str:
    """Run one repair attempt in the given mode. Edited file paths are appended to `edits` even if
    the agent fails. Returns the agent's summary."""
    system_prompt, _, allowed = MODES[mode]
    source = default_source()
    tools = [
        *make_read_tools(project_dir),
        *make_edit_tools(project_dir, edits),
        make_find_versions_tool(source),
        *make_library_tools(source),
        *make_migration_tools(source, applied),
    ]
    tools = [_guarded(t) for t in tools if allowed is None or t.name in allowed]
    log.info("repair: %s mode, tools: %s", mode, ", ".join(t.name for t in tools))
    agent = create_agent(model=llm, tools=tools, system_prompt=load_prompt(system_prompt))
    result = agent.invoke({"messages": [{"role": "user", "content": task}]},
                          config={"recursion_limit": RECURSION_LIMIT, "callbacks": [AgentTraceLogger("repair")]})
    content = result["messages"][-1].content
    if isinstance(content, list):  # some providers return content blocks
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content.strip()


def build_task(attempt: int, applied: list[AppliedUpgrade], build_log: str, history: list[dict],
               project_dir: str, mode: str = "full", known_fixes: str = "(none)", candidates: str = "",
               advice: str = "(none)", advice_source: str = "no consultant") -> str:
    upgrades = "\n".join(f"- {u.module}: {u.group}:{u.artifact} {u.old_version} -> {u.new_version}"
                         for u in applied) or "- (none)"
    earlier = "\n".join(
        f"- attempt {h['attempt']}: {h['summary'] or '(no summary)'} "
        f"[files: {', '.join(h['edits']) or 'none'}; errors before: {h['errors_before']}]"
        for h in history
    ) or "- none"
    task_prompt = MODES[mode][1]
    if mode == "dependency":
        return load_prompt(task_prompt, attempt=str(attempt), upgrades=upgrades, errors=error_excerpt(build_log),
                           java_context=java_context(build_log, project_dir), candidates=candidates,
                           known_fixes=known_fixes, advice=advice, advice_source=advice_source, history=earlier)
    if mode == "code":  # no POM sections or dependency hints: nothing that points away from the code
        return load_prompt(task_prompt, attempt=str(attempt), upgrades=upgrades, errors=error_excerpt(build_log),
                           java_context=java_context(build_log, project_dir), known_fixes=known_fixes, advice=advice, advice_source=advice_source,
                           history=earlier)
    return load_prompt(task_prompt, attempt=str(attempt), upgrades=upgrades,
                       errors=error_excerpt(build_log), pom_context=pom_context(build_log, project_dir),
                       java_context=java_context(build_log, project_dir), known_fixes=known_fixes, advice=advice, advice_source=advice_source,
                       hints=migration_hints(default_source(), build_log, applied), history=earlier)
