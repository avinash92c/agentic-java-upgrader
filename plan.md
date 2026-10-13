# Plan: Java upgrade agent v1 (LangGraph, Maven)

## Context

The workspace `D:\Dev\workspaces\vscode\java-upgrade-agent` holds only a bare `pyproject.toml` (Poetry, Python >=3.14, no dependencies, not a git repo). v1 implements the user's flow diagram: read the project's `pom.xml`, find newer versions, apply them one at a time, build, let an LLM repair build failures up to 3 times, then report. The fuller spec (tree diff, class-file check, vulnerability scan, commit handoff) is later work.

## Decisions (from the user)

- Agent loop on **LangChain + LangGraph**.
- Version source selectable: **Maven Central or JFrog Artifactory**.
- User runs the agent **on a clean Git branch they can revert**. The agent does not create branches or commit; it checks at startup that the target is a Git repo with a clean tree.
- **Latest stable only**: no milestone, RC, alpha, beta, nightly, preview or SNAPSHOT versions.
- **Only upgrade what has a version tag.** Dependencies without one are managed by the parent/BOM and move when that is upgraded.
- **Versions are looked up and applied one dependency at a time** (a loop in the graph); **the build runs once, after all upgrades are applied**.
- **Build success = Maven build without running tests.** Running tests is left to the user (config switch available).
- **LLM works through tool calls** (read POM, read Java classes, look up versions, consult javadocs) and is expected to fix build issues from documentation or its own knowledge.
- **`pom.xml` only** for v1; Gradle later.
- **Defer = stop the process and exit with a message.** No partial undo, no resume; the user restarts the flow from the beginning.
- **Credentials live in the agent's own config file, never read from `~/.m2/settings.xml`**, and must not reach the LLM. A better approach for secrets is deferred.

## Credentials and the LLM

- The user passes the config file to the agent (`--config <path>`). It selects the repository (Maven Central or JFrog Artifactory) and holds the JFrog URL and any API key or user credentials. Environment variables can override the secret values.
- If the passed config file sits inside the project being upgraded, the agent warns (it could be committed) and blocks that exact path from the LLM file tools.
- Only the repository client (Python code) reads credentials. They are not put in graph state, prompts or the report.
- LLM file tools are confined to the project directory, so the config file and `~/.m2/settings.xml` are out of reach; `.env` and `settings.xml` inside the project are also refused.
- Build output is passed through a redaction step (configured secrets, `user:password@` in URLs, auth headers) before any of it is shown to the LLM.

## Exit conditions (process stops with a message)

| Condition | Uses LLM retries? |
|---|---|
| Not a Git repo, or working tree not clean | no |
| Pre-upgrade build fails | no |
| Post-upgrade build fails and attempts are exhausted | yes |

Every exit after upgrades were applied prints what was changed and the command to return to the starting state (`git restore .`), since a restart requires a clean tree.

**Version lookup failures never stop the run.** Whatever the cause (not found, unreachable source, rejected credentials, timeout), that dependency is left unchanged, the reason is recorded, and the loop moves on. The report has a "could not check" section listing each one with its error so the user can act on it. If every lookup fails, no upgrades are applied and the report says so.

## OPEN: build failures the LLM cannot fix

**Unresolved, user will come back to this.** Some post-upgrade build failures are not code problems: an artifact that cannot be downloaded, a 401/403 from the repository, a network or proxy error, an internal library that needs `settings.xml` configuration. Sending these to the LLM wastes all retry attempts on something it cannot change. The proposed fix is a check between "build failed" and "retry": classify the failure from the Maven output, and send only compilation errors to the LLM while the rest go straight to defer. **Until this is decided, v1 sends every post-upgrade build failure through the retry loop.**

## OPEN: mvnrepository.com as a version source

**Unresolved, user will come back to this.** Plain requests to `mvnrepository.com/artifact/<group>/<artifact>` return HTTP 403 with a Cloudflare bot challenge (tested from this machine). Until it is solved, v1 reads `maven-metadata.xml` from the selected repository (works for both Central and Artifactory). Version lookup sits behind a `VersionSource` interface so a mvnrepository source can be added without touching the flow.

## Version and retry rules

All settled:

1. **Major versions: applied.** The agent always goes to the newest stable release, including a new major (Spring Boot 3 -> 4, Hibernate 5 -> 6). The report marks which upgrades crossed a major version.
2. **Parent and BOM imports: upgraded, and done first**, because that is what moves the managed dependencies.
3. **Retry limit with a shared build.** Three attempts cover the errors from every upgrade together, and major jumps make that tight. The limit is configurable (default 3) and an attempt that reduces the error count does not use one up.

## Flow (LangGraph)

```
start -> preflight -> baseline_build -> read_pom
                                           |
                     +-------------------> next_dependency -> [none left] --+
                     |                         |                            |
                     |                  find latest stable                  |
                     |                         |                            |
                     +-- apply if outdated ----+                            v
                                                              [nothing applied] -> report -> end
                                                                            |
                                                                          build <-----------+
                                                                     pass |   | fail        |
                                                           report <-------+   v             |
                                                             |          attempts left? -yes-> repair_agent
                                                            end               | no
                                                                        defer_to_user -> report -> end
```

- **Deterministic control**: the dependency loop, build runs and the attempt counter are graph logic, not LLM decisions.
- **Upgrade unit**: one `<version>` location. A `${property}` shared by several dependencies is one unit. Order: parent, BOM imports, then dependencies.
- **Baseline build** runs before any change; if it already fails, the run stops and says so (nothing can be attributed to the upgrades otherwise).
- **On defer**, the project keeps all applied upgrades and the LLM's edits so the user can continue manually; the report lists every upgrade, the remaining errors and each attempt. A full undo is `git restore .` on the user's clean branch.
- **Guard after repair**: the graph re-checks that the target versions are still in the POM, so the LLM cannot "fix" a build by quietly reverting an upgrade.
- **Build command**: `mvn -B -DskipTests package` (compiles main and test code, runs no tests). Configurable; the report states that tests were not run.

## LLM tools

`repair_agent` is a tool-calling agent (LangGraph prebuilt ReAct agent) given the new build errors, the upgrade being applied and the history of earlier attempts. Tools, all confined to the project directory:

| Tool | Purpose |
|---|---|
| `read_pom` | parsed dependencies, properties, parent |
| `read_file`, `list_files`, `search_code` | locate and read affected Java sources |
| `edit_file`, `write_file` | apply a fix; every edit is recorded for the report |
| `find_versions(group, artifact)` | stable versions from the selected repository |
| `inspect_class(fqcn)` | `javap` on the upgraded JAR in `~/.m2`: the real signatures in the new version |
| `read_library_docs(group, artifact, version, fqcn)` | javadoc/source from the artifact's `-javadoc.jar` / `-sources.jar` in the repository |

`find_versions` is the same function the `find_versions` graph node calls directly for the initial scan; exposing it as a tool lets the agent check companion artifacts during a repair (for example a relocated artifact ID).

## Layout

```
pyproject.toml                    add deps, [project.scripts] java-upgrade-agent
src/java_upgrade_agent/
  cli.py                          typer: `run [project-path] --config <file>`; prompts for the project path if omitted
  config.py                       pydantic-settings, loaded from the passed file: repository (central|artifactory), Artifactory URL/token, max_retries=3, build_command, jdk_home, model
  state.py                        graph state + models (UpgradeUnit, Candidate, BuildError, Attempt, UpgradeResult)
  graph.py                        StateGraph wiring and conditional edges
  nodes.py                        node functions above
  repair.py                       repair agent construction and prompt
  tools/
    pom.py                        read parent/BOM/dependencies/properties; resolve ${property}; in-place version edit (lxml, formatting kept)
    versions.py                   Maven version ordering, stable filter
    repo.py                       VersionSource interface; maven-metadata.xml client for Central and Artifactory
    maven.py                      run the build, capture log, parse compiler errors
    redact.py                     strip secrets from logs before they reach the LLM
    gitops.py                     repo/clean-tree checks, diff
    files.py                      sandboxed read/list/search/edit
    library.py                    javap inspection, javadoc/sources JAR lookup
  report.py                       markdown report: per upgrade before/after, result, files changed, attempts; skipped (managed, already latest)
tests/
  fixtures/sample-boot-app/       small Spring Boot Maven project with outdated dependencies
  test_*.py                       per tools module; graph tests with a fake chat model
```

Dependencies: `langgraph`, `langchain`, `langchain-anthropic`, `httpx`, `lxml`, `pydantic-settings`, `typer`, `rich`; dev: `pytest`, `respx`, `ruff`.

## Build order

1. Scaffold: `git init` in the agent repo, package layout, dependencies, config, CLI skeleton, fixture project.
2. `pom.py`, `versions.py`, `repo.py` with unit tests (recorded metadata responses).
3. `maven.py`, `gitops.py`; preflight and baseline build.
4. Graph: dependency loop, `apply_version`, single `build`, plus `report.py` (no LLM yet: a failed build goes straight to defer).
5. `files.py`, `library.py`, `repair.py`: tool-calling repair agent with attempt history and the 3-attempt limit.
6. End-to-end run on the fixture.

## Things to settle during implementation

- **JDK**: this machine has only Java 27 and `JAVA_HOME` is unset. Projects targeting Java 17/21 need a matching JDK, passed as `jdk_home`; preflight warns when the JDK is newer than the project's configured release.
- **Python 3.14 wheels** for `lxml` and LangGraph: confirmed at `poetry add`; if one is missing, relax `requires-python` to `>=3.12`.
- **Model id**: from config; default set after checking the `claude-api` skill.

## Verification

- `poetry run pytest`: version ordering and stable filter, POM read/edit (direct version, property version, shared property, no version, parent, BOM import), metadata client for both sources, Maven log parsing, preflight refusing a dirty tree.
- Graph tests with a fake chat model: nothing outdated; build passes first time; fails then repaired; attempts exhausted and deferred with a report; LLM reverting a version is caught by the guard.
- End to end: `poetry run java-upgrade-agent run tests/fixtures/sample-boot-app --config tests/fixtures/central.toml` against real Maven Central; check the report and that `git restore .` in the fixture returns it to the starting state.
