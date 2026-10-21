"""Prompt files shipped in the package's prompts/ folder."""

from importlib.resources import files

from .tracing import log


def load_prompt(name: str, **fields: str) -> str:
    """Read prompts/<name> and fill its {placeholder} fields."""
    source = files("java_upgrade_agent").joinpath("prompts", name)
    text = source.read_text(encoding="utf-8")
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    log.debug("prompt: %s (%s)%s", name, source,
              f" filled: {', '.join(f'{k}={v[:80]!r}' for k, v in fields.items())}" if fields else "")
    return text
