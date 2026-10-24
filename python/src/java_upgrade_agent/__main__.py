"""Run the agent: python -m java_upgrade_agent <project-dir> --config config.toml"""

import argparse
import tomllib
from datetime import datetime
from pathlib import Path

from .consultant import ConsultantSettings
from .graph import build_graph
from .learning import DEFAULT_HINTS_FILE
from .llm import LLMSettings, create_chat_model
from .tracing import log, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="java_upgrade_agent", description="Upgrade a Maven project's dependencies.")
    parser.add_argument("project_dir", type=Path, help="folder with the project's root pom.xml")
    parser.add_argument("--config", type=Path, required=True, help="TOML config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug detail: tool calls, dependency lists")
    parser.add_argument("--log-file", type=Path, help="also write the log to this file")
    parser.add_argument("--report", type=Path,
                        help="write the report to this file (default: upgrade-report-<timestamp>.md in the current folder)")
    args = parser.parse_args()
    setup_logging(args.verbose, args.log_file)

    with open(args.config, "rb") as f:
        config = tomllib.load(f)
    settings = LLMSettings(**config["llm"])
    llm = create_chat_model(settings)
    log.info("start: provider %s, model %s", settings.provider, settings.model)
    # Optional second model for the repair rounds (e.g. a frontier model); [llm] is used if it's absent.
    repair_llm = None
    if "repair_llm" in config:
        repair_settings = LLMSettings(**config["repair_llm"])
        repair_llm = create_chat_model(repair_settings)
        log.info("start: repair uses provider %s, model %s", repair_settings.provider, repair_settings.model)
    # Optional AI command-line tool (Claude Code, Gemini CLI, Codex CLI, ...) asked for fix advice.
    consultant = None
    if "consultant" in config:
        consultant = ConsultantSettings(**config["consultant"])
        log.info("start: consultant %s (%s), asked %s", consultant.label, " ".join(consultant.args()),
                 "for every issue" if consultant.when == "always" else "when an issue's first try failed")
    hints_file = Path(config.get("agent", {}).get("hints_file") or DEFAULT_HINTS_FILE).expanduser()
    log.info("start: fix hints file %s", hints_file)
    state = {
        "project_dir": str(args.project_dir.resolve()),
        "max_retries": config.get("agent", {}).get("max_retries", 3),
        "hints_file": str(hints_file),
    }

    final: dict = {}
    run_config = {"configurable": {"llm": llm, "repair_llm": repair_llm, "consultant": consultant},
                  "recursion_limit": 1000}  # one loop pass per dependency
    for mode, chunk in build_graph().stream(state, config=run_config, stream_mode=["updates", "values"]):
        if mode == "updates":
            for node in chunk:
                log.debug("node done: %s", node)
        else:
            final = chunk

    report = final.get("report", "(no report)")
    print()
    print(report)
    write_report(report, args.report, args.project_dir.resolve())


def write_report(report: str, requested: Path | None, project_dir: Path) -> None:
    """Save the report. Never inside the project being upgraded: it would show up as a change there."""
    default = Path.cwd() / f"upgrade-report-{datetime.now():%Y%m%d-%H%M%S}.md"
    path = (requested or default).resolve()
    if path.is_relative_to(project_dir):
        log.warning("report: %s is inside the project; writing to %s instead", path, default)
        path = default
    if path.is_relative_to(project_dir):  # the current folder is inside the project too
        path = Path.home() / default.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report + "\n", encoding="utf-8")
    log.info("report: written to %s", path)


if __name__ == "__main__":
    main()
