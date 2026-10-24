"""Compile-error repair: the LLM proposes edits as structured output and code applies them.

Step 1: the LLM gets the errors and the failing Java lines and answers with edits, or "unsure".
Step 2 (the LLM was unsure, no edit applied, or the same errors came back): code builds API notes from
the libraries' sources jars, following the type hierarchy: where the missing member was declared in
the old version (with its javadoc and @deprecated note), and the public methods of the new type and
all its supertypes. The LLM is asked again with those notes.
"""

import re
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from .prompts import load_prompt
from .tracing import AgentTraceLogger, log

LOCAL_REPO = Path.home() / ".m2" / "repository"
MAX_SIMILAR_METHODS = 8
MAX_SHARED_WORD_METHODS = 20
MAX_DOC_CHARS = 800
MAX_HIERARCHY_DEPTH = 4

SYMBOL = re.compile(r"symbol:\s*(method|variable|class|constructor)\s+(\w+)\s*(\([^)]*\))?")
LOCATION_TYPE = re.compile(r"location:\s*(?:class|interface|enum|record|variable \w+ of type)\s+([\w.$<>,? ]+)")
TYPE_DECL = r"\b(class|interface|enum|record)\s+{name}\b([^{{;]*)\{{"
METHOD_DECL = re.compile(r"(\w+)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*(?:throws\s+[\w.,\s]+)?\s*[;{]")
NOT_METHODS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized", "super", "this"}


class Edit(BaseModel):
    file: str = Field(description="The file, as shown in the task (relative to the project)")
    line: int | None = Field(default=None, description="First line to replace, using the line numbers shown in the task")
    end_line: int | None = Field(default=None, description="Last line to replace (same as line for one line)")
    old: str = Field(default="", description="Or, instead of line numbers: the current text to replace")
    new: str = Field(default="", description="The replacement text; empty to delete the line(s)")


class NewDependency(BaseModel):
    groupId: str
    artifactId: str
    version: str = Field(default="", description='e.g. "3.0.0" or "${some.property}"; empty if a parent/BOM manages it')
    scope: str = Field(default="", description='empty for the default (compile) scope, or e.g. "provided"')


class CodeFix(BaseModel):
    status: Literal["fix", "unsure"]
    add_dependencies: list[NewDependency] = Field(
        default_factory=list, description="Dependencies to add to pom.xml; code inserts them in the right place")
    edits: list[Edit] = Field(default_factory=list, description="Text replacements in Java files (or pom.xml)")
    reason: str = Field(description="One or two sentences: what changed in the library and how you fixed it")


def ask(llm: BaseChatModel, task: str) -> CodeFix:
    """One structured-output LLM call. Raises ValueError if the answer can't be used."""
    messages = [{"role": "system", "content": load_prompt("compile_fix_system.md")},
                {"role": "user", "content": task}]
    result = llm.with_structured_output(CodeFix, include_raw=True).invoke(
        messages, config={"callbacks": [AgentTraceLogger("fix")]})
    if result["parsed"] is None:
        raise ValueError(f"the model did not return a structured answer ({result['parsing_error'] or 'empty reply'})")
    return result["parsed"]


def project_file(project_dir: str, path: str) -> Path | None:
    """An existing file of the project for a path as written in a Maven error or by the model. Handles
    paths without a drive letter ("\\Users\\me\\proj\\src\\Foo.java", which Python would resolve on the
    current drive), "/C:/x" forms, and paths relative to the project or to its src/ folder."""
    root = Path(project_dir).resolve()
    text = path.strip().replace("\\", "/")
    if re.match(r"/[A-Za-z]:/", text):
        text = text[1:]
    candidates = [Path(text)]
    if text.startswith("/") and root.drive:  # absolute but no drive: use the project's drive
        candidates.append(Path(root.drive + text))
    candidates.append(root / text.lstrip("/"))
    if "/src/" in "/" + text:  # .../src/main/java/x/Foo.java -> project/src/main/java/x/Foo.java
        candidates.append(root / ("src/" + ("/" + text).split("/src/", 1)[1]))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            return resolved
    return None


def _resolve(project_dir: str, file: str, error_files: list[str]) -> Path | None:
    """The project file an edit refers to: a path relative to the project, an absolute path inside it,
    or a bare file name that matches one of the files in the errors."""
    found = project_file(project_dir, file)
    if found:
        return found
    name = file.strip().replace("\\", "/").split("/")[-1]
    matches = [p for p in (project_file(project_dir, f) for f in error_files) if p and p.name == name]
    return matches[0] if len(matches) == 1 else None


# What a model copies from our excerpts besides the code: ">>   84 | " or "  84 | " at the start of a line.
EXCERPT_PREFIX = re.compile(r"^[ \t]*(?:>>)?[ \t]*\d+[ \t]*\|[ \t]?", re.M)


def _strip_excerpt_prefixes(text: str) -> str:
    lines = text.splitlines()
    if lines and all(EXCERPT_PREFIX.match(line) or not line.strip() for line in lines):
        return EXCERPT_PREFIX.sub("", text)
    return text


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def apply_one(text: str, edit: Edit) -> tuple[str, str | None]:
    """Apply one edit to a file's text. Returns (new text, None) or (unchanged text, problem).

    - Line edit (`line`, optional `end_line`): those lines are replaced by `new`, or deleted if `new` is
      empty. A replacement written without indentation gets the original line's indentation.
    - Text edit (`old`): excerpt prefixes the model copied (">>   84 |") are removed; then an exact match,
      then a match ignoring whitespace differences (tabs vs spaces, line breaks). It must match exactly
      once. If `new` is empty and the match is a whole line, the line is deleted.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    new = _strip_excerpt_prefixes(edit.new).replace("\r\n", "\n")

    if edit.line:
        lines = text.split(newline)
        first, last = edit.line, edit.end_line or edit.line
        if not (1 <= first <= last <= len(lines)):
            return text, f"lines {first}-{last} are outside the file ({len(lines)} lines)"
        replacement = new.split("\n") if new.strip() else []
        if replacement and not _indent(replacement[0]) and _indent(lines[first - 1]):
            base = _indent(lines[first - 1])
            replacement = [base + r if r.strip() and not _indent(r) else r for r in replacement]
        return newline.join(lines[: first - 1] + replacement + lines[last:]), None

    old = _strip_excerpt_prefixes(edit.old)
    if not old.strip():
        return text, "the edit has neither line numbers nor text to replace"
    for candidate_old, candidate_new in ((old, new), (old.replace("\n", newline), new.replace("\n", newline))):
        if text.count(candidate_old) == 1:
            return text.replace(candidate_old, candidate_new, 1), None

    # Ignore whitespace differences: every run of whitespace in `old` matches any run in the file.
    pattern = re.compile(r"\s+".join(re.escape(token) for token in old.split()))
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return text, (f"the text to replace appears {len(matches)} times (must be exactly once); "
                      "give line numbers instead (line/end_line)")
    m = matches[0]
    start, end = m.start(), m.end()
    if not new.strip():  # delete; take the whole line if the match is the whole line
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end == -1 else line_end + 1
        if not text[line_start:start].strip() and not text[end:line_end].strip():
            start, end = line_start, line_end
        return text[:start] + text[end:], None
    return text[:start] + new.strip().replace("\n", newline) + text[end:], None


def apply_edits(project_dir: str, edits: list[Edit], error_files: list[str],
                allow_pom: bool = False) -> tuple[list[str], list[str]]:
    """Apply each edit if its `old` text appears exactly once. Files outside the project are refused, and
    pom.xml unless `allow_pom`. Returns (relative paths edited, problems)."""
    root = Path(project_dir).resolve()
    edited, problems = [], []
    for edit in edits:
        path = _resolve(project_dir, edit.file, error_files)
        if path is None:
            problems.append(f"{edit.file}: file not found in the project")
            continue
        if path.name == "pom.xml" and not allow_pom:
            problems.append(f"{edit.file}: pom.xml is not changed in a compile fix; use add_dependencies")
            continue
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
            updated, problem = apply_one(text, edit)
            if problem:
                problems.append(f"{edit.file}: {problem}")
                continue
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(updated)
        except OSError as e:  # never let one edit end the run
            problems.append(f"{edit.file}: {e}")
            continue
        rel = path.relative_to(root).as_posix()
        edited.append(rel)
        log.info("fix: edited %s", rel)
    return sorted(set(edited)), problems


# --- reading library sources ----------------------------------------------------------------

_JAR_NAMES: dict[Path, set[str]] = {}  # sources jar -> its file names, read once per run


def _sources_jar(coordinate: str, version: str) -> Path:
    group, artifact = coordinate.split(":")
    return LOCAL_REPO / group.replace(".", "/") / artifact / version / f"{artifact}-{version}-sources.jar"


def _jar_has(jar: Path, member: str) -> bool:
    if jar not in _JAR_NAMES:
        try:
            with zipfile.ZipFile(jar) as z:
                _JAR_NAMES[jar] = set(z.namelist())
        except (OSError, zipfile.BadZipFile):
            _JAR_NAMES[jar] = set()
    return member in _JAR_NAMES[jar]


def _read_member(jar: Path, member: str) -> str | None:
    if not jar.is_file():
        return None
    try:
        with zipfile.ZipFile(jar) as z:
            return z.read(member).decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return None


def _find_artifact(member: str, sources: dict[str, str]) -> str | None:
    """The group:artifact whose sources jar contains `member` (it may be another artifact than the
    one the error came from, e.g. a supertype in spring-ai-model). Groups sharing the package's first
    two segments are tried first."""
    package_prefix = ".".join(member.split("/")[:2])
    for coordinate in sorted(sources, key=lambda ga: not ga.startswith(package_prefix)):
        jar = _sources_jar(coordinate, sources[coordinate])
        if jar.is_file() and _jar_has(jar, member):
            return coordinate
    return None


# --- parsing type declarations ----------------------------------------------------------------

def _erase(type_name: str) -> str:
    """Remove generic arguments: Foo.Builder<Bar<Baz>> -> Foo.Builder."""
    out, depth = [], 0
    for ch in type_name:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()


def _split_types(text: str) -> list[str]:
    """A comma-separated type list (commas inside generics ignored), generics erased."""
    items, depth, current = [], 0, []
    for ch in text:
        depth += (ch == "<") - (ch == ">")
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    return [_erase(item) for item in items if item.strip()]


def _matching_brace(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            return i
    return len(text) - 1


def _shallow(text: str, start: int, end: int) -> str:
    """text[start:end] with everything inside nested braces blanked; same length, so positions match."""
    out, depth = [], 0
    for ch in text[start:end]:
        if ch == "}":
            depth -= 1
        out.append(ch if depth == 0 or ch == "\n" else " ")
        if ch == "{":
            depth += 1
    return "".join(out)


def _split_fqcn(fqcn: str) -> tuple[str, str, list[str]] | None:
    """org.x.Foo.Builder -> ("org.x", "org/x/Foo.java", ["Foo", "Builder"])."""
    parts = _erase(fqcn).replace("$", ".").split(".")
    for i, part in enumerate(parts):
        if part[:1].isupper():
            return ".".join(parts[:i]), "/".join(parts[: i + 1]) + ".java", parts[i:]
    return None


class _Type:
    """A type declaration found in a dependency's sources jar."""

    def __init__(self, fqcn: str, coordinate: str, version: str, source: str, body: tuple[int, int],
                 kind: str, supertypes: list[list[str]]):
        self.fqcn, self.coordinate, self.version = fqcn, coordinate, version
        self.source, self.body, self.kind = source, body, kind
        self.supertypes = supertypes  # per supertype: the full names it could resolve to, best first

    @property
    def simple(self) -> str:
        return self.fqcn.rsplit(".", 1)[-1]


def _load_type(fqcn: str, sources: dict[str, str]) -> _Type | None:
    """Find the declaration of `fqcn` (nested types included) in the dependencies' sources jars and
    read its extends/implements clauses."""
    split = _split_fqcn(fqcn)
    if split is None:
        return None
    package, member, chain = split
    coordinate = _find_artifact(member, sources)
    if coordinate is None:
        return None
    source = _read_member(_sources_jar(coordinate, sources[coordinate]), member)
    if source is None:
        return None

    start, end, kind, header = 0, len(source), "class", ""
    for name in chain:  # Foo in the file, then Builder inside Foo's body, ...
        m = re.search(TYPE_DECL.format(name=re.escape(name)), _shallow(source, start, end), re.S)
        if m is None:
            return None
        open_at = start + m.end() - 1
        kind, header = m.group(1), m.group(2)
        start, end = open_at + 1, _matching_brace(source, open_at)

    header = header.strip()
    if header.startswith("<"):  # the declared type's own type parameters
        header = header[_matching_angle(header) + 1:]
    extends = re.search(r"\bextends\s+(.+?)(?=\bimplements\b|$)", header, re.S)
    implements = re.search(r"\bimplements\s+(.+)$", header, re.S)
    names = (_split_types(extends.group(1)) if extends else []) + (_split_types(implements.group(1)) if implements else [])

    imports = {imp.rsplit(".", 1)[-1]: imp for imp in re.findall(r"^\s*import\s+([\w.]+)\s*;", source, re.M)}
    wildcards = re.findall(r"^\s*import\s+([\w.]+)\.\*\s*;", source, re.M)
    supertypes = []
    for name in names:
        first, _, rest = name.partition(".")
        if first[:1].islower():  # already fully qualified
            supertypes.append([name])
        elif first in imports:
            supertypes.append([imports[first] + ("." + rest if rest else "")])
        else:  # nested in this file, same package, or from a wildcard import
            outer = f"{package}.{chain[0]}" if package else chain[0]
            supertypes.append([f"{outer}.{name}", f"{package}.{name}", *[f"{w}.{name}" for w in wildcards]])
    full = ".".join([package, *chain]) if package else ".".join(chain)
    return _Type(full, coordinate, sources[coordinate], source, (start, end), kind, supertypes)


def _matching_angle(text: str) -> int:
    depth = 0
    for i, ch in enumerate(text):
        depth += (ch == "<") - (ch == ">")
        if depth == 0:
            return i
    return len(text) - 1


def _hierarchy(fqcn: str, sources: dict[str, str]) -> list[_Type]:
    """The type, its outer type (for nested types), and their supertypes, up to MAX_HIERARCHY_DEPTH
    levels, across all the dependencies' sources jars."""
    split = _split_fqcn(fqcn)
    start = [[fqcn]]
    if split and len(split[2]) > 1:
        start.append([f"{split[0]}.{split[2][0]}" if split[0] else split[2][0]])
    found: list[_Type] = []
    seen: set[str] = set()
    queue = [(candidates, 0) for candidates in start]
    while queue:
        candidates, depth = queue.pop(0)
        for candidate in candidates:
            if candidate in seen or candidate.startswith(("java.", "javax.")):
                break
            loaded = _load_type(candidate, sources)
            if loaded is None:
                continue
            seen.add(candidate)
            found.append(loaded)
            if depth < MAX_HIERARCHY_DEPTH:
                queue += [(s, depth + 1) for s in loaded.supertypes]
            break
    return found


def _methods(t: _Type) -> list[tuple[str, str, int]]:
    """(name, parameters, position in the source) of the public methods declared directly in the type."""
    start, end = t.body
    shallow = _shallow(t.source, start, end)
    methods = []
    for m in METHOD_DECL.finditer(shallow):
        name = m.group(1)
        cut = max(shallow.rfind(";", 0, m.start()), shallow.rfind("}", 0, m.start()), shallow.rfind("{", 0, m.start()))
        prefix = re.sub(r"/\*.*?\*/|//[^\n]*|@[\w.]+(\([^)]*\))?", " ", shallow[cut + 1: m.start()], flags=re.S)
        if name in NOT_METHODS or name == t.simple or "=" in prefix or not prefix.strip() or "private" in prefix:
            continue
        if t.kind != "interface" and not re.search(r"\b(public|protected)\b", prefix):
            continue
        methods.append((name, " ".join(m.group(2).split()), start + m.start()))
    return methods


def _javadoc_at(source: str, position: int) -> str:
    """Cleaned javadoc directly attached to the declaration at `position`, the @deprecated note first."""
    before = source[:position]
    start, end = before.rfind("/**"), before.rfind("*/")
    if start == -1 or end < start or re.search(r"[;{}]", before[end + 2:]):
        return ""
    doc = " ".join(re.sub(r"^\s*\*\s?", "", before[start + 3: end], flags=re.M).split())
    deprecated = re.search(r"@deprecated\s+(.*?)(?=\s@\w+|$)", doc)
    return ((f"DEPRECATED: {deprecated.group(1)} | " if deprecated else "") + doc)[:MAX_DOC_CHARS]


def _words(name: str) -> set[str]:
    """camelCase words, singular: toolNames -> {tool, name}."""
    words = re.findall(r"[a-z]+|[A-Z][a-z]*|\d+", name)
    return {w.lower()[:-1] if w.lower().endswith("s") and len(w) > 3 else w.lower() for w in words}


# --- API notes ---------------------------------------------------------------------------------

def api_notes(errors: list[dict], old_sources: dict[str, str], new_sources: dict[str, str]) -> str:
    """For each missing symbol: where it was declared in the old version's type hierarchy (with its
    javadoc and @deprecated note), and the public methods of the new type and all its supertypes."""
    notes, seen = [], set()
    for error in errors:
        text = " ".join(error["details"])
        symbol, location = SYMBOL.search(text), LOCATION_TYPE.search(text)
        if not (symbol and location):
            continue
        name, fqcn = symbol.group(2), _erase(location.group(1))
        if (name, fqcn) in seen:
            continue
        seen.add((name, fqcn))
        lines = [f"### {symbol.group(1)} {name}{symbol.group(3) or ''} on {fqcn}"]

        old_types = _hierarchy(fqcn, old_sources) if old_sources else []
        declared = [(t, pos) for t in old_types for n, _, pos in _methods(t) if n == name]
        for t, pos in declared[:3]:
            doc = _javadoc_at(t.source, pos)
            lines.append(f"In the old version, {name} was declared in {t.fqcn} ({t.coordinate} {t.version})"
                         + (f": {doc}" if doc else " (no javadoc)."))
        if old_types and not declared:
            lines.append(f"In the old version, {name} was not found in {fqcn} or its supertypes.")

        new_types = _hierarchy(fqcn, new_sources)
        if not new_types:
            lines.append("No sources found for the new version of this type.")
            notes.append("\n".join(lines))
            continue
        lines.append("New type hierarchy: " + " -> ".join(f"{t.fqcn} ({t.coordinate} {t.version})" for t in new_types))
        methods = list(dict.fromkeys(f"{n}({p}) [declared in {t.fqcn}]" for t in new_types for n, p, _ in _methods(t)))
        same = [m for m in methods if m.startswith(name + "(")]
        lines.append(f"In the new version {name} " + ("still exists: " + "; ".join(same) if same
                                                      else f"no longer exists in {fqcn} or its supertypes."))
        shared = [m for m in methods if _words(m.split("(")[0]) & _words(name)]
        if shared:
            lines.append(f"Methods sharing a word with {name} (any type in the hierarchy): "
                         + "; ".join(shared[:MAX_SHARED_WORD_METHODS]))
        similar = sorted(methods, key=lambda m: SequenceMatcher(None, name.lower(), m.split("(")[0].lower()).ratio(),
                         reverse=True)[:MAX_SIMILAR_METHODS]
        lines.append("Methods with similar names (may be unrelated; a similar name does not mean the same purpose): "
                     + "; ".join(similar))
        notes.append("\n".join(lines))
        log.info("fix: API notes for %s.%s: old hierarchy %d type(s), new hierarchy %d type(s): %s", fqcn, name,
                 len(old_types), len(new_types), ", ".join(t.simple for t in new_types))
    return "\n\n".join(notes) or "(no library sources found for the missing symbols)"
