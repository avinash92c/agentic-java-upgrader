"""Library inspection tools: the real API of an upgraded artifact, from its JAR and sources JAR."""

import shutil
import subprocess
import tempfile
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


def make_library_tools(source: MavenCentralSource) -> list[BaseTool]:
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

    return [inspect_class, read_library_docs]
