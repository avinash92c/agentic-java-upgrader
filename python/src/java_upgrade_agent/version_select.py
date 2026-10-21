"""Version selection: code fetches the published versions, one LLM call picks the newest stable one."""

import json
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from .prompts import load_prompt
from .state import Dependency
from .tools.repo import default_source
from .tracing import AgentTraceLogger, log

# Versions shown to the LLM: the last N in Maven Central's list, which is roughly oldest to newest.
PROMPT_VERSIONS = 25


class Selection(BaseModel):
    status: Literal["upgrade", "up_to_date"]
    new_version: str | None = Field(default=None, description="The chosen version; only when status is upgrade")
    reason: str = Field(description="One short sentence")


def select_version(llm: BaseChatModel, dependency: Dependency) -> Selection:
    """Raises VersionLookupError if Maven Central can't be read, ValueError if the answer is unusable."""
    versions = default_source().get_versions(dependency.group, dependency.artifact)  # cached per run
    recent = versions[-PROMPT_VERSIONS:]
    log.debug("[select] %s:%s: sending the last %d of %d versions to the LLM",
              dependency.group, dependency.artifact, len(recent), len(versions))

    messages = [
        {"role": "system", "content": load_prompt("select_system.md")},
        {"role": "user", "content": load_prompt(
            "select_task.md", group=dependency.group, artifact=dependency.artifact,
            version=dependency.version, versions=json.dumps(recent),
        )},
    ]
    result = llm.with_structured_output(Selection, include_raw=True).invoke(
        messages, config={"callbacks": [AgentTraceLogger("select")]}
    )
    selection, error = result["parsed"], result["parsing_error"]
    if selection is None:
        raw = getattr(result["raw"], "content", result["raw"])
        log.debug("[select] could not parse the answer (%s); raw content: %r", error, raw)
        raise ValueError(f"the model did not return a structured answer ({error or 'empty reply'})")

    # A model can return a version that doesn't exist: only accept published ones.
    if selection.status == "upgrade" and selection.new_version not in versions:
        raise ValueError(f"the model chose {selection.new_version}, which is not a published version")
    return selection
