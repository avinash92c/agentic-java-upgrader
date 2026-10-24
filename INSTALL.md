# Installation and setup

How to install the agent, set up a model, and run it on a Maven project. Commands are for Windows PowerShell; macOS/Linux equivalents are noted where they differ.

For what the agent does, see the [main README](README.md); for its tools, see [TOOLS.md](TOOLS.md).

## 1. Requirements

| Needed | Version / notes | Check with |
|---|---|---|
| Python | 3.11 to 3.14 | `python --version` |
| [Poetry](https://python-poetry.org/docs/#installation) | 2.x | `poetry --version` |
| Maven | 3.9+ (the agent runs whatever `mvn` is on the PATH) | `mvn -v` |
| JDK | the one Maven uses (`JAVA_HOME`); the agent offers to update projects whose Java version this JDK can no longer compile | `mvn -v` shows it |
| A model | a local [Ollama](https://ollama.com/) server, **or** an API key for Anthropic, OpenAI or Google | see step 3 |
| Git | optional; with Git you can undo a run with `git restore .` | `git --version` |
| An AI command-line tool | optional consultant: Claude Code, Gemini CLI or Codex CLI, logged in | see step 5 |

## 2. Install the agent

```powershell
git clone <this repository> agentic-java-upgrader
cd agentic-java-upgrader\python
poetry install
```

`poetry install` creates a virtual environment in `python\.venv` and installs the agent with its dependencies (LangChain, LangGraph and the provider packages).

Check:

```powershell
poetry run python -m java_upgrade_agent --help
```

## 3. Set up a model

### Option A: local model with Ollama (no API key)

1. Install [Ollama](https://ollama.com/download) and start it (it serves on `http://localhost:11434`).
2. Pull a model that supports **tool calling**. `qwen3:8b` is the current test model; very small models (around 4B) often fail at tool calls.
   ```powershell
   ollama pull qwen3:8b
   ```
3. In the config (step 4) keep `reasoning = false`: qwen3 is a "thinking" model, and with thinking on it can spend minutes reasoning and return empty answers.

### Option B: a cloud model

Get an API key from Anthropic, OpenAI or Google, and put it in an environment variable rather than in the config file:

```powershell
setx ANTHROPIC_API_KEY "<your key>"     # or OPENAI_API_KEY / GOOGLE_API_KEY
```

`setx` applies to new terminals; open a new PowerShell window afterwards. (macOS/Linux: `export ANTHROPIC_API_KEY=...` in your shell profile.)

## 4. Create the config file

```powershell
Copy-Item config.example.toml config.toml
```

Then edit `config.toml`. A local-model setup:

```toml
[repository]
type = "central"                                 # Maven Central

[llm]                                            # version selection and applying fixes
provider = "ollama"
model = "qwen3:8b"
base_url = "http://localhost:11434"
temperature = 0
timeout = 180                                    # seconds per call
max_tokens = 4096
extra = { reasoning = false, num_ctx = 16384 }   # thinking off; enough context for repair

[agent]
max_retries = 3                                  # tries per build issue
hints_file = "~/.java-upgrade-agent/migration_hints.yaml"   # fixes learned across runs
```

For a cloud model instead, set `provider` (`anthropic`, `openai` or `google`) and `model`, and leave out `base_url` and `extra`.

Optional sections (examples are in `config.example.toml`):
- `[repair_llm]`: a different (stronger) model only for applying repair fixes.
- `[consultant]`: an AI command-line tool that finds fixes (step 5).

Keep `config.toml` out of Git if it ever contains a key: add `python/config.toml` to `.gitignore`.

## 5. Optional: an AI command-line tool as consultant

If you have Claude Code, Gemini CLI or Codex CLI with a subscription, the agent can ask it how to fix each build issue. It runs read-only in the project folder (reads files, may search the web) and only gives advice; your model (`[repair_llm]`, or `[llm]`) applies the fix. The CLI never edits files itself, so every change is made by the agent and shows up in the log and the report.

1. Check it's on the PATH and logged in:
   ```powershell
   where.exe claude          # macOS/Linux: which claude
   claude                    # once interactively, to log in; then exit
   ```
2. Add to `config.toml`:
   ```toml
   [consultant]
   preset = "claude"         # claude | gemini | codex
   name = "Claude Code"
   when = "always"           # or "after_first_try"
   timeout = 300
   ```
   If the command isn't found, give the full path with `command = [...]` instead of `preset` (see `config.example.toml`). Write Windows paths with forward slashes or in single quotes (`'C:\Users\...'`): in double quotes TOML reads the backslashes as escapes and the config won't load.

Each question uses your subscription's quota and takes roughly 15 seconds to a few minutes.

## 6. Prepare the project to upgrade

- **Use a copy or a Git branch you can throw away.** The agent edits `pom.xml` and Java files in place.
- If it's a Git repo, the working tree must be **clean** (commit or stash first); without Git the agent warns and continues.
- The project must have a standard Maven layout: `pom.xml` at the root, and each module listed under `<modules>` with its own `pom.xml`.

## 7. Run

From the `python` folder:

```powershell
poetry run python -m java_upgrade_agent C:\path\to\maven-project --config config.toml -v --log-file run.log --report report.md
```

| Option | Meaning |
|---|---|
| `--config` | the config file (required) |
| `-v` | detailed log: every LLM request and response, tool calls, Maven Central URLs |
| `--log-file` | also write the log to a file |
| `--report` | the report file (default `upgrade-report-<timestamp>.md` in the current folder; never inside the project) |

At the start the log shows the model, the consultant (if any) and the hints file. If the project's Java version is too old for your JDK, you are asked whether to update it (answer `17`, `21`, `25`, or `n` to stop).

If the build fails after the upgrades, the log shows each repair round: which issue it works on (category, file and line), the consultant's advice if any, and the changes applied. One issue is fixed per round, then the project is rebuilt.

At the end the report is printed and saved. It lists the upgrades, every repair round, anything left to fix, and how to undo the changes.

## 8. Afterwards

- **Review the changes** (`git diff` in the project).
- **Run the tests** yourself: the agent builds with `-DskipTests`.
- **Start the application**: a passing build doesn't catch runtime problems (e.g. configuration files naming classes that moved).
- To undo, in a Git repo: `git restore .` and `git clean -fd`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named java_upgrade_agent` | Run from the `python` folder with `poetry run ...`, after `poetry install`. |
| `No module named httpx` / `truststore` / `yaml` | They normally come with LangChain; install them into the environment: `poetry run pip install httpx truststore pyyaml`. |
| `CERTIFICATE_VERIFY_FAILED` or Maven `PKIX path building failed` | An antivirus or proxy inspects HTTPS. The agent's own lookups use the Windows certificate store; for Maven set `$env:MAVEN_OPTS = "-Djavax.net.ssl.trustStoreType=Windows-ROOT"` (or `setx` it). Poetry may need the same fix to reach pypi.org. |
| Preflight: "the model did not make a tool call" | Use a model that supports tool calling (e.g. `qwen3:8b`), not a code-completion model like `codellama`. |
| Empty answers / very slow model calls with Ollama | Set `extra = { reasoning = false, num_ctx = 16384 }`, `timeout` and `max_tokens` in `[llm]`. |
| `consultant: claude not found` | Use the full path: `command = ["C:/Users/<you>/.local/bin/claude.exe", "-p", ...]`. |
| Config error on a Windows path | In TOML use forward slashes or single quotes for paths; `"C:\Users\..."` in double quotes is an invalid escape. |
| The same issue keeps coming back | Each issue gets `max_retries` tries, then it is listed under remaining issues in the report. A stronger `[repair_llm]` or the `[consultant]` usually helps with recent library changes. |
| The agent keeps reusing a wrong fix | Remove that entry from `~/.java-upgrade-agent/migration_hints.yaml` (or delete the file to start fresh). |
| `Stopped: baseline build failed` | The project doesn't build before any change. Fix that first (or let the agent update the Java version if it offers to). |
