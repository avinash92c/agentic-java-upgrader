"""Build repair: a tool-calling agent that edits the project until the errors are fixed."""

import re

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel

from .prompts import load_prompt
from .state import AppliedUpgrade
from .tools.files import make_edit_tools, make_read_tools
from .tools.library import make_library_tools
from .tools.repo import default_source, make_find_versions_tool
from .tracing import AgentTraceLogger

RECURSION_LIMIT = 60  # roughly 30 tool calls per attempt
MAX_ERROR_LINES = 150
NOISE = re.compile(r"\[ERROR\]\s*(-> \[Help|To see the full|Re-run Maven|For more information|$)")
COMPILER_ERROR = re.compile(r"\[ERROR\] .+\.(java|kt):\[\d+")


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


def run_repair(llm: BaseChatModel, project_dir: str, task: str, edits: list[str]) -> str:
    """Run one repair attempt. Edited file paths are appended to `edits` even if the agent fails.
    Returns the agent's summary."""
    source = default_source()
    tools = [
        *make_read_tools(project_dir),
        *make_edit_tools(project_dir, edits),
        make_find_versions_tool(source),
        *make_library_tools(source),
    ]
    agent = create_agent(model=llm, tools=tools, system_prompt=load_prompt("repair_system.md"))
    result = agent.invoke({"messages": [{"role": "user", "content": task}]},
                          config={"recursion_limit": RECURSION_LIMIT, "callbacks": [AgentTraceLogger("repair")]})
    content = result["messages"][-1].content
    if isinstance(content, list):  # some providers return content blocks
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content.strip()


def build_task(attempt: int, applied: list[AppliedUpgrade], build_log: str, history: list[dict]) -> str:
    upgrades = "\n".join(f"- {u.module}: {u.group}:{u.artifact} {u.old_version} -> {u.new_version}"
                         for u in applied) or "- (none)"
    earlier = "\n".join(
        f"- attempt {h['attempt']}: {h['summary'] or '(no summary)'} "
        f"[files: {', '.join(h['edits']) or 'none'}; errors before: {h['errors_before']}]"
        for h in history
    ) or "- none"
    return load_prompt("repair_task.md", attempt=str(attempt), upgrades=upgrades,
                       errors=error_excerpt(build_log), history=earlier)
