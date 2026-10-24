"""Reading the project's POMs, collecting versioned dependencies, and editing a version in place.

Files are read and written with newline="" so line endings and all other formatting are kept:
an edit replaces only the characters of one version value.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..state import Dependency, DependencyMap

PROPERTY = re.compile(r"\$\{([^}]+)\}")
SELF_VERSIONS = {"${project.version}", "${version}", "${pom.version}"}  # the project's own modules
BLOCK = re.compile(r"<(parent|dependency)>(.*?)</\1>", re.S)


def _tag(name: str) -> re.Pattern:
    return re.compile(rf"<{re.escape(name)}>\s*(.*?)\s*</{re.escape(name)}>", re.S)


def _text(element: ET.Element, tag: str) -> str | None:
    """Stripped text of a direct child, in any POM namespace (or none)."""
    child = element.find(f"{{*}}{tag}")
    return child.text.strip() if child is not None and child.text and child.text.strip() else None


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def read_poms(project_dir: str) -> dict[str, str]:
    """Read the root pom.xml and, for a multi-module project, each module's pom.xml.

    Standard layout: the root pom.xml lists module directory names under <modules>, and each
    module directory has its own pom.xml (nested modules are followed the same way).
    Returns {relative path of the pom.xml: file content}. Raises FileNotFoundError if one is missing.
    """
    root = Path(project_dir)
    poms: dict[str, str] = {}

    def read(module_dir: Path) -> None:
        pom = module_dir / "pom.xml"
        if not pom.is_file():
            raise FileNotFoundError(f"no pom.xml in {module_dir.relative_to(root).as_posix() or '.'}")
        text = _read(pom)
        poms[pom.relative_to(root).as_posix()] = text
        modules = ET.fromstring(text).find("{*}modules")
        for module in modules if modules is not None else []:
            if module.text and module.text.strip():
                read(module_dir / module.text.strip())

    read(root)
    return poms


@dataclass
class Location:
    """Where one version value is written: the characters [start, end) of a POM file."""

    path: str  # POM path relative to the project
    start: int
    end: int
    property: str | None  # set when the version comes from ${property}

    def key(self, group: str, artifact: str) -> str:
        """Identity of the location; two dependencies sharing a property share a key."""
        return f"{self.path}:${{{self.property}}}" if self.property else f"{self.path}:{group}:{artifact}"


class ProjectPoms:
    """The project's POMs as they are on disk now."""

    def __init__(self, project_dir: str, texts: dict[str, str]):
        self.dir = Path(project_dir)
        self.texts = texts
        self.trees = {path: ET.fromstring(text) for path, text in texts.items()}
        self.path_of = {_text(root, "artifactId"): path for path, root in self.trees.items()}
        self._props: dict[str, dict[str, str]] = {}

    @classmethod
    def load(cls, project_dir: str) -> "ProjectPoms":
        return cls(project_dir, read_poms(project_dir))

    def parent_path(self, path: str) -> str | None:
        """Path of this POM's parent if the parent is part of the project."""
        parent = self.trees[path].find("{*}parent")
        return self.path_of.get(_text(parent, "artifactId")) if parent is not None else None

    def properties(self, path: str) -> dict[str, str]:
        """<properties> of the POM, inherited from parent POMs inside the project."""
        if path not in self._props:
            self._props[path] = {}  # guards against a parent cycle
            root = self.trees[path]
            parent_path = self.parent_path(path)
            props = dict(self.properties(parent_path)) if parent_path else {}
            parent = root.find("{*}parent")
            group = _text(root, "groupId") or (_text(parent, "groupId") if parent is not None else None)
            if group:
                props["project.groupId"] = group
            section = root.find("{*}properties")
            for prop in section if section is not None else []:
                if isinstance(prop.tag, str) and prop.text is not None:
                    props[prop.tag.split("}")[-1]] = prop.text.strip()
            self._props[path] = props
        return self._props[path]

    def resolve(self, value: str | None, path: str) -> str | None:
        """Substitute ${...} properties; None if something can't be resolved inside the project."""
        props = self.properties(path)
        for _ in range(10):  # properties can refer to other properties
            if not value or "${" not in value:
                return value
            resolved = PROPERTY.sub(lambda m: props.get(m.group(1), m.group(0)), value)
            if resolved == value:
                return None
            value = resolved
        return None

    def collect(self) -> DependencyMap:
        """Versioned dependencies per POM: {POM artifactId: [parent, BOM imports, other versioned deps]}.

        Left out: dependencies without a version, the project's own modules, unresolvable versions,
        plugins and anything under <profiles> (only top-level sections are read).
        """
        own = set(self.path_of)
        result: DependencyMap = {}
        for path, root in self.trees.items():
            found: list[Dependency] = []
            seen: set[tuple[str, str]] = set()

            def add(entry: ET.Element) -> None:
                raw_version = _text(entry, "version")
                artifact = _text(entry, "artifactId")
                if not raw_version or raw_version in SELF_VERSIONS or artifact in own:
                    return
                group, version = self.resolve(_text(entry, "groupId"), path), self.resolve(raw_version, path)
                if group and artifact and version and (group, artifact) not in seen:
                    seen.add((group, artifact))
                    found.append(Dependency(group=group, artifact=artifact, version=version))

            parent = root.find("{*}parent")
            if parent is not None:
                add(parent)
            managed = root.findall("{*}dependencyManagement/{*}dependencies/{*}dependency")
            for dep in managed:  # BOM imports first
                if _text(dep, "scope") == "import":
                    add(dep)
            for dep in managed:
                if _text(dep, "scope") != "import":
                    add(dep)
            for dep in root.findall("{*}dependencies/{*}dependency"):
                add(dep)

            result[_text(root, "artifactId") or path] = found
        return result

    def locate(self, module: str, group: str, artifact: str) -> Location | None:
        """Find where `module`'s version of group:artifact is written: the <version> of its
        <parent>/<dependency> entry, or the ${property} it uses (in the module or a parent POM)."""
        path = self.path_of.get(module)
        if path is None:
            return None
        text = self.texts[path]
        for block in BLOCK.finditer(text):
            body, body_start = block.group(2), block.start(2)
            a, g, v = _tag("artifactId").search(body), _tag("groupId").search(body), _tag("version").search(body)
            if not (a and g and v) or a.group(1) != artifact or self.resolve(g.group(1), path) != group:
                continue
            prop = PROPERTY.fullmatch(v.group(1))
            if prop is None:
                return Location(path, body_start + v.start(1), body_start + v.end(1), None)
            return self._locate_property(prop.group(1), path)
        return None

    def _locate_property(self, name: str, path: str | None) -> Location | None:
        while path is not None:  # the module first, then its parents inside the project
            text = self.texts[path]
            section = _tag("properties").search(text)
            if section:
                value = _tag(name).search(section.group(1))
                if value:
                    offset = section.start(1)
                    return Location(path, offset + value.start(1), offset + value.end(1), name)
            path = self.parent_path(path)
        return None

    def rename_artifact(self, group: str, old: str, new: str) -> list[str]:
        """Replace <artifactId>old</artifactId> with `new` in every <dependency> of `group`.
        Only those characters change. Returns the paths of the POMs that were edited."""
        edited = []
        for path in list(self.texts):
            text = self.texts[path]
            blocks = [b for b in BLOCK.finditer(text) if b.group(1) == "dependency"]
            for block in reversed(blocks):  # from the end, so earlier offsets stay valid
                body, body_start = block.group(2), block.start(2)
                a, g = _tag("artifactId").search(body), _tag("groupId").search(body)
                if a and g and a.group(1) == old and self.resolve(g.group(1), path) == group:
                    text = text[: body_start + a.start(1)] + new + text[body_start + a.end(1):]
            if text != self.texts[path]:
                with open(self.dir / path, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                self.texts[path] = text
                edited.append(path)
        return edited

    def pom_for(self, java_file: Path) -> str:
        """The POM of the module a source file belongs to (the nearest pom.xml above it in the project)."""
        root = self.dir.resolve()
        folder = java_file.resolve().parent
        while folder.is_relative_to(root):
            rel = (folder / "pom.xml").relative_to(root).as_posix()
            if rel in self.texts:
                return rel
            if folder == root:
                break
            folder = folder.parent
        return "pom.xml"

    def add_dependency(self, path: str, group: str, artifact: str, version: str = "", scope: str = "") -> str:
        """Add a <dependency> to the POM's project-level <dependencies> (created if missing), using the file's
        own indentation; nothing else in the file changes. Returns what was done, for logs and reports."""
        root = ET.fromstring(self.texts[path])  # the current text: earlier additions count too
        for dep in root.findall("{*}dependencies/{*}dependency"):
            if self.resolve(_text(dep, "groupId"), path) == group and _text(dep, "artifactId") == artifact:
                return f"{group}:{artifact} is already declared in {path}"
        text = self.texts[path]
        newline = "\r\n" if "\r\n" in text else "\n"
        # Spans where a <dependencies> element is not the project-level one.
        nested = [m.span() for m in re.finditer(r"<(dependencyManagement|profiles|build|reporting)\b.*?</\1>", text, re.S)]
        block = next((m for m in re.finditer(r"<dependencies>(.*?)</dependencies>", text, re.S)
                      if not any(s <= m.start() < e for s, e in nested)), None)

        def indent_of(position: int) -> str:
            line_start = text.rfind("\n", 0, position) + 1
            return re.match(r"[ \t]*", text[line_start:]).group(0)

        unit = "\t" if "\n\t<" in text else "    "
        if block is not None:
            close = block.end() - len("</dependencies>")
            outer = indent_of(close)
        else:
            close = text.rindex("</project>")
            outer = indent_of(close) + unit
        inner, field = outer + unit, outer + unit + unit
        lines = [f"{inner}<dependency>", f"{field}<groupId>{group}</groupId>", f"{field}<artifactId>{artifact}</artifactId>"]
        if version:
            lines.append(f"{field}<version>{version}</version>")
        if scope:
            lines.append(f"{field}<scope>{scope}</scope>")
        lines.append(f"{inner}</dependency>")
        snippet = newline.join(lines)
        if block is not None:  # before </dependencies>, after the last entry
            line_start = text.rfind("\n", 0, close) + 1
            text = text[:line_start] + snippet + newline + text[line_start:]
        else:  # a new <dependencies> section before </project>
            line_start = text.rfind("\n", 0, close) + 1
            section = f"{outer}<dependencies>{newline}{snippet}{newline}{outer}</dependencies>{newline}"
            text = text[:line_start] + section + text[line_start:]
        with open(self.dir / path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.texts[path] = text
        self.trees[path] = ET.fromstring(text)
        coordinates =":".join(x for x in (group, artifact, version) if x) + (f" ({scope})" if scope else "")
        return f"added dependency {coordinates} to {path}"

    def write(self, location: Location, value: str) -> None:
        text = self.texts[location.path]
        text = text[: location.start] + value + text[location.end :]
        with open(self.dir / location.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.texts[location.path] = text
