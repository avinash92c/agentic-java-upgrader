"""Learning from fixes: a fix whose error is gone after the next build is written to migration_hints.yaml,
and later runs give matching hints to the model, so the same error is fixed straight away.

Entry:
  signature: "cannot find symbol | symbol: method toolName(java.lang.String) | location: class org.x.Builder"
  kind: code | pom | other
  library: "org.springframework.ai:spring-ai-google-genai"     (or "unknown")
  versions: {from: "1.1.5", to: "2.0.2"}
  fix: <the model's summary>
  changes: ["Foo.java: `old` -> `new`", ...]
  source: "<provider>/<model>"
  verified: build
  date: 2026-10-24
"""

import re
from datetime import date
from pathlib import Path

import yaml

from .tracing import log

DEFAULT_HINTS_FILE = Path.home() / ".java-upgrade-agent" / "migration_hints.yaml"
MAX_HINTS = 3
PROVIDERS = {"ChatAnthropic": "anthropic", "ChatOpenAI": "openai", "ChatGoogleGenerativeAI": "google",
             "ChatOllama": "ollama"}

PATH = re.compile(r"(?:/?[A-Za-z]:)?[\\/][^\s:\[\]]+")  # file paths in a message
POSITION = re.compile(r"@ line \d+, column \d+|line \d+, column \d+|:\[\d+,\d+\]")
SYMBOL = re.compile(r"symbol:\s*(.+?)(?:\s*\||$)")
LOCATION = re.compile(r"location:\s*(.+?)(?:\s*\||$)")
PACKAGE_MISSING = re.compile(r"package ([\w.]+) does not exist")


def signature(issue: dict) -> str:
    """The error without file paths, line numbers or columns, so the same error matches across files and runs."""
    if "error" in issue:  # code and dependency issues: the compile error's details
        parts = issue["error"]["details"]
    else:
        parts = [issue["text"]]
    cleaned = [" ".join(POSITION.sub("", PATH.sub("", p)).split()) for p in parts]
    return " | ".join(p for p in cleaned if p)


def source_of(llm) -> str:
    name = type(llm).__name__ if llm is not None else "unknown"
    model = getattr(llm, "model", None) or getattr(llm, "model_name", None) or "unknown"
    return f"{PROVIDERS.get(name, name)}/{model}"


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return data if isinstance(data, list) else []
    except (OSError, yaml.YAMLError) as e:
        log.warning("learning: could not read %s: %s", path, e)
        return []


def record(path: Path, entry: dict) -> None:
    """Append a verified fix, unless the same signature already has the same fix."""
    entries = _load(path)
    if any(e.get("signature") == entry["signature"] and e.get("changes") == entry["changes"] for e in entries):
        log.info("learning: fix for '%s' already in %s", entry["signature"], path)
        return
    entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    log.info("learning: recorded fix for '%s' (%s) in %s", entry["signature"], entry["source"], path)


def _keys(sig: str) -> set[str]:
    """What else makes two signatures 'the same error': the symbol with its location, or a missing package."""
    keys = set()
    symbol, location = SYMBOL.search(sig), LOCATION.search(sig)
    if symbol and location:
        keys.add(f"{symbol.group(1)}@{location.group(1)}")
    if m := PACKAGE_MISSING.search(sig):
        keys.add(f"package:{m.group(1)}")
    return keys


def find_hints(path: Path, sig: str) -> list[dict]:
    """Recorded fixes for the same error: same signature first, then same symbol+location or missing package."""
    entries = _load(path)
    exact = [e for e in entries if e.get("signature") == sig]
    keys = _keys(sig)
    related = [e for e in entries if e not in exact and keys & _keys(e.get("signature", ""))]
    return (exact + related)[-MAX_HINTS:][::-1]  # newest first


def format_hints(hints: list[dict]) -> str:
    if not hints:
        return "(none)"
    blocks = []
    for h in hints:
        versions = h.get("versions") or {}
        lines = [f"- error: {h.get('signature')}",
                 f"  library: {h.get('library', 'unknown')} {versions.get('from', '?')} -> {versions.get('to', '?')}",
                 f"  fix: {h.get('fix')}"]
        lines += [f"  change: {c}" for c in h.get("changes", [])]
        lines.append(f"  (source: {h.get('source')}, verified: {h.get('verified')}, {h.get('date')})")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def new_entry(issue: dict, library: str, versions: dict, summary: str, changes: list[str], source: str) -> dict:
    return {"signature": signature(issue), "kind": issue["kind"], "library": library, "versions": versions,
            "fix": " ".join(summary.split())[:600], "changes": changes, "source": source,
            "verified": "build", "date": date.today().isoformat()}
