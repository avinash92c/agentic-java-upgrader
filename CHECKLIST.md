# Implementation checklist: Java upgrade agent v1

Derived from [`plan.md`](plan.md). Code paths are relative to `python/src/java_upgrade_agent/`.

## Open decisions

- [ ] Is a tool-calling LLM required? Now used only by repair and the preflight probe (see plan.md "OPEN: is a tool-calling LLM required?")
- [ ] Per-step cap on LLM tool calls (proposed `agent.max_tool_calls = 25`; repair uses a fixed recursion limit of 60 today)
- [ ] Implementation language (Python for now; Go and Java under evaluation)
- [x] Build command: `mvn -B clean install -DskipTests` (tests left to the user)
- [ ] Git with uncommitted changes: still stops the run; make it a warning too?
- [x] Agent builder: `langchain.agents.create_agent` (LangChain 1.x)
- [x] Versioned `<dependencyManagement>` entries are included in the dependency map
- [x] Repair works one issue at a time (issue list per build, rebuild after each round)
- [x] A command-line model (Claude CLI) as the main `[llm]`: tried and removed; the CLI stays a read-only consultant so every edit goes through the agent and is tracked

## 0. Project setup

- [x] `pyproject.toml` with Poetry, Python `>=3.11,<3.15`, LangChain/LangGraph and the four provider packages
- [x] `[tool.poetry] packages = [{ include = "java_upgrade_agent", from = "src" }]`
- [x] `readme` line removed (`poetry check` no longer needs `README.md`)
- [ ] Declare `httpx`, `truststore` and `pyyaml` in `pyproject.toml` (`poetry add` blocked by Norton HTTPS scanning; they come in transitively for now)
- [ ] `[project.scripts] java-upgrade-agent = ...` once the CLI exists
- [ ] Dev group: `pytest`, `respx`, `ruff`
- [ ] `.gitignore` (`.venv/`, `__pycache__/`, `dist/`, `build/`)
- [ ] Remove unused LLM extraction: `extract.py`, `prompts/extract_system.md`, `prompts/extract_task.md`
- [ ] Remove unused prompts: `repair_dependency_system.md`, `repair_dependency_task.md`, `repair_pom_system.md` (check `repair_code_*` too)

## 1. Configuration, LLM, logging and sessions

- [x] `llm.py`: `LLMSettings` and `create_chat_model()` for anthropic / openai / google / ollama
- [x] `config.example.toml`: `[repository]` (Maven Central), `[llm]`, optional `[repair_llm]` and `[consultant]`, `[agent] max_retries`, `hints_file`
- [x] Optional `[repair_llm]`: a second (stronger) model used only in the repair rounds
- [x] `__main__.py`: `python -m java_upgrade_agent <project> --config <file> [-v] [--log-file] [--report]`, prints the report and saves it (never inside the project)
- [x] `tracing.py`: logging setup; `AgentTraceLogger` logs every LLM request/response (full messages, thinking, token counts, raw metadata) and tool call at DEBUG
- [x] Log lines in every node; Maven Central URLs logged
- [ ] `config.py`: load and validate the full config (`[build]`, `[agent] data_dir`, `prompts_dir`); env vars override secrets
- [ ] `session.py`: session id, session folder under `data_dir/sessions/`, `agent.log`, `config.json` (secrets removed), `events.jsonl`
- [ ] `cli.py`: proper CLI (`run [project-path] --config <file>`; prompt for the path if omitted)

## 2. POMs, versions and repository

- [x] `tools/pom.py`: read root and module POMs (standard layout), collect versioned dependencies in code (properties resolved; parent, BOM imports, managed and direct deps; own modules, plugins, profiles skipped)
- [x] `tools/pom.py`: locate a version (literal or `${property}`, module or parent POM) and edit it in place, keeping formatting and line endings
- [x] Shared or inherited property edited once; later entries recorded as "applied via"; not found → "could not apply"
- [x] `tools/repo.py`: `VersionSource`; Maven Central `maven-metadata.xml` client (OS certificate store), per-run cache, `fetch()` for JARs, `find_versions` tool (repair only)
- [x] `version_select.py`: code fetches the versions, one structured-output LLM call picks (last 25 versions in the prompt), code rejects a version that is not published
- [x] Prompts `select_system.md` (step-by-step rules with example), `select_task.md`
- [ ] Rename the `find_versions` node to `select_version` (plan name)

## 3. Build, preflight and failure classification

- [x] Preflight: Git optional (warning without it; uncommitted changes stop the run), LLM tool-call probe for `[llm]` and `[repair_llm]`
- [x] Java version check: if the JDK can't compile the project's Java version, ask to update it to 17 / 21 / 25 (`n` stops the run); the baseline build is skipped after an update
- [x] `run_maven`: baseline build and post-upgrade build (`mvn -B clean install -DskipTests`), log captured
- [x] Sources JARs downloaded after every build (`dependency:sources`), resolved versions recorded for API notes
- [x] `classify_failure`: environment patterns (resolution, 401/403/407, network, TLS) vs code; pom.xml edited by the last repair → code
- [ ] Redaction of secrets in build logs before they reach the LLM
- [x] JDK too new for the project's release → asks to update the Java version (see preflight)

## 4. Graph and report

- [x] `state.py`: graph state and models
- [x] `graph.py`: `StateGraph` wiring and conditional edges
- [x] Loop order: root then modules; within each, parent, BOM imports, dependencies
- [x] Lookup and selection failures never stop the run; recorded as "could not check"
- [x] `report` node: markdown report (Java version update, upgrades with major jumps, applied via / could not apply / could not check, repair rounds with API notes, remaining issues, undo hint for Git / non-Git)
- [ ] Report written to the session folder, built from `events.jsonl`
- [ ] `prompts.py`: user `prompts_dir` overrides, placeholder check at startup

## 5. Repair

- [x] `tools/files.py`: `list_files`, `read_file`, `search_code`, `edit_file`, `write_file`; confined to the project, `.env` / `settings.xml` refused, edits recorded
- [x] `tools/library.py`: `inspect_class` (javap), `read_library_docs` (sources JAR); `~/.m2` first, then Maven Central
- [x] `repair.py` and prompts `repair_system.md`, `repair_task.md`
- [x] Issue list per build: each error is categorised `pom`, `dependency` (`package X does not exist`), `code` or `other`
- [x] One issue per round (plus other issues with the same error signature), rebuild after each round
- [x] `max_retries` = tries per issue; safety cap of 50 rounds per run
- [x] Detailed log of which issue each round is fixing
- [x] Renamed dependencies: BOM comparison renames an artifact the new BOM dropped (no LLM)
- [x] Dependency issues: Maven Central class search for the artifact providing the missing package
- [x] `compile_fix.py`: structured fix (`add_dependencies` inserted into the right `pom.xml` by code, `edits` as line edits or text replacements); tolerant matching (indentation, CRLF, whitespace, copied line prefixes)
- [x] Wider code context: small files whole, larger files a window around the error plus imports and constructors
- [x] API notes from the sources JARs: old declaration with javadoc and `@deprecated`, new class methods across the type hierarchy
- [x] Consultant (`consultant.py`): Claude Code / Gemini CLI / Codex CLI, read-only in the project folder, gives advice in CHANGE blocks; the agent's model applies the fix
- [x] Learning (`learning.py`): fixes verified by the next build are saved to `migration_hints.yaml` and offered in later runs (not for syntax-error fixes, failed rounds, or when a new error appears within 3 lines)
- [x] Guard: reverted upgrade versions are put back and recorded

## 6. Tests and end-to-end

- [ ] Fixture: multi-module Spring Boot project (parent, BOM import, literal and property versions, shared property)
- [ ] Fixture: recorded Maven logs (compile error, 401, artifact not found, unknown host, mixed)
- [ ] Unit tests: POM collect/locate/edit, Maven Central client, classification, version check, files sandbox, edit matching, learning, report
- [ ] Graph tests with a fake chat model
- [ ] End-to-end run on the fixture (Maven Central, one cloud provider, one Ollama model)
- [x] Real-project runs: a Spring/Struts project (Java 7 -> 17, build passes after 5 rounds) and a Spring AI project (1.1.5 -> 2.0.2, build passes)

## To do

- [ ] Dependency CVE checker and fixer
- [ ] Gradle support
- [ ] Brain LLM: Claude / Gemini / ChatGPT CLI mode as consultant
- [ ] Run the unit tests before closing a run

## Later (v2 and beyond)

- [ ] Artifactory or other in-house repository connector (`VersionSource`; URL and credentials in config, used only in the connector)
- [ ] `SKILL.md`-style skill files loaded into the repair prompt
- [ ] Distribution (wheel / standalone build)
- [ ] Deterministic version selection (code picks the newest stable version, no LLM call)
- [ ] Javadoc of candidate replacement methods in the API notes (see `compile-fix-search-notes.md`)
- [ ] Fixing failing unit tests
- [ ] Additional language support
