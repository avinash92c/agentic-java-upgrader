"""Dependency extraction: the LLM reads the POMs through tools and returns a structured list."""

from importlib.resources import files

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from .state import Dependency, DependencyMap
from .tools.files import make_read_tools

MAX_ATTEMPTS = 2  # one retry on a failed or malformed answer


class ModuleDependencies(BaseModel):
    module: str = Field(description="The POM's own <artifactId>")
    dependencies: list[Dependency] = Field(
        description="Parent, BOM imports and versioned dependencies, in that order, with resolved versions"
    )


class ExtractionResult(BaseModel):
    """Upgradable dependencies per POM. A list of modules (not a dict keyed by module name),
    because providers handle fixed fields in structured output far more reliably than free keys."""

    modules: list[ModuleDependencies] = Field(description="Root POM first, then modules in <modules> order")


def load_prompt(name: str) -> str:
    return files("java_upgrade_agent").joinpath("prompts", name).read_text(encoding="utf-8")


def extract_dependencies(llm: BaseChatModel, project_dir: str, pom_paths: list[str]) -> DependencyMap:
    """Run the extraction agent and return {module artifactId: [Dependency, ...]} in the LLM's order.

    Raises the last error if every attempt fails.
    """
    agent = create_agent(
        model=llm,
        tools=make_read_tools(project_dir),
        system_prompt=load_prompt("extract_system.md"),
        response_format=ExtractionResult,
    )
    task = load_prompt("extract_task.md").replace("{pom_paths}", "\n".join(f"- {p}" for p in pom_paths))
    # Room for one read per POM, a few extra lookups, and the final answer.
    run_config = {"recursion_limit": 2 * len(pom_paths) + 12}

    last_error: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            result = agent.invoke({"messages": [{"role": "user", "content": task}]}, config=run_config)
            answer: ExtractionResult = result["structured_response"]
            return {m.module: m.dependencies for m in answer.modules}
        except Exception as e:  # malformed output, provider error, recursion limit: retry once
            last_error = e
    raise last_error
