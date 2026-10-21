# Implementation checklist: Java upgrade agent v1

Derived from [`plan.md`](plan.md). Code paths are relative to `python/src/java_upgrade_agent/`.

## Open decisions

- [ ] Is a tool-calling LLM required? Now used only by repair and the preflight probe (see plan.md "OPEN: is a tool-calling LLM required?")
- [ ] Per-step cap on LLM tool calls (proposed `agent.max_tool_calls = 25`; repair uses a fixed recursion limit of 60 today)
- [ ] Implementation language (Python for now; Go and Java under evaluation)
- [ ] Build command: code runs `mvn -B clean install` (runs tests); plan says `mvn -B -DskipTests package`
- [ ] Git with uncommitted changes: still stops the run; make it a warning too?
- [x] Agent builder: `langchain.agents.create_agent` (LangChain 1.x)
- [x] Versioned `<dependencyManagement>` entries are included in the dependency map

## 0. Project setup

- [x] `pyproject.toml` with Poetry, Python `>=3.11,<3.15`, LangChain/LangGraph and the four provider packages
- [x] `[tool.poetry] packages = [{ include = "java_upgrade_agent", from = "src" }]`
- [x] `readme` line removed (`poetry check` no longer needs `README.md`)
- [ ] Declare `httpx` and `truststore` in `pyproject.toml` (`poetry add` blocked by Norton HTTPS scanning)
- [ ] `[project.scripts] java-upgrade-agent = ...` once the CLI exists
- [ ] Dev group: `pytest`, `respx`, `ruff`
- [ ] `.gitignore` (`.venv/`, `__pycache__/`, `dist/`, `build/`)
- [ ] Remove unused LLM extraction: `extract.py`, `prompts/extract_system.md`, `prompts/extract_task.md`

## 1. Configuration, LLM, logging and sessions

- [x] `llm.py`: `LLMSettings` and `create_chat_model()` for anthropic / openai / google / ollama
- [x] `config.example.toml`: `[repository]` (Maven Central), `[llm]`, `[agent] max_retries`
- [x] `__main__.py`: `python -m java_upgrade_agent <project> --config <file> [-v] [--log-file]`, prints the report
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

- [x] Preflight: Git optional (warning without it; uncommitted changes stop the run), LLM tool-call probe
- [x] `run_maven`: baseline build and post-upgrade build (`mvn -B clean install`), log captured
- [x] `classify_failure`: environment patterns (resolution, 401/403/407, network, TLS) vs code; pom.xml edited by the last repair → code
- [ ] Redaction of secrets in build logs before they reach the LLM
- [ ] JDK newer than the project's release → warning

## 4. Graph and report

- [x] `state.py`: graph state and models
- [x] `graph.py`: `StateGraph` wiring and conditional edges
- [x] Loop order: root then modules; within each, parent, BOM imports, dependencies
- [x] Lookup and selection failures never stop the run; recorded as "could not check"
- [x] `report` node: markdown report in state (upgrades with major jumps, applied via / could not apply / could not check, repair attempts, remaining errors, undo hint for Git / non-Git)
- [ ] Report written to the session folder, built from `events.jsonl`
- [ ] `prompts.py`: user `prompts_dir` overrides, placeholder check at startup

## 5. Repair

- [x] `tools/files.py`: `list_files`, `read_file`, `search_code`, `edit_file`, `write_file`; confined to the project, `.env` / `settings.xml` refused, edits recorded
- [x] `tools/library.py`: `inspect_class` (javap), `read_library_docs` (sources JAR); `~/.m2` first, then Maven Central
- [x] `repair.py` and prompts `repair_system.md`, `repair_task.md`
- [x] Retry limit (`max_retries`); an attempt that reduces the error count doesn't use one up
- [x] Guard: reverted upgrade versions are put back and recorded

## 6. Tests and end-to-end

- [ ] Fixture: multi-module Spring Boot project (parent, BOM import, literal and property versions, shared property)
- [ ] Fixture: recorded Maven logs (compile error, 401, artifact not found, unknown host, mixed)
- [ ] Unit tests: POM collect/locate/edit, Maven Central client, classification, version check, files sandbox, report
- [ ] Graph tests with a fake chat model
- [ ] End-to-end run on the fixture (Maven Central, one cloud provider, one Ollama model)

## Later (v2 and beyond)

- [ ] Artifactory or other in-house repository connector (`VersionSource`; URL and credentials in config, used only in the connector)
- [ ] `SKILL.md`-style skill files loaded into the repair prompt
- [ ] Gradle support
- [ ] Distribution (wheel / standalone build)
