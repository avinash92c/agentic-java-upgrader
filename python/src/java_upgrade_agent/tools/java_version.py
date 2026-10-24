"""The project's Java version vs the JDK Maven runs on, and updating the version in the POMs."""

import re
import shutil
import subprocess
from pathlib import Path

from .pom import ProjectPoms

# Where a POM sets the Java version: these properties, or the same tags in maven-compiler-plugin's
# <configuration> (<release>, <source>, <target>).
VERSION_PROPERTIES = ["maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "java.version"]
PLUGIN_TAGS = ["release", "source", "target"]
LTS_VERSIONS = [17, 21, 25]


def _major(value: str | None) -> int | None:
    """"1.7" -> 7, "17" -> 17, None or unparsable -> None."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(?:1\.)?(\d+)\s*", value)
    return int(m.group(1)) if m else None


def jdk_version(project_dir: str) -> int | None:
    """Major version of the JDK Maven uses (from `mvn -v`, which honours JAVA_HOME)."""
    mvn = shutil.which("mvn") or "mvn"
    result = subprocess.run([mvn, "-v"], cwd=project_dir, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    m = re.search(r"Java version:\s*(?:1\.)?(\d+)", result.stdout)
    return int(m.group(1)) if m else None


def min_release(jdk: int) -> int:
    """Lowest Java release this JDK's compiler accepts: JDK 12 dropped 6, JDK 20 dropped 7."""
    return 8 if jdk >= 20 else 7 if jdk >= 12 else 6


def _compiler_settings(poms: ProjectPoms, path: str) -> list[str]:
    """Raw values of <release>/<source>/<target> in this POM's maven-compiler-plugin configuration.
    (iterfind, not iter: only the ElementPath functions understand the {*} namespace wildcard.)"""
    values = []
    for plugin in poms.trees[path].iterfind(".//{*}plugin"):
        artifact = plugin.find("{*}artifactId")
        if artifact is None or (artifact.text or "").strip() != "maven-compiler-plugin":
            continue
        for tag in PLUGIN_TAGS:
            values += [(e.text or "").strip() for e in plugin.iterfind(f".//{{*}}{tag}") if e.text]
    return values


def project_version(project_dir: str) -> int | None:
    """The lowest Java version any POM in the project targets."""
    poms = ProjectPoms.load(project_dir)
    found = []
    for path in poms.trees:
        props = poms.properties(path)
        found += [_major(props.get(name)) for name in VERSION_PROPERTIES]
        found += [_major(poms.resolve(value, path)) for value in _compiler_settings(poms, path)]
    versions = [v for v in found if v is not None]
    return min(versions) if versions else None


def update_version(project_dir: str, new: int) -> list[str]:
    """Set every literal Java version in the POMs to `new` (properties and maven-compiler-plugin
    <release>/<source>/<target>), changing only those values. ${...} references are left alone; the
    properties they point at (also custom ones such as java-version) are updated instead.
    Returns the POM paths edited."""
    poms = ProjectPoms.load(project_dir)
    referenced = {m.group(1) for path in poms.trees for value in _compiler_settings(poms, path)
                  if (m := re.fullmatch(r"\$\{([\w.\-]+)\}", value))}
    tags = list(dict.fromkeys(VERSION_PROPERTIES + sorted(referenced) + PLUGIN_TAGS))
    pattern = re.compile(r"<(" + "|".join(re.escape(t) for t in tags) + r")>\s*((?:1\.)?\d+)\s*</\1>")
    edited = []
    for path, text in poms.texts.items():
        # <source>/<target>/<release> only count inside maven-compiler-plugin; properties anywhere.
        def replace(m: re.Match) -> str:
            tag = m.group(1)
            if tag in PLUGIN_TAGS and "maven-compiler-plugin" not in text[max(0, m.start() - 2000): m.start()]:
                return m.group(0)
            return f"<{tag}>{new}</{tag}>"

        updated = pattern.sub(replace, text)
        if updated != text:
            with open(Path(project_dir) / path, "w", encoding="utf-8", newline="") as f:
                f.write(updated)
            edited.append(path)
    return edited
