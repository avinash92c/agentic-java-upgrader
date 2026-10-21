# Plan: Java upgrade agent v1 (LangGraph, Maven)

## Context

The work lives in `D:\Dev\workspaces\vscode\agentic-java-upgrader`. This is a git repo on `main` with `LICENSE`, `README.md`, `plan.md` and the flow diagram (`agent-flow-v1.drawio` / `.png`), and the Python project in `python/` (`python/pyproject.toml`, Poetry; code under `python/src/java_upgrade_agent/`). v1 implements the diagram: read the project's `pom.xml` files into a structured dependency list, find newer versions, apply them one at a time, build, let an LLM repair build failures up to 3 times, then report. The fuller spec (tree diff, class-file check, vulnerability scan, commit handoff) is later work.

## Decisions (from the user)

- Agent loop on **LangChain + LangGraph**.
- Version source: **Maven Central** in v1. Artifactory and other in-house repositories are a **TODO connector** behind the same `VersionSource` interface.
- **Git is optional** (decided 2026-10-18). The agent does not create branches or commit. If the project is a Git repo, the working tree must be clean (undo with `git restore .`); if it is not, the agent warns that changes are made in place and it cannot offer an undo.
- **Latest stable only**: no milestone, RC, alpha, beta, nightly, preview or SNAPSHOT versions.
- **Only upgrade what has a version tag.** Dependencies without one are managed by the parent/BOM and move when that is upgraded.
- **Versions are looked up and applied one dependency at a time** (a loop in the graph); **the build runs once, after all upgrades are applied**.
- **Build success = Maven build without running tests.** Running tests is left to the user (config switch available).
- **The repair LLM works through tool calls** (read files, search code, look up versions, inspect library classes and sources) and is expected to fix build issues from documentation or its own knowledge. See "LLM scope and functions".
- **Dependencies are collected by parsing the POMs in code** (decided 2026-10-18; replaces the earlier LLM extraction). See "Dependency collection".
- **`pom.xml` only** for v1, **including multi-module projects** (all modules are read); Gradle later.
- **Defer = stop the process and exit with a message.** No partial undo, no resume; the user restarts the flow from the beginning.
- **Credentials live in the agent's own config file, never read from `~/.m2/settings.xml`**, and must not reach the LLM. A better approach for secrets is deferred.
- **Build failures are classified before retry** (decided 2026-10-14): only code failures go to the LLM; environment failures defer straight away. See below.
- **The LLM chooses each new version** (decided 2026-10-18): code fetches the published versions from Maven Central and passes them in the prompt; one LLM call (structured output, no tools) picks the newest stable version by the rules in its prompt (stable only, new majors allowed). Code checks only that the pick is a published version; otherwise the dependency is recorded as "could not check".
- **LLM provider is configurable** (decided 2026-10-14): Claude (Anthropic), ChatGPT (OpenAI), Gemini (Google) or Ollama, each through its own LangChain integration. See "LLM provider".
- **One config file holds every agent setting** and is passed in at start (`--config`): repository, LLM, build, retries, data directory, prompts directory.
- **Each run is a session**: a session ID is generated at start, and all session data (logs, build output, attempts, edits) goes under a configurable data directory. The final report is built from that session data.
- **Prompts live as files in a folder**, one per LLM step, so they can be edited without rebuilding the agent.
- **v2**: `SKILL.md`-style skill files (for example per-library migration guides) loaded into the repair step for better fixes.

## Dependency collection (code)

The `extract_dependencies` node parses the POMs in code (`tools/pom.py`); no LLM is involved.

- **Reading**: the root `pom.xml`, then each directory listed under `<modules>` must have its own `pom.xml` (standard layout; nested modules are followed the same way). A missing or unparsable POM stops the run before any change.
- **Output format** (TBD; may change): a JSON object keyed by project/module, each holding that POM's dependency list. A single-module project has one key; a multi-module project has one key for the root plus one per module.
  ```json
  {
    "<project-name/module-name>": [
      { "group": "<group-id>", "artifact": "<artifact-id>", "version": "<version>" }
    ]
  }
  ```
  Example (multi-module):
  ```json
  {
    "shop-parent": [
      { "group": "org.springframework.boot", "artifact": "spring-boot-starter-parent", "version": "3.2.5" },
      { "group": "com.fasterxml.jackson", "artifact": "jackson-bom", "version": "2.17.0" }
    ],
    "shop-api":  [ { "group": "org.mapstruct", "artifact": "mapstruct", "version": "1.5.5.Final" } ],
    "shop-core": [ { "group": "com.google.guava", "artifact": "guava", "version": "33.0.0-jre" } ]
  }
  ```
- **Key** = the POM's own `<artifactId>` (the project name for a single-module project, the module name otherwise). Keys are unique within a reactor build, and they map back to the module's `pom.xml` path for editing.
- **What goes in**, under the POM where it is declared: the `<parent>`, BOM imports (`<dependencyManagement>` entries with `<scope>import</scope>`), and dependencies that have a version tag, including versioned `<dependencyManagement>` entries (in multi-module projects most versions sit in the root's dependency management). Not included in v1: dependencies without a version tag, plugins. The project's own modules (internal parents, inter-module dependencies at `${project.version}`) are left out, as is anything under `<profiles>`. A module with nothing to list has an empty array.
- **`version`** is the resolved value: `${spring.version}` is resolved to `6.1.2`, following `<properties>` up through parent POMs in the project. A version that can't be resolved inside the project is left out.
- **The same artifact can appear under several modules**, each with the version that module declares.
- The map is a Pydantic-typed structure (`state.DependencyMap`: `dict[str, list[Dependency]]`). It will be written to the session folder as `dependencies.json`, and the report groups upgrades by the same keys.
- **Loop order**: root project first, then modules in `<modules>` order; within each, parent, then BOM imports, then dependencies.
- **Version lookups** are cached per `group:artifact`, so an artifact listed under several modules is looked up once.
- **Applying an upgrade** stays deterministic: for each entry, `pom.py` finds where that module's version for the `group:artifact` is defined (literal tag in the module POM, or a `${property}` in the module or an inherited parent POM in the project) and edits it there. If an earlier entry already changed that same location (shared property, or a property inherited from the root), the entry is recorded as "applied via <key>/<artifact>" and nothing is edited twice. If no definition is found, the entry is recorded as "could not apply" and the loop moves on. A property shared by several artifacts is changed once, to the version for the first artifact in loop order.

## Configuration file

TOML, loaded by `config.py` (pydantic-settings, validated at startup; secrets overridable by env vars):

```toml
[repository]   type = "central"   # TODO: "artifactory" (url; api_key | username/password) with the Artifactory connector
[llm]          provider; model; temperature; max_tokens; timeout; api_key; base_url; extra = { ... }   # extra passed to the chat model as-is
[build]        command = "mvn -B -DskipTests package"; jdk_home; run_tests = false
[agent]        max_retries = 3; data_dir = "~/.java-upgrade-agent"; prompts_dir = ""   # empty = built-in prompts
```

## Sessions and agent data

- At start: `session_id = <UTC timestamp>-<6 hex chars>` (sorts by time and stays unique). It is printed to the console and in the report.
- Session folder `<data_dir>/sessions/<session_id>/`:
  - `agent.log`: the run log (also at console level through `rich`).
  - `config.json`: the effective config with secrets removed.
  - `events.jsonl`: one record per step (preflight result, each lookup and apply, each build, classification, each repair attempt with tool calls and file edits, defer reason).
  - `builds/NN-<baseline|post|attempt-k>.log`: full Maven output (redacted).
  - `report.md`: the final report.
- `report.py` builds the report by reading `events.jsonl`, not from in-memory state, so a run that crashes still leaves enough to explain what happened.
- Everything written is redacted (same redaction step as for the LLM).
- `data_dir` must be outside the project being upgraded; preflight refuses a path inside it, because it would dirty the git tree and the LLM file tools could read it.
- No resume in v1 (as decided); old sessions are kept until the user deletes them.

## Prompts

- Built-in defaults ship in the package at `src/java_upgrade_agent/prompts/`. Setting `agent.prompts_dir` points the agent at a user folder; a file there replaces the built-in file with the same name, and missing files fall back to the built-in ones.
- One pair of files per LLM step. v1 has two LLM steps:
  - version selection: `select_system.md` (step-by-step rules: drop pre-releases, keep the flavour, compare numerically, never invent a version; worked example) and `select_task.md` (group, artifact, current version, the last 25 published versions).
  - repair: `repair_system.md` (role, rules: don't revert upgrades, use tools, minimal edits) and `repair_task.md` (template filled with upgrades applied, errors, earlier attempts).
- Templates use `{placeholder}` fields; at startup the loader checks that each file has the fields its step needs and stops with a clear message if not.
- The prompt files used, with their path and hash, are recorded in the session's `events.jsonl`.
- The v2 skills hook goes here: the loader will add relevant skill files to the repair prompt.

## LLM provider

- Config section `[llm]`: `provider` (`anthropic` | `openai` | `google` | `ollama`), `model`, optional `temperature`, `api_key` (cloud providers) and `base_url` (Ollama, default `http://localhost:11434`; can also point OpenAI at a compatible endpoint).
- `llm.py` maps the provider to its LangChain chat model: `ChatAnthropic` (`langchain-anthropic`), `ChatOpenAI` (`langchain-openai`), `ChatGoogleGenerativeAI` (`langchain-google-genai`), `ChatOllama` (`langchain-ollama`). Both LLM steps only see a `BaseChatModel` (structured output for selection, tools bound for repair), so nothing else depends on the provider.
- All four provider packages are **regular dependencies**, always installed (simpler, and a standalone build needs them all anyway).
- API keys follow the credential rules: from the config file, overridable by the standard env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`), never in state, prompts or the report, and added to the redaction list.
- **Tool calling is required** (for repair; see the OPEN question below). Preflight makes one small tool-call probe to the configured model (after the git checks, before the baseline build). A model that can't call tools (common with smaller Ollama models) or a rejected key stops the run before any change is made.
- Default: `anthropic` with the current Claude model id (checked with the `claude-api` skill at implementation). No model ids are hard-coded for the other providers; the user sets `model`.
- The report names the provider and model used. The README notes that cloud providers receive the project's source excerpts, while Ollama keeps everything on the machine.

## Credentials and the LLM

- The user passes the config file to the agent (`--config <path>`). It selects the repository and holds the JFrog URL and any API key or user credentials (TODO, with the Artifactory connector). Environment variables can override the secret values.
- If the passed config file sits inside the project being upgraded, the agent warns (it could be committed) and blocks that exact path from the LLM file tools.
- Only the repository client (Python code) reads credentials. They are not put in graph state, prompts or the report.
- LLM file tools are confined to the project directory, so the config file and `~/.m2/settings.xml` are out of reach; `.env` and `settings.xml` inside the project are also refused.
- Build output is passed through a redaction step (configured secrets, `user:password@` in URLs, auth headers) before any of it is shown to the LLM.

## Exit conditions (process stops with a message)

| Condition | Uses LLM retries? |
|---|---|
| Git repo with uncommitted changes (no Git: warning only) | no |
| LLM key rejected, or model can't call tools | no |
| Pre-upgrade build fails | no |
| A POM is missing or can't be parsed | no |
| Post-upgrade build fails with an environment error (classified) | no |
| Post-upgrade build fails and attempts are exhausted | yes |

Every exit after upgrades were applied prints what was changed and, in a Git repo, the command to return to the starting state (`git restore .`, plus `git clean -fd` for files the agent created), since a restart requires a clean tree. Without Git the report says the changes were made in place.

**Version lookup failures never stop the run.** Whatever the cause (not found, unreachable source, rejected credentials, timeout), that dependency is left unchanged, the reason is recorded, and the loop moves on. The report has a "could not check" section listing each one with its error so the user can act on it. If every lookup fails, no upgrades are applied and the report says so.

## Build failure classification (decided)

A `classify_failure` step sits between "build failed" and "attempts left?". It reads the Maven log (`tools/maven.py`) and returns `code` or `environment`.

- **environment**, which defers straight away with no LLM call: dependency resolution failures (`Could not resolve dependencies`, `Could not transfer artifact`, `Could not find artifact`, `Non-resolvable parent POM`), HTTP 401/403/407 and `Unauthorized`, network and TLS errors (`UnknownHostException`, `Connection refused`/`timed out`, `PKIX path building failed`), and plugin resolution failures.
- **code**, which goes to `repair_agent`: compiler errors (`COMPILATION ERROR`, `[ERROR] …/X.java:[l,c]`), test-compile errors, and anything not matched above. Unknown failures go to the LLM because the retry limit caps the cost.
- **Exception**: a resolution error for an artifact or version that the repair agent itself added or changed in the previous attempt counts as `code` and goes back to the agent, since the LLM introduced it and can fix it.
- On an environment defer, the report gives the category, a redacted excerpt of the log and a hint (check repository credentials, network/proxy, or `settings.xml` for internal libraries).

## Version lookup: Maven Central

For Maven Central the agent reads the artifact's `maven-metadata.xml` straight from the repository:

```
https://repo1.maven.org/maven2/<groupId with . replaced by />/<artifactId>/maven-metadata.xml
```

Example: `com.fasterxml.jackson.core:jackson-databind` ->
`https://repo1.maven.org/maven2/com/fasterxml/jackson/core/jackson-databind/maven-metadata.xml`

Code reads the `<versioning><versions><version>` entries (pre-releases included) with a plain API call, cached per artifact for the run. The last 25 entries of that list (roughly the newest) go into the selection prompt; the LLM picks the newest stable one, and code checks the pick against the full list (see `select_version`). The same list is available to the repair agent as the `find_versions` tool.

## Version and retry rules

1. **Major versions: applied.** The agent always goes to the newest stable release, including a new major (Spring Boot 3 -> 4, Hibernate 5 -> 6). The report marks which upgrades crossed a major version.
2. **Parent and BOM imports: upgraded, and done first**, because that is what moves the managed dependencies.
3. **Retry limit with a shared build.** Three attempts cover the errors from every upgrade together, and major jumps make that tight. The limit is configurable (default 3) and an attempt that reduces the error count does not use one up.

## Flow (LangGraph)

```
start -> preflight -> baseline_build -> extract_dependencies
                                           |
                     +-------------------> next_dependency -> [none left] --+
                     |                         |                            |
                     |               select_version (LLM)                   |
                     |                         |                            |
                     +-- apply if outdated ----+                            v
                                                              [nothing applied] -> report -> end
                                                                            |
                                                                          build <---------------+
                                                                     pass |   | fail            |
                                                           report <-------+   v                 |
                                                                       classify_failure         |
                                                                   environment |   | code       |
                                                                               |   v            |
                                                                               | attempts left? -yes-> repair_agent
                                                                               |   | no
                                                                               v   v
                                                                         defer_to_user -> report -> end
```

- **Deterministic control**: the dependency loop, build runs, classification and the attempt counter are graph logic, not LLM decisions. The LLM produces the list and picks each new version.
- **`select_version` (LLM)**: code fetches the current dependency's published versions; one LLM call returns `{status: upgrade | up_to_date, new_version, reason}` as structured output. `up_to_date` leaves the dependency unchanged. A failed lookup, an unusable answer, or a pick that is not a published version is recorded as "could not check" and the loop moves on. (The node is called `find_versions` in the code today.)
- **Upgrade unit**: one entry under one project/module key, applied at the `<version>` location `pom.py` finds for it (see "Dependency collection"). Order: root then modules; within each, parent, BOM imports, then dependencies.
- **Baseline build** runs before any change; if it already fails, the run stops and says so (nothing can be attributed to the upgrades otherwise).
- **On defer**, the project keeps all applied upgrades and the LLM's edits so the user can continue manually; the report lists every upgrade, the remaining errors, the defer reason (environment or attempts exhausted) and each attempt. In a Git repo, a full undo is `git restore .` (and `git clean -fd`).
- **Guard after repair**: the graph re-checks that the target versions are still in the POM, so the LLM cannot "fix" a build by quietly reverting an upgrade.
- **Build command**: `mvn -B -DskipTests package` (compiles main and test code, runs no tests). Configurable; the report states that tests were not run.

## OPEN: is a tool-calling LLM required?

**Unresolved; the user is thinking about how to do it.** Models without tool support (for example `codellama:7b` on Ollama) are stopped by the preflight probe.

Where tool calls are used now:
- **Build repair**: the LLM reads, searches and edits files, and inspects library APIs, through tools.
- **Preflight**: the tool-call probe exists only because of repair.
- Version selection no longer uses tools (decided 2026-10-18): code fetches the list and the LLM answers with structured output.

Points to think about (not decisions):
- Repair is harder without tools: code would have to gather the context up front (failing files, errors, library signatures) and apply edits the LLM returns as text, and the LLM could no longer look things up as it goes.
- Without tool calls the preflight probe would be dropped or changed.

## LLM scope and functions

**Version selection** is a single structured-output call: code supplies all the data in the prompt. **Repair** works through **tool calls**: it is given a task and a set of tools, decides which tools to call, our Python code runs them and returns the results, and the loop continues until the model gives its answer (`langchain.agents.create_agent` with the provider's chat model). It never gets the whole project pasted into a prompt.

### Where the LLM is used

| Step | Graph node | What the LLM does | Output | Prompts |
|---|---|---|---|---|
| 1. Version selection | `select_version` | Picks the newest stable version from the published versions given in the prompt (no pre-releases; new majors allowed) | `{status, new_version, reason}` (structured output, no tools) | `select_system.md`, `select_task.md` |
| 2. Build repair | `repair_agent` | Reads the build errors, finds the affected code, checks the new library API, edits files to fix the build | Edited files plus a short summary of what it changed and why | `repair_system.md`, `repair_task.md` |

### What the LLM does not do (deterministic code does)

- Read the POMs and collect the dependencies (`pom.py`).
- Fetch the published versions, and check the LLM's pick is one of them (`repo.py`, `version_select.py`).
- Apply version upgrades to the POM (`pom.py`).
- Run Maven, judge whether the build passed, or classify failures (`maven.py`).
- Count attempts, decide to retry or defer, or end the run (graph edges).
- Undo upgrades: after each repair the guard checks that every upgraded version is still in place.

### Tools

All tools are confined to the project directory (config file, `.env` and `settings.xml` refused), and every call is recorded in `events.jsonl`.

The tools are used by the repair agent only.

| Tool | Purpose |
|---|---|
| `list_files(path, pattern)` | list files, find module POMs and sources |
| `read_file(path)` | read a POM or Java source |
| `search_code(pattern)` | find usages of a class or method across the project |
| `edit_file(path, old, new)` | targeted replacement; every edit recorded for the report |
| `write_file(path, content)` | create or replace a file |
| `find_versions(group, artifact)` | all published versions from Maven Central (`maven-metadata.xml`), pre-releases included; lets the agent check companion or relocated artifacts (`javax` -> `jakarta`) |
| `inspect_class(group, artifact, version, fqcn)` | `javap` on the artifact's JAR (`~/.m2`, else downloaded): the real signatures in that version |
| `read_library_docs(group, artifact, version, fqcn)` | the class source (with javadoc) from the artifact's `-sources.jar` |

### Inputs each step gets

- **Selection**: group, artifact and current version of one dependency, the last 25 published versions, plus the selection rules from the prompt.
- **Repair**: redacted build errors, the applied upgrades (old -> new, major jump marked), earlier attempts with their edits and resulting errors, attempt number and limit.

### Limits and rules

- Knowledge sources: the tools above plus the model's own knowledge (e.g. known migration steps). No web access in v1; v2 adds skill files (`SKILL.md`-style migration guides) to the repair prompt.
- Credentials and secrets never reach the LLM; build output is redacted first.
- Repair may change other dependencies in `pom.xml` (add, swap, bump a companion) but not revert an upgraded version.
- Repair does not run builds itself; the graph runs exactly one build after each attempt, so the attempt count stays meaningful.
- **To confirm**: a per-step cap on tool calls (proposed `agent.max_tool_calls = 25`) so a model can't loop on reads; on hitting it, the attempt ends and the graph builds as usual.

## Layout

All paths below are under `python/`.

```
pyproject.toml                    exists: Poetry, Python >=3.11,<3.15; add [project.scripts] java-upgrade-agent and the src package entry when the CLI is written
src/java_upgrade_agent/
  cli.py                          typer: `run [project-path] --config <file>`; prompts for the project path if omitted
  config.py                       pydantic-settings, loaded from the passed file (see "Configuration file")
  llm.py                          provider -> LangChain chat model factory; tool-call probe
  session.py                      session id, session folder, logging setup, events.jsonl writer
  prompts.py                      prompt loader: user prompts_dir over built-ins, placeholder check
  prompts/                        built-in prompt files (select_*.md, repair_*.md)
  version_select.py               fetch versions, one structured-output LLM call, published-version check
  state.py                        graph state + models (Dependency, DependencyMap, QueueItem, AppliedUpgrade, FailureKind)
  graph.py                        StateGraph wiring and conditional edges
  nodes.py                        node functions above, including classify_failure
  repair.py                       repair agent construction and prompt
  tools/
    pom.py                        read root and module POMs; collect versioned dependencies; find where a group:artifact's version is defined (literal or ${property}); in-place version edit (formatting kept); used by extraction, apply and the guard
    repo.py                       VersionSource interface; maven-metadata.xml client for Maven Central (Artifactory connector: TODO)
    maven.py                      run the build, capture log, parse compiler errors, classify failure (code | environment)
    redact.py                     strip secrets from logs before they reach the LLM
    gitops.py                     repo/clean-tree checks, diff
    files.py                      sandboxed read/list/search/edit
    library.py                    javap inspection, javadoc/sources JAR lookup
  report.py                       markdown report built from the session's events.jsonl: per upgrade before/after, result, files changed, attempts, defer reason; skipped (managed, already latest)
tests/
  fixtures/sample-boot-app/       small multi-module Spring Boot Maven project with outdated dependencies (parent, BOM import, literal and property versions, a shared property)
  fixtures/maven-logs/            recorded Maven outputs: compile error, 401, artifact not found, unknown host, mixed
  test_*.py                       per tools module; graph tests with a fake chat model
```

Dependencies, as in `python/pyproject.toml` today: `langchain`, `langgraph`, `langchain-community`, `langchain-anthropic`, `langchain-openai`, `langchain-google-genai`, `langchain-ollama`.

Added when the module that needs them is built: `lxml` (`pom.py`), `httpx` (`repo.py`), `pydantic-settings` (`config.py`), `typer` and `rich` (`cli.py`, logging); dev group: `pytest`, `respx`, `ruff`.

## Build order

1. Replace the repo's `plan.md` with this plan. Scaffold in `python/` (git already initialised, `pyproject.toml` exists): package layout, dependencies, config, `session.py`, CLI skeleton, example config file, fixture project, `.gitignore`.
2. `pom.py`, `repo.py` with unit tests (recorded metadata responses).
3. `maven.py` (including `classify_failure`), `gitops.py`; preflight and baseline build.
4. `llm.py` (provider factory + preflight probe), `prompts.py` + built-in prompts, dependency collection in `pom.py`, `version_select.py`.
5. Graph: extraction, dependency loop, `apply_version`, single `build`, `classify_failure`, plus `report.py` (no repair yet: a failed build goes straight to defer).
6. `files.py` edit tools, `library.py`, `repair.py`: tool-calling repair agent with attempt history and the 3-attempt limit.
7. End-to-end run on the fixture.

## Things to settle during implementation

- **JDK**: this machine has only Java 27 and `JAVA_HOME` is unset. Projects targeting Java 17/21 need a matching JDK, passed as `jdk_home`; preflight warns when the JDK is newer than the project's configured release.
- **Python 3.14 wheels** for `lxml` and the provider packages' dependencies: confirmed at `poetry install` / `poetry add`.
- **Model id**: from config; Anthropic default set after checking the `claude-api` skill.

## Verification

- `poetry run pytest`: POM read/edit (direct version, property version, shared property, no version, parent, BOM import), metadata client for Maven Central, Maven log parsing, failure classification on each recorded log, preflight refusing a dirty tree, LLM factory building the right class per provider, API keys redacted, config validation (bad values, `data_dir` inside the project), prompt override and fallback plus the missing-placeholder error, session folder contents and a report rebuilt from a recorded `events.jsonl`.
- Dependency collection tests: single-module (one key); multi-module (root plus every module, same artifact under two modules); properties resolved through parents; own modules, profiles and plugins skipped; an inherited property is edited once and the second entry is recorded as "applied via"; an entry `pom.py` can't locate is recorded as "could not apply".
- Version selection tests with a fake chat model: a valid pick is accepted; an unpublished pick is rejected; an empty or unparsable answer is recorded as "could not check".
- Graph tests with a fake chat model: nothing outdated; build passes first time; fails then repaired; attempts exhausted and deferred with a report; environment failure deferred with no LLM call; LLM-introduced bad artifact goes back to the agent; LLM reverting a version is caught by the guard.
- End to end: `poetry run java-upgrade-agent run tests/fixtures/sample-boot-app --config tests/fixtures/central.toml` against real Maven Central; check `dependencies.json` against the fixture's POMs by hand, the report, and that `git restore .` in the fixture returns it to the starting state. Repeat the repair path with at least one cloud provider and one Ollama model that supports tools.
