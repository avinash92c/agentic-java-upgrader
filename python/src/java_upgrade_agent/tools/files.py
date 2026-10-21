"""File tools for the LLM, confined to the project directory."""

import re
from pathlib import Path

from langchain_core.tools import BaseTool, tool

REFUSED_NAMES = {".env", "settings.xml"}  # may hold credentials
SKIPPED_DIRS = {".git", "target", "node_modules", ".idea"}
MAX_FILE_BYTES = 200_000
MAX_LISTED = 500
MAX_MATCHES = 100
SEARCHED_SUFFIXES = {".java", ".kt", ".xml", ".properties", ".yml", ".yaml"}


class _Sandbox:
    def __init__(self, project_dir: str):
        self.root = Path(project_dir).resolve()

    def resolve(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise PermissionError(f"{path} is outside the project")
        if target.name in REFUSED_NAMES:
            raise PermissionError(f"{path} is not readable by the agent")
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."


def make_read_tools(project_dir: str) -> list[BaseTool]:
    """Read-only tools (`list_files`, `read_file`) bound to one project directory."""
    box = _Sandbox(project_dir)

    @tool
    def list_files(path: str = ".", pattern: str = "*") -> list[str] | str:
        """List files under a project directory that match a glob pattern.

        `path` is relative to the project root. `pattern` is a glob such as "*", "*.xml" or
        "**/pom.xml" ("**" searches subdirectories). Build output and VCS folders are skipped.
        """
        try:
            base = box.resolve(path)
        except PermissionError as e:
            return f"error: {e}"
        if not base.is_dir():
            return f"error: {path} is not a directory"
        found = []
        for p in sorted(base.glob(pattern)):
            rel = box.relative(p)
            if p.is_file() and not SKIPPED_DIRS.intersection(rel.split("/")) and p.name not in REFUSED_NAMES:
                found.append(rel)
                if len(found) >= MAX_LISTED:
                    break
        return found

    @tool
    def read_file(path: str) -> str:
        """Read a text file in the project, for example "pom.xml" or "core/pom.xml".

        `path` is relative to the project root.
        """
        try:
            target = box.resolve(path)
        except PermissionError as e:
            return f"error: {e}"
        if not target.is_file():
            return f"error: {path} does not exist"
        if target.stat().st_size > MAX_FILE_BYTES:
            return f"error: {path} is larger than {MAX_FILE_BYTES} bytes"
        return target.read_text(encoding="utf-8", errors="replace")

    return [list_files, read_file]


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def make_edit_tools(project_dir: str, edits: list[str]) -> list[BaseTool]:
    """`search_code`, `edit_file` and `write_file`. Every changed file's path is appended to `edits`."""
    box = _Sandbox(project_dir)

    @tool
    def search_code(pattern: str) -> list[str] | str:
        """Search the project's source and config files (.java, .kt, .xml, .properties, .yml) with a
        regular expression. Returns up to 100 matches as "path:line: text"."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"error: invalid regular expression: {e}"
        matches = []
        for p in sorted(box.root.rglob("*")):
            rel = box.relative(p)
            if (not p.is_file() or p.suffix not in SEARCHED_SUFFIXES or p.name in REFUSED_NAMES
                    or SKIPPED_DIRS.intersection(rel.split("/"))):
                continue
            for number, line in enumerate(_read(p).splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if len(matches) >= MAX_MATCHES:
                        return matches
        return matches

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace one exact occurrence of `old` with `new` in a project file.

        `old` must appear exactly once; include enough surrounding lines to make it unique.
        """
        try:
            target = box.resolve(path)
        except PermissionError as e:
            return f"error: {e}"
        if not target.is_file():
            return f"error: {path} does not exist"
        text = _read(target)
        if text.count(old) == 0 and "\r\n" in text:  # the model writes \n; keep the file's CRLF
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
        if count != 1:
            return f"error: the text to replace appears {count} times in {path}; it must appear exactly once"
        _write(target, text.replace(old, new, 1))
        edits.append(box.relative(target))
        return f"edited {path}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Create a project file, or replace its whole content. Prefer edit_file for small changes."""
        try:
            target = box.resolve(path)
        except PermissionError as e:
            return f"error: {e}"
        target.parent.mkdir(parents=True, exist_ok=True)
        _write(target, content)
        edits.append(box.relative(target))
        return f"wrote {path}"

    return [search_code, edit_file, write_file]
