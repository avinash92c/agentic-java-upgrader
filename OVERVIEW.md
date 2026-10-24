# Overview

## What the agent does

You point the agent at a Maven (Java) project. It:

1. **Checks** the project is safe to change (a clean Git tree, if it is a Git repo) and that the model works.
2. **Checks the Java version.** If the project's Java version is too old for your JDK, it asks whether to move it to 17, 21 or 25.
3. **Builds** the project once before changing anything, so problems that were already there are not blamed on the upgrade.
4. **Finds every versioned dependency** in the project's `pom.xml` files.
5. **Looks up the newest stable version** of each one on Maven Central and writes it into the POM.
6. **Builds again.** If the build passes, it is done.
7. **Repairs the build** if it fails: it lists the errors and fixes them one at a time, rebuilding after each fix, until the build passes or it runs out of tries.
8. **Hands over** to you what it could not fix, with the reason.
9. **Writes a report** of everything it changed and how to undo it.

Every change is made by the agent itself, so each one appears in the log and the report.

## Features

**Upgrades**
- Single and multi-module Maven projects
- Parent POMs, BOM imports, managed and direct dependencies, `${property}` versions
- Newest stable release only (no alpha, beta, RC, milestone or SNAPSHOT), including new major versions
- In-place POM edits that keep the file's formatting
- Java version update for old projects (to 17, 21 or 25)

**Repair**
- Errors sorted into POM, missing dependency, compile and other errors, and fixed in that order
- One issue per round; the same error in several places is fixed in one go
- Renamed artifacts fixed directly from the BOM comparison, without the LLM
- Missing packages: finds on Maven Central which artifact provides them
- Compile errors: the LLM is shown the code around the error and returns exact edits, which the agent applies
- API notes from the library sources (old and new methods, javadoc, deprecations) when the LLM is unsure
- A guard that stops a fix from quietly reverting an upgrade
- Problems outside the code (downloads, credentials, network) are handed to you straight away

**Models**
- Claude, ChatGPT, Gemini or a local model through Ollama
- Optional stronger model just for repairs
- Optional consultant: Claude Code, Gemini CLI or Codex CLI (your subscription login) advises on fixes; it never edits files

**Learning**
- Fixes confirmed by a passing build are saved and offered again when the same error comes up in a later run

**Logs and report**
- A step-by-step log, with full LLM requests and responses in verbose mode
- A markdown report: upgrades, every repair round, what is left, and how to undo

## Not yet

- Artifactory and other in-house repositories (Maven Central only)
- Gradle projects
- Dependency CVE checks
- Running or fixing the project's unit tests

## More detail

- [README.md](README.md): features and current status
- [INSTALL.md](INSTALL.md): installation and setup
- [FLOW.md](FLOW.md): the full flow, step by step
- [TOOLS.md](TOOLS.md): every tool and function the agent uses
