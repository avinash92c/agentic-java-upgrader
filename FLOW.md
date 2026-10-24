# Agent flow (current code)

Every step of a run, from the command line to the report. Node names are the LangGraph nodes in
`python/src/java_upgrade_agent/graph.py`; the code is in `nodes.py` unless another file is named.
All routing is graph code, not LLM decisions.

```
 ╔══════════════════════════════════════════════════════════════════════════════════════════════════════╗
 ║ START   python -m java_upgrade_agent <project-dir> --config config.toml [-v] [--log-file] [--report]  ║
 ╚══════════════════════════════════════════════════════════════════════════════════════════════════════╝
   │  __main__.py
   │   • logging set up (-v: every LLM request/response, tool call, Maven Central URL)
   │   • [llm]        -> chat model (anthropic | openai | google | ollama)           llm.py
   │   • [repair_llm] -> optional second model for repair rounds (else [llm])
   │   • [consultant] -> optional CLI settings (claude | gemini | codex | own command) consultant.py
   │   • [agent]      -> max_retries (tries per issue, default 3), hints_file (~/.java-upgrade-agent/...)
   │   • state = {project_dir, max_retries, hints_file}; run config = {llm, repair_llm, consultant}
   ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ preflight                                                                                            │
 │  1. git rev-parse --is-inside-work-tree                                                              │
 │       not a repo / git missing ──> WARNING "changes are made in place", continue                     │
 │       repo + git status --porcelain not empty ──> STOP "uncommitted changes" ─────────────────┐      │
 │  2. no [llm] ──> STOP ────────────────────────────────────────────────────────────────────────┤      │
 │  3. tool-call probe: model.bind_tools([ping]).invoke("Call the ping tool now.")               │      │
 │       for [llm], and for [repair_llm] if set                                                  │      │
 │       error, or no tool_calls in the reply ──> STOP "use a model that supports tool calling" ─┤      │
 │  4. Java version check (tools/java_version.py)                                                │      │
 │       project version: maven.compiler.release/source/target, java.version, compiler plugin    │      │
 │       JDK version: mvn -v                                                                     │      │
 │       unknown ──> skip check                                                                  │      │
 │       project >= oldest release the JDK supports ──> OK                                       │      │
 │       too old ──> ask:  "Update the project to a newer Java version? [17/21/25/n]"            │      │
 │                    n / no / not interactive ──> STOP "update declined" ───────────────────────┤      │
 │                    17 | 21 | 25 (default: first) ──> update_version(): rewrite in every POM   │      │
 │                                                     java_update = {from, to, files}           │      │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
   │ preflight_ok                                                                                │
   ▼                                                                                             │
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐ │
 │ baseline_build                                                                               │ │
 │   java_update set? ──yes──> SKIP the build (old code is repaired later)                      │ │
 │                             still: mvn -B dependency:sources -> baseline_sources {g:a: ver}  │ │
 │   no ──> run_maven():                                                                        │ │
 │            mvn -B clean install -D skipTests            (log captured)                       │ │
 │            mvn -B dependency:sources                    (sources JARs into ~/.m2;            │ │
 │                                                          resolved versions -> baseline_sources)│
 │          FAILED ──> exit_reason "baseline build failed before any upgrade" ─────────────────────┤
 └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
   │ baseline_ok                                                                                 │
   ▼                                                                                             │
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐ │
 │ extract_dependencies                                     tools/pom.py  ProjectPoms           │ │
 │   read root pom.xml + every <modules> pom.xml (standard layout)                              │ │
 │   collect versioned: parent, BOM imports, <dependencyManagement>, <dependencies>             │ │
 │   ${property} resolved through the POM and its parents                                       │ │
 │   skipped: own modules, plugins, profiles, entries without a version                         │ │
 │   order: root first, then modules; within each: parent, BOM imports, dependencies            │ │
 │   -> dependencies {"project/module": [{group, artifact, version}]}, queue, applied = []      │ │
 │   POM missing / unparsable ──> exit_reason "could not read the POMs" ────────────────────────────┤
 └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
   │ extraction_ok                                                                               │
   ▼                                                                                             │
 ┌───────────────────────────── UPGRADE LOOP (one dependency per pass) ─────────────────────────┐ │
 │                                                                                              │ │
 │   ┌─────────────────────┐   queue empty                                                      │ │
 │ ┌>│ next_dependency     │──────────────┬── applied or java_update ──> build (below)           │ │
 │ │ │  current = queue[0] │              └── nothing changed ─────────────────────────────────────┤
 │ │ └─────────────────────┘                                                                    │ │
 │ │   │ current                                                                                │ │
 │ │   ▼                                                                                        │ │
 │ │ ┌──────────────────────────────────────────────────────────────────────────────────────┐   │ │
 │ │ │ find_versions                                          version_select.py              │   │ │
 │ │ │   cached group:artifact? ──> reuse the earlier answer (no lookup, no LLM)             │   │ │
 │ │ │   GET repo1.maven.org/maven2/<g>/<a>/maven-metadata.xml   (tools/repo.py, cached,     │   │ │
 │ │ │                                                            OS certificate store)      │   │ │
 │ │ │   last 25 versions + current ──> ONE structured LLM call ([llm])                      │   │ │
 │ │ │       prompts select_system.md / select_task.md                                       │   │ │
 │ │ │       rules: newest stable; no alpha/beta/M/RC/SNAPSHOT; major jumps allowed          │   │ │
 │ │ │       -> {status: upgrade | up_to_date, new_version, reason}                          │   │ │
 │ │ │   code check: chosen version must be in the published list                            │   │ │
 │ │ │   lookup/LLM error ──> could_not_check += reason (never stops the run)                │   │ │
 │ │ └──────────────────────────────────────────────────────────────────────────────────────┘   │ │
 │ │   │ new version?                                                                           │ │
 │ │   ├── no (up to date / failed) ───────────────────────────────────────────────────┐        │ │
 │ │   ▼ yes                                                                           │        │ │
 │ │ ┌──────────────────────────────────────────────────────────────────────────────┐  │        │ │
 │ │ │ apply_version                                            tools/pom.py         │  │        │ │
 │ │ │   locate where the module's version is defined:                               │  │        │ │
 │ │ │     literal <version> or ${property}, in the module or a parent POM           │  │        │ │
 │ │ │   not found ──> apply_notes "could not apply"                                 │  │        │ │
 │ │ │   already edited (shared/inherited property) ──> apply_notes "applied via X"  │  │        │ │
 │ │ │   else write the value in place (formatting + line endings kept)              │  │        │ │
 │ │ │        applied += {module, group, artifact, old, new}                         │  │        │ │
 │ │ └──────────────────────────────────────────────────────────────────────────────┘  │        │ │
 │ │   │                                                                               │        │ │
 │ └───┴───────────────────────────────────────────────────────────────────────────────┘        │ │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
                                                                                                 │
   ┌─────────────────────────────────────────────────────────────────────────────────────────┐   │
   │ build                                                                                   │   │
 ┌>│   run_maven(): mvn -B clean install -D skipTests ; mvn -B dependency:sources            │   │
 │ │                -> build_log, current_sources {g:a: version}                             │   │
 │ │   LEARNING CHECK (if the previous repair round left a pending fix):                     │   │
 │ │     its error signature still in this build ─────────> not recorded                     │   │
 │ │     a new error within 3 lines of a fixed place ─────> not recorded                     │   │
 │ │     otherwise ──> learning.record() -> migration_hints.yaml                             │   │
 │ │                   {signature, kind, library, versions, fix, changes, source,            │   │
 │ │                    verified: build, date}   (skipped if the same fix is already there)  │   │
 │ └─────────────────────────────────────────────────────────────────────────────────────────┘   │
 │     │ passed ─────────────────────────────────────────────────────────────────────────────────┤
 │     ▼ failed                                                                                  │
 │ ┌─────────────────────────────────────────────────────────────────────────────────────────┐   │
 │ │ classify_failure                                                                        │   │
 │ │   ENVIRONMENT pattern in the log? (Could not resolve/transfer/find artifact,            │   │
 │ │     Non-resolvable parent, 401/403/407, Unauthorized, UnknownHost, Connection           │   │
 │ │     refused/timed out, PKIX, plugin resolution)                                         │   │
 │ │       no ──> code                                                                       │   │
 │ │       yes, but the last repair round edited a pom.xml ──> code (the agent caused it)    │   │
 │ │       yes ──> environment                                                               │   │
 │ └─────────────────────────────────────────────────────────────────────────────────────────┘   │
 │     │                                                                                         │
 │     ├── environment ─────────────────────────────────────────────────────────────┐            │
 │     ├── attempts >= 50 (MAX_REPAIR_ROUNDS) ──────────────────────────────────────┤            │
 │     ├── every issue used its max_retries tries ──────────────────────────────────┤            │
 │     ▼ an issue with tries left                                                   │            │
 │ ┌─────────────────────────────────────────────────────────────────────────────────────────┐   │
 │ │ repair_agent  (ONE ISSUE PER ROUND)                                                     │   │
 │ │                                                                                         │   │
 │ │ 1. ISSUE LIST  repair.build_issues(build_log), in fix order:                            │   │
 │ │      pom         'dependencies.dependency.version' missing, Could not find artifact,    │   │
 │ │                  Non-resolvable ...  (block compilation, so first)                      │   │
 │ │      dependency  compile error "package X does not exist"   (sorted by file, line)      │   │
 │ │      code        other compile errors file:[line,col] + symbol/location (by file, line) │   │
 │ │      other       any other [ERROR] line; "build failed (see log)" if none matched       │   │
 │ │    issue = first one with tries < max_retries                                           │   │
 │ │                                                                                         │   │
 │ │ 2. GROUP  every issue of the same kind with the same signature (error text without      │   │
 │ │           paths/lines) and tries left -> fixed together; tries +1 for each member       │   │
 │ │                                                                                         │   │
 │ │ 3. LOG    round N, issues gone/new since last round, whole list with "tried k/n" or     │   │
 │ │           "set aside", issue details, code at the error                                 │   │
 │ │                                                                                         │   │
 │ │ 4. dependency issue ──> candidates: classes the file imports from the package,          │   │
 │ │      searched on Maven Central (search.maven.org fc:), javax.* also as jakarta.*        │   │
 │ │                                                                                         │   │
 │ │ 5. CONSULTANT (if [consultant]; when = always, or after_first_try and try > 1)          │   │
 │ │      CLI runs read-only in the project folder, prompt on stdin (consultant_task.md):    │   │
 │ │      error at every place, code/POM excerpt, upgrades + Java update, candidates,        │   │
 │ │      earlier tries  ──> advice in CHANGE blocks (file, lines, current, replace with /   │   │
 │ │      add dependency). It never edits files. Timeout/failure -> no advice.               │   │
 │ │                                                                                         │   │
 │ │ 6. FIX by kind  (model = [repair_llm] or [llm])                                         │   │
 │ │                                                                                         │   │
 │ │   pom ────────┬─ no advice: _rename_for_issue (no LLM): missing artifact that an        │   │
 │ │               │    upgraded BOM clearly renamed -> rename in pom.xml (BOM comparison)   │   │
 │ │               └─ advice given, or no clear rename ──┐                                   │   │
 │ │   dependency ── extra = candidate artifacts ────────┤                                   │   │
 │ │   code ─────────────────────────────────────────────┤                                   │   │
 │ │                                                     ▼                                   │   │
 │ │     ┌──────────────────────────────────────────────────────────────────────────────┐    │   │
 │ │     │ _fix_structured   compile_fix.py   ONE structured-output call                │    │   │
 │ │     │  prompt compile_fix_system.md + compile_fix_task.md:                         │    │   │
 │ │     │    upgrades, code context (file whole if <= 250 lines, else imports, setup   │    │   │
 │ │     │    code and ±25 lines around each error; "same error at N places: fix all"), │    │   │
 │ │     │    POM excerpts for pom issues, candidates, known fixes from the hints file, │    │   │
 │ │     │    consultant advice, earlier tries of this issue                            │    │   │
 │ │     │  answer CodeFix {status: fix | unsure, add_dependencies[], edits[], reason}  │    │   │
 │ │     │  add_dependencies -> code inserts into the pom.xml owning the failing file   │    │   │
 │ │     │                      (skips duplicates)                                      │    │   │
 │ │     │  edits -> apply_edits(): line edits (line/end_line, indentation kept) or     │    │   │
 │ │     │           old -> new text, tolerant: excerpt prefixes stripped, exact, CRLF, │    │   │
 │ │     │           whitespace-insensitive, whole-line delete; pom.xml only for        │    │   │
 │ │     │           pom/dependency issues; paths resolved inside the project           │    │   │
 │ │     │  code issue and nothing applied ──> API NOTES from the sources JARs, ask     │    │   │
 │ │     │     again: old declaration (javadoc, @deprecated), new class + supertypes'   │    │   │
 │ │     │     methods across artifacts, similar names                                  │    │   │
 │ │     └──────────────────────────────────────────────────────────────────────────────┘    │   │
 │ │                                                                                         │   │
 │ │   other ── _fix_with_agent: tool-calling agent (repair.py, create_agent, "full" mode,   │   │
 │ │            all tools, recursion limit 60): list/read/search/edit/write files,           │   │
 │ │            read_pom, find_versions, read_artifact_pom, find_replacement_artifact,       │   │
 │ │            inspect_class (javap), read_library_docs; repeat calls blocked;              │   │
 │ │            .env / settings.xml refused; edits recorded                                  │   │
 │ │                                                                                         │   │
 │ │   any exception ──> "fix failed with an error", round still counts                      │   │
 │ │                                                                                         │   │
 │ │ 7. PENDING FIX for learning: only if files were edited, the fix didn't fail/was not     │   │
 │ │      "unsure", and the issue is not a syntax error -> pending_hint (+ error places)     │   │
 │ │ 8. GUARD  _restore_reverted(): any upgraded version changed by the fix is put back      │   │
 │ │ 9. repair_history += {round, issue, try, summary, edits, restored, API notes}           │   │
 │ └─────────────────────────────────────────────────────────────────────────────────────────┘   │
 │     │                                                                            │            │
 └─────┘ rebuild                                                                    ▼            │
                                                         ┌─────────────────────────────────────┐ │
                                                         │ defer_to_user                       │ │
                                                         │  environment: "check credentials,   │ │
                                                         │    network/proxy or settings.xml"   │ │
                                                         │  otherwise: "still fails after N    │ │
                                                         │    rounds; X issues remain, Y tried │ │
                                                         │    max_retries times"               │ │
                                                         └─────────────────────────────────────┘ │
                                                                            │                    │
   ┌────────────────────────────────────────────────────────────────────────┴────────────────────┘
   ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ report  (markdown)                                                                                   │
 │   project, model, repair model, build command; outcome (passed / stopped / deferred + reason)        │
 │   Java version update, upgrades (major jumps marked), applied via / could not apply / could not check │
 │   repair rounds: issue, try, summary, edits, restored versions, API notes                            │
 │   remaining issues (tried Nx) + remaining build errors excerpt                                       │
 │   undo: git restore . + git clean -fd  (or "no Git: changes were made in place")                     │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
   │  __main__.py: print the report; write it to --report or upgrade-report-<timestamp>.md
   │               (never inside the project: falls back to the current folder, then the home folder)
   ▼
 ╔═════╗
 ║ END ║
 ╚═════╝
```

## Routing summary

| From | Condition | To |
|---|---|---|
| START | | preflight |
| preflight | ok | baseline_build |
| preflight | stopped | report |
| baseline_build | ok (or skipped after a Java update) | extract_dependencies |
| baseline_build | failed | report |
| extract_dependencies | POMs read | next_dependency |
| extract_dependencies | POMs unreadable | report |
| next_dependency | a dependency left | find_versions |
| next_dependency | none left, something applied or Java updated | build |
| next_dependency | none left, nothing changed | report |
| find_versions | newer version chosen | apply_version |
| find_versions | up to date / lookup failed | next_dependency |
| apply_version | | next_dependency |
| build | passed | report |
| build | failed | classify_failure |
| classify_failure | environment, or 50 rounds, or no issue with tries left | defer_to_user |
| classify_failure | an issue with tries left | repair_agent |
| repair_agent | | build |
| defer_to_user | | report |
| report | | END |

## Model calls per run

| Where | Model | Kind |
|---|---|---|
| preflight | [llm], [repair_llm] | one tool-call probe each |
| find_versions | [llm] | one structured call per distinct group:artifact |
| repair: consultant | CLI | one read-only call per round (if configured) |
| repair: pom / dependency / code | [repair_llm] or [llm] | one structured call, a second one with API notes if nothing applied (code) |
| repair: other | [repair_llm] or [llm] | tool-calling agent, up to ~30 tool calls |
