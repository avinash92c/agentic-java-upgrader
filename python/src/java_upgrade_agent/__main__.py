"""Run the agent: python -m java_upgrade_agent <project-dir> --config config.toml"""

import argparse
import tomllib
from pathlib import Path

from .graph import build_graph
from .llm import LLMSettings, create_chat_model
from .tracing import log, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="java_upgrade_agent", description="Upgrade a Maven project's dependencies.")
    parser.add_argument("project_dir", type=Path, help="folder with the project's root pom.xml")
    parser.add_argument("--config", type=Path, required=True, help="TOML config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug detail: tool calls, dependency lists")
    parser.add_argument("--log-file", type=Path, help="also write the log to this file")
    args = parser.parse_args()
    setup_logging(args.verbose, args.log_file)

    with open(args.config, "rb") as f:
        config = tomllib.load(f)
    settings = LLMSettings(**config["llm"])
    llm = create_chat_model(settings)
    log.info("start: provider %s, model %s", settings.provider, settings.model)
    state = {
        "project_dir": str(args.project_dir.resolve()),
        "max_retries": config.get("agent", {}).get("max_retries", 3),
    }

    final: dict = {}
    run_config = {"configurable": {"llm": llm}, "recursion_limit": 1000}  # one loop pass per dependency
    for mode, chunk in build_graph().stream(state, config=run_config, stream_mode=["updates", "values"]):
        if mode == "updates":
            for node in chunk:
                log.debug("node done: %s", node)
        else:
            final = chunk

    print()
    print(final.get("report", "(no report)"))


if __name__ == "__main__":
    main()
