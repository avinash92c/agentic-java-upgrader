"""Consultant: an AI command-line tool the user already has (Claude Code, Gemini CLI, Codex CLI or any
other) is asked how to fix one build issue. It runs read-only in the project folder (it may read files
and search the web) and only gives advice; the agent's own model applies the fix.

The prompt is sent on stdin, not as an argument: on Windows these CLIs are often .cmd wrappers, and
cmd.exe mangles long arguments with line breaks, quotes or %.
"""

import shutil
import subprocess
import time

from pydantic import BaseModel, Field

from .tracing import log

# Read-only, non-interactive invocations. Check them against your CLI's version (`<cli> --help`);
# use `command` in the config to override.
PRESETS = {
    "claude": ["claude", "-p", "--output-format", "text", "--max-turns", "10",
               "--allowedTools", "Read,Grep,Glob,WebSearch,WebFetch"],
    "gemini": ["gemini"],  # reads the prompt from stdin, answers and exits
    "codex": ["codex", "exec", "--sandbox", "read-only", "-"],  # "-": prompt from stdin
}
MAX_ADVICE_CHARS = 6000


class ConsultantSettings(BaseModel):
    """The [consultant] config section."""

    preset: str | None = Field(default=None, description="claude | gemini | codex")
    command: list[str] | None = Field(default=None, description="full command; overrides the preset")
    name: str | None = None  # shown in logs, the report and the hints file
    when: str = "always"  # "always" (every repair round) | "after_first_try" (only once [llm] alone failed)
    timeout: float = 300

    def args(self) -> list[str]:
        if self.command:
            return list(self.command)
        if self.preset in PRESETS:
            return list(PRESETS[self.preset])
        raise ValueError(f"[consultant] needs `command`, or `preset` = one of {', '.join(PRESETS)}")

    @property
    def label(self) -> str:
        return self.name or self.preset or self.args()[0]


def should_ask(settings: ConsultantSettings | None, try_number: int) -> bool:
    if settings is None:
        return False
    return settings.when == "always" or try_number > 1


def ask(settings: ConsultantSettings, prompt: str, project_dir: str) -> str | None:
    """Run the CLI with the prompt on stdin, in the project folder. Returns its answer, or None."""
    args = settings.args()
    args[0] = shutil.which(args[0]) or args[0]  # resolves claude.cmd / gemini.cmd on Windows
    log.info("consultant: asking %s (%s), up to %.0fs", settings.label, " ".join(settings.args()), settings.timeout)
    started = time.monotonic()
    try:
        result = subprocess.run(args, input=prompt, cwd=project_dir, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=settings.timeout)
    except FileNotFoundError:
        log.warning("consultant: %s not found; set [consultant] command to the full path", settings.args()[0])
        return None
    except subprocess.TimeoutExpired:
        log.warning("consultant: %s gave no answer within %.0fs", settings.label, settings.timeout)
        return None
    answer = result.stdout.strip()
    if result.returncode != 0 or not answer:
        log.warning("consultant: %s failed (exit %d): %s", settings.label, result.returncode,
                    " ".join((result.stderr or answer or "no output").split())[:300])
        return None
    log.info("consultant: %s answered in %.0fs (%d chars):\n%s", settings.label, time.monotonic() - started,
             len(answer), "\n".join("    " + line for line in answer[:MAX_ADVICE_CHARS].splitlines()))
    return answer[:MAX_ADVICE_CHARS]
