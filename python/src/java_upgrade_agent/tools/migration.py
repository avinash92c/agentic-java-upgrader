"""Finding what replaced a dependency that an upgraded parent/BOM no longer manages.

Two sources, tried in order; the first one with an answer wins:
1. BOM comparison: the managed artifacts of the old and new version of each upgraded parent/BOM
   (from Maven Central). If the dependency was managed before and isn't now, the artifacts that
   appeared in the new version are ranked by name similarity.
2. Release notes: the project's GitHub repository (from the POM's <scm><url>, or its parent's),
   releases between the old and new version, lines that mention the dependency or a rename.
"""

import os
import re
import ssl
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import httpx
import truststore
from langchain_core.tools import BaseTool, tool

from ..state import AppliedUpgrade
from ..tracing import log
from .repo import MavenCentralSource, VersionLookupError

MAX_DEPTH = 4  # parent / imported-BOM levels followed when collecting managed artifacts
MAX_CANDIDATES = 5
MAX_NOTES_CHARS = 4000
GITHUB_PAGES = 3  # 100 releases per page

MISSING_VERSION = re.compile(r"'dependencies\.dependency\.version' for ([\w.\-]+):([\w.\-]+):\w+ is missing")
NOT_FOUND = re.compile(r"Could not find artifact ([\w.\-]+):([\w.\-]+):")
GITHUB_REPO = re.compile(r"github\.com[/:]([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?:[/#?].*)?$")
RENAME_WORDS = re.compile(r"renam|replac|moved|deprecat|removed|relocat", re.IGNORECASE)


def missing_dependencies(log_text: str) -> list[tuple[str, str]]:
    """group:artifact pairs from Maven errors about a missing version or an artifact that can't be found."""
    found: list[tuple[str, str]] = []
    for pattern in (MISSING_VERSION, NOT_FOUND):
        for m in pattern.finditer(log_text):
            if (m.group(1), m.group(2)) not in found:
                found.append((m.group(1), m.group(2)))
    return found


def _child(element: ET.Element | None, tag: str) -> str:
    found = element.find(f"{{*}}{tag}") if element is not None else None
    return found.text.strip() if found is not None and found.text else ""


class _Poms:
    """Published POMs from Maven Central, parsed with properties inherited from their parents."""

    def __init__(self, source: MavenCentralSource):
        self.source = source
        self._cache: dict[tuple[str, str, str], tuple[ET.Element, dict[str, str]] | None] = {}

    def load(self, group: str, artifact: str, version: str, depth: int = 0):
        key = (group, artifact, version)
        if key not in self._cache:
            self._cache[key] = None
            try:
                text = self.source.fetch(f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom")
                root = ET.fromstring(text)
            except (VersionLookupError, ET.ParseError) as e:
                log.debug("migration: could not read %s:%s:%s pom: %s", group, artifact, version, e)
                return None
            props: dict[str, str] = {}
            parent = root.find("{*}parent")
            if parent is not None and depth < MAX_DEPTH:
                loaded = self.load(_child(parent, "groupId"), _child(parent, "artifactId"), _child(parent, "version"),
                                   depth + 1)
                if loaded:
                    props.update(loaded[1])
            section = root.find("{*}properties")
            for prop in section if section is not None else []:
                if isinstance(prop.tag, str) and prop.text:
                    props[prop.tag.split("}")[-1]] = prop.text.strip()
            props["project.groupId"] = _child(root, "groupId") or _child(parent, "groupId")
            props["project.version"] = _child(root, "version") or _child(parent, "version")
            self._cache[key] = (root, props)
        return self._cache[key]

    @staticmethod
    def resolve(value: str, props: dict[str, str]) -> str:
        for _ in range(5):
            new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
            if new == value:
                break
            value = new
        return value

    def managed(self, group: str, artifact: str, version: str, prefix: str, depth: int = 0) -> set[tuple[str, str]]:
        """group:artifact pairs managed by this POM, its parents and imported BOMs. Only parents and
        BOMs whose groupId starts with `prefix` are followed, to keep the number of downloads small."""
        loaded = self.load(group, artifact, version)
        if loaded is None:
            return set()
        root, props = loaded
        result: set[tuple[str, str]] = set()
        for dep in root.findall("{*}dependencyManagement/{*}dependencies/{*}dependency"):
            g, a = self.resolve(_child(dep, "groupId"), props), _child(dep, "artifactId")
            if _child(dep, "scope") == "import":
                if g.startswith(prefix) and depth < MAX_DEPTH:
                    result |= self.managed(g, a, self.resolve(_child(dep, "version"), props), prefix, depth + 1)
            else:
                result.add((g, a))
        parent = root.find("{*}parent")
        if parent is not None and _child(parent, "groupId").startswith(prefix) and depth < MAX_DEPTH:
            result |= self.managed(_child(parent, "groupId"), _child(parent, "artifactId"),
                                   _child(parent, "version"), prefix, depth + 1)
        return result

    def scm_repo(self, group: str, artifact: str, version: str) -> tuple[str, str] | None:
        """GitHub owner/repo from <scm><url> of the POM or the nearest parent that has one."""
        for _ in range(MAX_DEPTH + 1):
            loaded = self.load(group, artifact, version)
            if loaded is None:
                return None
            root, props = loaded
            url = self.resolve(_child(root.find("{*}scm"), "url"), props)
            if m := GITHUB_REPO.search(url):
                return m.group(1), m.group(2)
            parent = root.find("{*}parent")
            if parent is None:
                return None
            group, artifact, version = _child(parent, "groupId"), _child(parent, "artifactId"), _child(parent, "version")
        return None


def _similarity(a: str, b: str) -> float:
    tokens_a, tokens_b = set(a.split("-")), set(b.split("-"))
    overlap = len(tokens_a & tokens_b) / max(1, len(tokens_a | tokens_b))
    return overlap + SequenceMatcher(None, a, b).ratio()


def _prefix(group: str) -> str:
    return ".".join(group.split(".")[:2])


def _name_parts(artifact: str) -> set[str]:
    """artifactId split on "-", with a plural "s" removed: "spring-ai-advisors-vector-store" ->
    {spring, ai, advisor, vector, store}."""
    return {p[:-1] if p.endswith("s") and len(p) > 3 else p for p in artifact.lower().split("-") if p}


def clear_rename(source: MavenCentralSource, group: str, artifact: str,
                 applied: list[AppliedUpgrade]) -> tuple[str, AppliedUpgrade] | None:
    """The artifact that replaced group:artifact, when that is unambiguous: an upgraded parent/BOM
    managed it before and doesn't now, and exactly one artifact it newly manages in the same group
    has the same name parts (order and plural aside), e.g. spring-ai-advisors-vector-store ->
    spring-ai-vector-store-advisor. Returns (new artifactId, the upgrade) or None."""
    poms = _Poms(source)
    prefix = _prefix(group)
    for u in applied:
        old = poms.managed(u.group, u.artifact, u.old_version, prefix)
        if (group, artifact) not in old:
            continue
        new = poms.managed(u.group, u.artifact, u.new_version, prefix)
        if (group, artifact) in new:
            return None
        parts = _name_parts(artifact)
        matches = [a for g, a in new - old if g == group and _name_parts(a) == parts]
        if len(matches) == 1:
            return matches[0], u
        log.info("migration: %s:%s: no unambiguous rename in %s %s (%d candidates)", group, artifact,
                 u.artifact, u.new_version, len(matches))
        return None
    return None


def compare_boms(poms: _Poms, group: str, artifact: str, applied: list[AppliedUpgrade]) -> tuple[str | None, AppliedUpgrade | None]:
    """Source 1. Returns (answer or None, the upgrade that used to manage the dependency)."""
    prefix = _prefix(group)
    for u in applied:
        old = poms.managed(u.group, u.artifact, u.old_version, prefix)
        if (group, artifact) not in old:
            continue
        new = poms.managed(u.group, u.artifact, u.new_version, prefix)
        log.info("migration: %s:%s was managed by %s %s (%d managed), new %s (%d managed)", group, artifact,
                 u.artifact, u.old_version, len(old), u.new_version, len(new))
        if (group, artifact) in new:
            return None, u  # still managed: the BOMs don't explain the error
        added = [ga for ga in new - old if ga[0] == group] or [ga for ga in new - old if ga[0].startswith(prefix)]
        if not added:
            return None, u
        ranked = sorted(added, key=lambda ga: _similarity(artifact, ga[1]), reverse=True)[:MAX_CANDIDATES]
        answer = (f"{group}:{artifact} was managed by {u.group}:{u.artifact} {u.old_version} but is not managed by "
                  f"{u.new_version}. Artifacts newly managed by {u.new_version}, closest name first: "
                  + ", ".join(f"{g}:{a}" for g, a in ranked)
                  + ". The first one is the likely replacement (renamed artifact); check it fits before using it.")
        return answer, u
    return None, None


def _github_get(client: httpx.Client, url: str):
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    log.info("migration: GET %s", url)
    response = client.get(url, headers=headers)
    log.info("migration: HTTP %d", response.status_code)
    return response.json() if response.status_code == 200 else None


def release_notes(poms: _Poms, group: str, artifact: str, upgrade: AppliedUpgrade | None) -> str | None:
    """Source 2. Lines of GitHub release notes (between the old and new version) about the dependency."""
    if upgrade is not None:
        coords, old_version, new_version = (upgrade.group, upgrade.artifact, upgrade.new_version), \
            upgrade.old_version, upgrade.new_version
    else:  # no known upgrade: use the dependency's own newest POM
        try:
            versions = poms.source.get_versions(group, artifact)
        except VersionLookupError:
            return None
        coords, old_version, new_version = (group, artifact, versions[-1]), None, None
    repo = poms.scm_repo(*coords)
    if repo is None:
        log.info("migration: no GitHub <scm> url for %s:%s", coords[0], coords[1])
        return None
    owner, name = repo

    releases = []
    with httpx.Client(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=20,
                      headers={"User-Agent": "agentic-java-upgrader"}) as client:
        for page in range(1, GITHUB_PAGES + 1):
            try:
                batch = _github_get(client, f"https://api.github.com/repos/{owner}/{name}/releases?per_page=100&page={page}")
            except httpx.HTTPError as e:
                log.warning("migration: GitHub request failed: %s", e)
                break
            if not batch:
                break
            releases += batch

    tags = [r.get("tag_name", "").lstrip("vV") for r in releases]  # newest first
    end = tags.index(new_version) if new_version in tags else 0
    start = tags.index(old_version) if old_version in tags else len(tags)
    window = releases[end:start] or releases[:30]

    keys = {artifact} | {t for t in artifact.split("-") if len(t) > 3}
    out: list[str] = []
    for release in window:
        lines = (release.get("body") or "").splitlines()
        hits = [i for i, line in enumerate(lines)
                if artifact in line or (RENAME_WORDS.search(line) and sum(k in line for k in keys) >= 2)]
        if not hits:
            continue
        out.append(f"## {release.get('tag_name')}")
        shown: set[int] = set()
        for i in hits:
            for j in range(max(0, i - 1), min(len(lines), i + 2)):
                if j not in shown:
                    shown.add(j)
                    out.append(lines[j].rstrip())
        if sum(len(line) for line in out) > MAX_NOTES_CHARS:
            break
    if not out:
        log.info("migration: no release notes of %s/%s mention %s", owner, name, artifact)
        return None
    text = "\n".join(out)[:MAX_NOTES_CHARS]
    return f"Release notes of github.com/{owner}/{name} that mention {artifact}:\n{text}"


def find_replacement(source: MavenCentralSource, group: str, artifact: str, applied: list[AppliedUpgrade]) -> str:
    """Try the BOM comparison, then the release notes; return the first answer."""
    poms = _Poms(source)
    answer, upgrade = compare_boms(poms, group, artifact, applied)
    if answer:
        log.info("migration: %s:%s answered by BOM comparison", group, artifact)
        return answer
    notes = release_notes(poms, group, artifact, upgrade)
    if notes:
        log.info("migration: %s:%s answered by release notes", group, artifact)
        return notes
    return (f"No replacement found for {group}:{artifact}: the upgraded BOMs don't explain it and the release "
            "notes don't mention it. Use the pom.xml and your own knowledge of the library.")


def migration_hints(source: MavenCentralSource, build_log: str, applied: list[AppliedUpgrade]) -> str:
    """Hints for the repair task, for every dependency the build log reports as missing or not found."""
    hints = []
    for group, artifact in missing_dependencies(build_log)[:3]:
        try:
            hints.append(f"- {find_replacement(source, group, artifact, applied)}")
        except Exception as e:  # a hint must never break the repair step
            log.warning("migration: lookup for %s:%s failed: %s", group, artifact, e)
    return "\n".join(hints) or "(none)"


def make_migration_tools(source: MavenCentralSource, applied: list[AppliedUpgrade]) -> list[BaseTool]:
    @tool
    def find_replacement_artifact(group: str, artifact: str) -> str:
        """Find what replaced a dependency that the upgraded parent/BOM no longer manages (for example
        after "'dependencies.dependency.version' for g:a:jar is missing" or "Could not find artifact").

        First compares the managed artifacts of the old and new parent/BOM version; if that gives no
        answer, searches the project's GitHub release notes between the two versions.
        """
        return find_replacement(source, group, artifact, applied)

    return [find_replacement_artifact]
