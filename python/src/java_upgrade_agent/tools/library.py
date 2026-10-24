"""Library inspection tools: an artifact's POM (managed dependencies), and its real API from the JAR and sources JAR."""

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from .repo import MavenCentralSource, VersionLookupError

MAX_OUTPUT = 40_000
LOCAL_REPO = Path.home() / ".m2" / "repository"
CACHE = Path(tempfile.gettempdir()) / "java-upgrade-agent" / "jars"


def _jar(source: MavenCentralSource, group: str, artifact: str, version: str, classifier: str = "") -> Path:
    """The artifact's JAR from ~/.m2, or downloaded from the repository into a temp cache."""
    name = f"{artifact}-{version}{'-' + classifier if classifier else ''}.jar"
    relative = f"{group.replace('.', '/')}/{artifact}/{version}/{name}"
    local = LOCAL_REPO / relative
    if local.is_file():
        return local
    cached = CACHE / relative
    if not cached.is_file():
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(source.fetch(relative))
    return cached


def _child(element: ET.Element, tag: str) -> str:
    found = element.find(f"{{*}}{tag}")
    return found.text.strip() if found is not None and found.text else ""


def describe_pom(text: str) -> str:
    """A compact summary of a POM: parent, managed dependencies (versions resolved from its own
    properties where possible) and the BOMs it imports."""
    root = ET.fromstring(text)
    props = {}
    section = root.find("{*}properties")
    for prop in section if section is not None else []:
        if isinstance(prop.tag, str) and prop.text:
            props[prop.tag.split("}")[-1]] = prop.text.strip()
    props["project.version"] = _child(root, "version")

    def resolve(value: str) -> str:
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)

    lines = [f"{_child(root, 'groupId')}:{_child(root, 'artifactId')}:{_child(root, 'version')}"]
    parent = root.find("{*}parent")
    if parent is not None:
        lines.append(f"parent: {_child(parent, 'groupId')}:{_child(parent, 'artifactId')}:{_child(parent, 'version')}")
    managed = root.findall("{*}dependencyManagement/{*}dependencies/{*}dependency")
    imports = [d for d in managed if _child(d, "scope") == "import"]
    others = [d for d in managed if _child(d, "scope") != "import"]
    if imports:
        lines.append("imported BOMs:")
        lines += [f"  {_child(d, 'groupId')}:{_child(d, 'artifactId')}:{resolve(_child(d, 'version'))}" for d in imports]
    lines.append(f"managed dependencies ({len(others)}):")
    lines += [f"  {resolve(_child(d, 'groupId'))}:{_child(d, 'artifactId')}:{resolve(_child(d, 'version'))}"
              for d in others]
    return "\n".join(lines)


def make_library_tools(source: MavenCentralSource) -> list[BaseTool]:
    @tool
    def read_artifact_pom(group: str, artifact: str, version: str) -> str:
        """Read the POM of a published artifact, typically a parent or BOM (e.g. spring-boot-starter-parent,
        spring-boot-dependencies, spring-ai-bom), and list the dependencies it manages and the BOMs it imports.

        Use it to find out whether an upgraded parent/BOM still manages a dependency, and under which
        name and version. A parent such as spring-boot-starter-parent usually manages nothing itself:
        follow its `parent:` line (e.g. spring-boot-dependencies) to see the managed list.
        """
        relative = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"
        try:
            text = source.fetch(relative).decode("utf-8", errors="replace")
        except VersionLookupError as e:
            return f"error: could not get {artifact}-{version}.pom: {e.reason}"
        try:
            return describe_pom(text)[:MAX_OUTPUT]
        except ET.ParseError:
            return f"error: {artifact}-{version}.pom is not valid XML"

    @tool
    def inspect_class(group: str, artifact: str, version: str, fqcn: str) -> str:
        """Show the public and protected members of a class as compiled in a specific artifact version
        (javap output), e.g. to see a method's real signature after an upgrade.

        `fqcn` is the fully qualified class name, e.g. "org.springframework.http.HttpStatus".
        """
        try:
            jar = _jar(source, group, artifact, version)
        except VersionLookupError as e:
            return f"error: could not get {artifact}-{version}.jar: {e.reason}"
        javap = shutil.which("javap") or "javap"
        result = subprocess.run([javap, "-cp", str(jar), fqcn], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        output = (result.stdout or result.stderr).strip()
        return output[:MAX_OUTPUT] or f"error: javap returned nothing for {fqcn}"

    @tool
    def read_library_docs(group: str, artifact: str, version: str, fqcn: str) -> str:
        """Read the Java source (with its javadoc) of a class from the artifact's -sources.jar.

        `fqcn` is the fully qualified class name; for a nested class give the outer class.
        """
        try:
            jar = _jar(source, group, artifact, version, classifier="sources")
        except VersionLookupError as e:
            return f"error: no sources jar for {artifact}-{version}: {e.reason}"
        member = fqcn.split("$")[0].replace(".", "/") + ".java"
        try:
            with zipfile.ZipFile(jar) as z:
                return z.read(member).decode("utf-8", errors="replace")[:MAX_OUTPUT]
        except KeyError:
            return f"error: {member} is not in {jar.name}"
        except zipfile.BadZipFile:
            return f"error: {jar.name} is not a valid jar"

    return [read_artifact_pom, inspect_class, read_library_docs]
