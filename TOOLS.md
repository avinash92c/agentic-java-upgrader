# Tools and functions

What the agent can do, and which part does it: the **LLM tools** a model calls itself, the **code functions** the agent runs without a model, and the **structured fix format** the local model fills in. Code is in `python/src/java_upgrade_agent/`.

Also see [README.md](README.md) for the overall flow and setup.

## Who does what

| Part | Role |
|---|---|
| Code (deterministic) | Reads the POMs, looks up versions, applies upgrades and fixes, runs Maven, sorts errors into issues, checks results, learns verified fixes |
| `[llm]`: local model (e.g. `qwen3:8b`) | Picks the new version per dependency; turns each build issue (plus any advice) into concrete changes |
| `[repair_llm]`: optional | Replaces `[llm]` for the repair rounds |
| `[consultant]`: optional CLI (Claude Code, Gemini CLI, Codex CLI, ...) | Read-only: finds the fix for an issue (reads the project, searches the web) and returns advice; never edits |

## LLM tools

Tools a model can call during a repair round, through LangChain tool calling. They are given to the repair **agent**, which is used for **"other" issues** (anything that isn't a compile, missing-package or POM error). Code, dependency and POM issues use the structured fix format instead (below).

All tools are confined to the project folder. `.env` and `settings.xml` can't be read. Every call is logged with `-v`.

| Tool | Arguments | What it does | Defined in |
|---|---|---|---|
| `list_files` | `path`, `pattern` | Lists files under a project folder matching a glob (`*.xml`, `**/pom.xml`); skips `.git`, `target`, `node_modules`, `.idea` | `tools/files.py` |
| `read_file` | `path` | Reads a project file (up to 200 KB) | `tools/files.py` |
| `search_code` | `pattern` | Regex search over `.java`, `.kt`, `.xml`, `.properties`, `.yml`; up to 100 matches as `path:line: text` | `tools/files.py` |
| `edit_file` | `path`, `old`, `new` | Replaces one exact occurrence of `old` with `new`; keeps the file's line endings; every edit recorded | `tools/files.py` |
| `write_file` | `path`, `content` | Creates or replaces a file; recorded | `tools/files.py` |
| `find_versions` | `group`, `artifact` | All published versions of an artifact on Maven Central (`maven-metadata.xml`), pre-releases included | `tools/repo.py` |
| `read_artifact_pom` | `group`, `artifact`, `version` | Reads a published POM (usually a parent or BOM): its parent, imported BOMs and managed dependencies | `tools/library.py` |
| `inspect_class` | `group`, `artifact`, `version`, `fqcn` | `javap` on the artifact's JAR (`~/.m2`, else downloaded): the class's real public/protected members in that version | `tools/library.py` |
| `read_library_docs` | `group`, `artifact`, `version`, `fqcn` | The class's source with javadoc, from the artifact's `-sources.jar` | `tools/library.py` |
| `find_replacement_artifact` | `group`, `artifact` | What replaced a dependency that the upgraded parent/BOM no longer manages: BOM comparison first, then GitHub release notes | `tools/migration.py` |

**Guards on every tool call** (`repair.py`, `_guarded`):
- String arguments are trimmed. `group`/`artifact`/`version` must be a single value without spaces, otherwise the model gets an explanation instead of a result.
- Lookups (`find_versions`, `read_artifact_pom`, `inspect_class`, `read_library_docs`, `find_replacement_artifact`) called again with the same arguments in one attempt are answered "already called" instead of running again.
- Any error inside a tool is returned to the model as `error: ...`, so one bad call can't end the attempt.

## Structured fix format (code, dependency and POM issues)

For these issues the local model makes **one structured-output call** (no tool calling) and code applies the result (`compile_fix.py`, `nodes._fix_structured`):

```
status:            fix | unsure
add_dependencies:  [{groupId, artifactId, version, scope}]   # inserted into pom.xml by code
edits:             [{file, line, end_line, new}]              # replace or delete lines (preferred)
                   [{file, old, new}]                         # or replace text
reason:            what changed in the library and how it was fixed
```

| Applied by | What it does |
|---|---|
| `ProjectPoms.add_dependency` (`tools/pom.py`) | Inserts a `<dependency>` into the right module's project-level `<dependencies>` (created if missing) with the file's indentation and line endings; `${property}` versions kept; skipped if already declared |
| `apply_one` / `apply_edits` (`compile_fix.py`) | Line edits: replace or delete lines by number, keeping indentation. Text edits: removes copied excerpt prefixes (`>>  84 |`), tries an exact match, then a whitespace-insensitive match; must match exactly once; deleting a whole line removes the line. pom.xml only for dependency/POM issues; files outside the project refused |

If nothing could be applied for a compile error, the model is asked again with **API notes** (below). The same error at several places is fixed in one call (every place shown, one edit per place).

## Code functions (no model)

### Project POMs: `tools/pom.py`

| Function | What it does |
|---|---|
| `read_poms` | Reads the root `pom.xml` and every module's `pom.xml` (standard `<modules>` layout) |
| `ProjectPoms.collect` | Versioned dependencies per POM (parent, BOM imports, managed and direct dependencies), versions resolved through `${properties}` and parent POMs; own modules, plugins and profiles skipped |
| `ProjectPoms.locate` / `write` | Finds where a dependency's version is written (literal `<version>` or `${property}`, in the module or a parent POM) and changes only that value |
| `ProjectPoms.rename_artifact` | Renames an `<artifactId>` in every matching `<dependency>` (used for clear renames found by the BOM comparison) |
| `ProjectPoms.pom_for` | The module POM a source file belongs to |
| `ProjectPoms.add_dependency` | See the structured fix format |

### Versions and Maven Central: `tools/repo.py`, `version_select.py`

| Function | What it does |
|---|---|
| `MavenCentralSource.get_versions` | Published versions from `maven-metadata.xml` (OS certificate store, so HTTPS-inspecting proxies and antivirus work); cached per artifact |
| `MavenCentralSource.fetch` | Downloads a file (POM, JAR, sources JAR) from Maven Central |
| `MavenCentralSource.artifacts_with_class` | Maven Central class search: which artifacts contain a class (used for missing packages) |
| `select_version` | The LLM picks the newest stable version from the last 25 published ones; code rejects a pick that isn't published |

### Migration help: `tools/migration.py`

| Function | What it does |
|---|---|
| `missing_dependencies` | Dependencies a build log reports as "version is missing" or "could not find artifact" |
| `clear_rename` | Whether an upgraded BOM clearly renamed a dependency (same name parts, exactly one candidate); if so the agent renames it in code, without an LLM |
| `compare_boms` | Compares the managed artifacts of the old and new parent/BOM and ranks the new ones by name similarity |
| `release_notes` | Finds the project's GitHub repo from the POM's `<scm><url>` and returns release-note lines between the old and new version that mention the artifact |
| `find_replacement` / `migration_hints` | BOM comparison first, release notes second; the result goes into the repair prompt as a hint |

### Build and issues: `nodes.py`, `repair.py`

| Function | What it does |
|---|---|
| `run_maven` | Runs `mvn -B clean install -DskipTests`, then `download_sources` |
| `download_sources` | `mvn dependency:sources` after every build; records which library versions were resolved (old ones at the baseline, new ones later) |
| `classify_failure` | Environment problem (download, auth, network, TLS) vs something the agent can fix |
| `build_issues` | Splits a failed build into issues: `pom`, `dependency` (`package X does not exist`), `code` (compile errors), `other`; repeats and Maven's wrapper lines removed; both Windows path formats understood |
| `java_fix_context` | What the model sees of a Java file: the whole file if ≤ 250 lines, otherwise imports, fields, setup methods (constructors, `@PostConstruct`, `@Bean`) and ±25 lines around the error |
| `pom_context` | The pom.xml lines a POM error points to |
| `dependency_candidates` | Artifacts that provide the classes of a missing package (Maven Central class search; for `javax.*` also the `jakarta.*` equivalent) |
| `project_file` (`compile_fix.py`) | Turns any path from an error or a model (no drive letter, `/C:/...`, relative, `.../src/...`) into the real file in the project |

### API notes: `compile_fix.py`

| Function | What it does |
|---|---|
| `api_notes` | For a missing method: walks the class's type hierarchy across the dependencies' sources JARs (up to 4 levels, other artifacts included); reports where it was declared in the old version (with its javadoc and `@deprecated` note) and the new version's methods per type: same name, sharing a word, similar names |

### Java version: `tools/java_version.py`

| Function | What it does |
|---|---|
| `project_version` | The project's Java version (`maven.compiler.*`, `java.version`, or `maven-compiler-plugin` settings, properties resolved) |
| `jdk_version` / `min_release` | The JDK Maven runs on, and the oldest Java version it can still compile |
| `update_version` | Sets the Java version in the POMs (properties and plugin settings, including custom properties the plugin refers to) after you confirm at the prompt |

### Consultant: `consultant.py`

| Function | What it does |
|---|---|
| `ask` | Runs the configured CLI read-only in the project folder with the question on stdin; returns its answer (cause, exact line-numbered changes, confidence), which goes into the local model's prompt |
| `should_ask` | `when = "always"` (every round) or `"after_first_try"` |

### Learning: `learning.py`

| Function | What it does |
|---|---|
| `signature` | The error without paths, lines or columns, so the same error matches across files and runs |
| `record` | Appends a fix to `migration_hints.yaml` once the next build no longer shows the error and shows no new error within 3 lines of the fixed place; syntax-error fixes and failed rounds are never recorded |
| `find_hints` / `format_hints` | Known fixes for an error (same signature, or same symbol and class, or same missing package), given to the model as "known fixes from earlier runs" |

## Repair round at a glance

1. `build_issues` sorts the build errors; the first issue with tries left is taken, together with every issue that has the **same signature**.
2. `pom` issue: `clear_rename` first (no LLM).
3. The consultant (if configured) is asked; known fixes are looked up.
4. Code, dependency, POM: one structured-output call, applied by code. Other: the tool agent with the LLM tools above.
5. The guard puts back any upgraded version a fix reverted.
6. Rebuild; if the error is gone (and nothing new appeared nearby), the fix is learned.
