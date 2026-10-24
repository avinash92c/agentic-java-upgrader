"""Version sources: where the published versions of a Maven artifact are read from.

v1 has Maven Central only. An Artifactory / in-house repository connector (TODO) will
implement the same `VersionSource` interface.
"""

import ssl
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod

import httpx
import truststore
from langchain_core.tools import BaseTool, tool

from ..tracing import log

USER_AGENT = "agentic-java-upgrader/0.1.0"


class VersionLookupError(Exception):
    """A version lookup failed. `reason` is short and safe to show to the user and the LLM."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class VersionSource(ABC):
    @abstractmethod
    def get_versions(self, group: str, artifact: str) -> list[str]:
        """All published versions of `group:artifact`, in the order the repository lists them."""


class MavenCentralSource(VersionSource):
    """Reads `maven-metadata.xml` from Maven Central."""

    BASE_URL = "https://repo1.maven.org/maven2"

    def __init__(self, timeout: float = 15.0):
        # The OS certificate store, not Python's bundled one: HTTPS-inspecting antivirus and
        # corporate proxies present certificates that only the OS trusts.
        self._client = httpx.Client(
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._versions: dict[tuple[str, str], list[str]] = {}  # per run; the selection check reuses it

    @classmethod
    def metadata_url(cls, group: str, artifact: str) -> str:
        return f"{cls.BASE_URL}/{group.strip().replace('.', '/')}/{artifact.strip()}/maven-metadata.xml"

    def get_versions(self, group: str, artifact: str) -> list[str]:
        key = (group.strip(), artifact.strip())
        if key not in self._versions:
            self._versions[key] = self._download_versions(group, artifact)
        else:
            log.debug("maven central: %s:%s versions from cache", group, artifact)
        return self._versions[key]

    def _download_versions(self, group: str, artifact: str) -> list[str]:
        url = self.metadata_url(group, artifact)
        log.info("maven central: GET %s", url)
        try:
            response = self._client.get(url)
        except httpx.TimeoutException:
            log.warning("maven central: timeout for %s", url)
            raise VersionLookupError("timeout") from None
        except httpx.HTTPError as e:
            log.warning("maven central: network error for %s: %s", url, e)
            raise VersionLookupError(f"network error: {e}") from None

        log.info("maven central: HTTP %d, %d bytes", response.status_code, len(response.content))
        if response.status_code == 404:
            raise VersionLookupError("not found")
        if response.status_code != 200:
            raise VersionLookupError(f"HTTP {response.status_code}")

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise VersionLookupError("invalid metadata") from None
        versions = [v.text.strip() for v in root.iterfind("versioning/versions/version") if v.text]
        if not versions:
            raise VersionLookupError("no versions listed")
        log.info("maven central: %s:%s has %d versions, last listed %s", group, artifact, len(versions), versions[-1])
        return versions

    def fetch(self, relative_path: str) -> bytes:
        """Download a file from the repository, e.g. "org/foo/bar/1.0/bar-1.0-sources.jar"."""
        url = f"{self.BASE_URL}/{relative_path}"
        log.info("maven central: GET %s", url)
        try:
            response = self._client.get(url)
        except httpx.HTTPError as e:
            log.warning("maven central: network error for %s: %s", url, e)
            raise VersionLookupError(f"network error: {e}") from None
        log.info("maven central: HTTP %d, %d bytes", response.status_code, len(response.content))
        if response.status_code != 200:
            raise VersionLookupError("not found" if response.status_code == 404 else f"HTTP {response.status_code}")
        return response.content

    SEARCH_URL = "https://search.maven.org/solrsearch/select"

    def artifacts_with_class(self, fqcn: str, rows: int = 200) -> list[tuple[str, list[str]]]:
        """Artifacts on Maven Central that contain the class `fqcn` (Maven Central's class search), as
        [("group:artifact", [versions seen])]. Artifacts whose groupId matches the class's package come
        first (javax.annotation:* for javax.annotation.Resource), then those with the most versions.
        Empty on any error."""
        params = {"q": f'fc:"{fqcn}"', "rows": rows, "wt": "json"}
        log.info("maven central: class search %s?q=fc:\"%s\"", self.SEARCH_URL, fqcn)
        try:
            response = self._client.get(self.SEARCH_URL, params=params)
            docs = response.json().get("response", {}).get("docs", []) if response.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as e:
            log.warning("maven central: class search failed: %s", e)
            return []
        found: dict[str, list[str]] = {}
        for doc in docs:
            if doc.get("g") and doc.get("a"):
                found.setdefault(f"{doc['g']}:{doc['a']}", []).append(doc.get("v", "?"))
        log.info("maven central: %d artifact(s) contain %s", len(found), fqcn)
        package = fqcn.rsplit(".", 1)[0]

        def rank(item: tuple[str, list[str]]):
            group = item[0].split(":")[0]
            return (not (package.startswith(group) or group.startswith(package)), -len(item[1]))

        return sorted(found.items(), key=rank)

    def close(self) -> None:
        self._client.close()


_default_source: MavenCentralSource | None = None


def default_source() -> MavenCentralSource:
    """One shared Maven Central client for the whole run."""
    global _default_source
    if _default_source is None:
        _default_source = MavenCentralSource()
    return _default_source


def make_find_versions_tool(source: VersionSource) -> BaseTool:
    """The `find_versions` LLM tool, bound to the configured version source."""

    @tool
    def find_versions(group: str, artifact: str) -> dict:
        """List all published versions of a Maven artifact.

        Versions are in the order the repository lists them, roughly by publication date; this is
        not strict version order (a patch to an older line can appear after a newer version).
        The list includes pre-release versions (alpha, beta, milestone, RC, SNAPSHOT).
        Returns {"group", "artifact", "versions": [...]}, or {"group", "artifact", "error": reason}
        when the lookup fails (for example "not found").
        """
        try:
            return {"group": group, "artifact": artifact, "versions": source.get_versions(group, artifact)}
        except VersionLookupError as e:
            return {"group": group, "artifact": artifact, "error": e.reason}

    return find_versions
