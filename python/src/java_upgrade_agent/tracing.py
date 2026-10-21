"""Logging setup and a callback that traces an agent's LLM calls and tool calls.

With -v (DEBUG) every LLM request is logged in full (each message the model receives, including
system prompt and tool results), along with each response and each tool call's full input and output.
"""

import json
import logging
import sys
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger("java_upgrade_agent")


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    # LLM text can contain characters the Windows console encoding can't show; replace them
    # instead of failing the log line.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    log.handlers = handlers
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False


def _text(value) -> str:
    """Full text of a message content, tool input or tool output."""
    value = getattr(value, "content", value)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _indent(text: str) -> str:
    return "\n".join("      " + line for line in text.splitlines()) or "      (empty)"


class AgentTraceLogger(BaseCallbackHandler):
    """Logs (DEBUG) every LLM request and response and every tool call of one agent step."""

    def __init__(self, step: str):
        self.step = step
        self.calls = 0

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.calls += 1
        params = kwargs.get("invocation_params") or {}
        tools = [t.get("function", t).get("name", "?") for t in params.get("tools") or [] if isinstance(t, dict)]
        batch = messages[0] if messages else []
        log.debug("[%s] LLM request #%d: %d message(s); tools offered: %s",
                  self.step, self.calls, len(batch), ", ".join(tools) or "none")
        for message in batch:
            log.debug("[%s]   %s message (%d chars):\n%s", self.step, message.type, len(_text(message)),
                      _indent(_text(message)))
            for call in getattr(message, "tool_calls", None) or []:
                log.debug("[%s]     earlier tool call %s(%s)", self.step, call["name"], _text(call["args"]))

    def on_llm_end(self, response, **kwargs):
        for generation in (response.generations[0] if response.generations else []):
            message = getattr(generation, "message", None)
            content = _text(message) if message is not None else generation.text
            meta = getattr(message, "response_metadata", {}) or {}
            stop = meta.get("done_reason") or meta.get("finish_reason") or meta.get("stop_reason") or "-"
            usage = getattr(message, "usage_metadata", None) or {}
            log.debug("[%s] LLM response #%d (stop: %s; tokens in %s, out %s; content %d chars):\n%s",
                      self.step, self.calls, stop, usage.get("input_tokens", "?"), usage.get("output_tokens", "?"),
                      len(content), _indent(content))
            extra = getattr(message, "additional_kwargs", None) or {}
            thinking = extra.get("reasoning_content") or extra.get("thinking")
            if thinking:
                log.debug("[%s]   thinking (%d chars):\n%s", self.step, len(thinking), _indent(thinking))
            other = {k: v for k, v in extra.items() if k not in ("reasoning_content", "thinking", "tool_calls")}
            if other:
                log.debug("[%s]   additional_kwargs: %s", self.step, _text(other))
            for call in getattr(message, "tool_calls", None) or []:
                log.debug("[%s]   requests tool %s(%s)", self.step, call["name"], _text(call["args"]))
            log.debug("[%s]   raw response_metadata: %s", self.step, _text(meta))

    def on_llm_error(self, error, **kwargs):
        log.debug("[%s] LLM error: %s", self.step, error)

    def on_tool_start(self, serialized, input_str, **kwargs):
        name = (serialized or {}).get("name", "tool")
        log.debug("[%s] tool %s input: %s", self.step, name, _text(kwargs.get("inputs") or input_str))

    def on_tool_end(self, output, **kwargs):
        text = _text(output)
        log.debug("[%s] tool output (%d chars):\n%s", self.step, len(text), _indent(text))

    def on_tool_error(self, error, **kwargs):
        log.debug("[%s] tool error: %s", self.step, error)
