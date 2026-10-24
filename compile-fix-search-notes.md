# Compile-fix search: how the agent looks in sources and javadoc for a fix

Notes for review, 2026-10-23. Describes the code as it is now (`python/src/java_upgrade_agent/compile_fix.py`, `nodes.py`). Running example: the Spring AI 1.1.5 -> 2.0.2 error on `GoogleGenAiChatOptions.Builder.toolName(String)`.

## 1. Where the sources come from

- After every build (baseline, post-upgrade, every repair rebuild) a separate `mvn -B dependency:sources` downloads each dependency's `-sources.jar` into `~/.m2/repository`.
- Code records which versions Maven resolved (from its "files have been resolved" list):
  - baseline build -> `baseline_sources`: the **old** versions, e.g. `org.springframework.ai:spring-ai-google-genai -> 1.1.5`
  - later builds -> `current_sources`: the **new** versions (`-> 2.0.2`). A build that fails on the POM keeps the last known map.
- Only sources jars are used. Javadoc jars (HTML) are not downloaded or read; javadoc comes from the `/** ... */` comments in the sources.

## 2. When the search runs

Compile errors put the repair step in "code" mode, which runs a compile-fix attempt:

- **Step 1, no search.** The LLM gets the errors and the failing Java lines and answers with edits (`{file, old, new}`) or `unsure`.
- **Step 2, the search.** Runs if the LLM said `unsure`, none of its edits could be applied, or the same errors came back after a step-1 fix.

## 3. What the search does, per missing symbol

Input from Maven:

```
symbol:   method toolName(java.lang.String)
location: class org.springframework.ai.google.genai.GoogleGenAiChatOptions.Builder
```

1. **Name and type:** `toolName`, and the full class name `org.springframework.ai.google.genai.GoogleGenAiChatOptions.Builder`.
2. **Which jar:** a nested class lives in its outer class's file, `org/springframework/ai/google/genai/GoogleGenAiChatOptions.java`. Code checks the dependencies' sources jars (same group prefix first) for that file.
3. **The declaration:** in that file, find `class GoogleGenAiChatOptions`, then `class Builder` inside its body (by matching braces), and read Builder's `extends` / `implements` clause.
4. **Supertypes:** each supertype name is resolved to a full class name (the file's imports, same package, nested types, wildcard imports). Its sources jar is found, possibly in another artifact (e.g. `spring-ai-model`), and the walk repeats, up to 4 levels. The outer class is walked too; `java.*` / `javax.*` are skipped. Result: the hierarchy, e.g. `GoogleGenAiChatOptions.Builder -> ToolCallingChatOptions.Builder -> ChatOptions.Builder`.
5. **Old version** (same walk over `baseline_sources`): find the type that declared `toolName(...)`, take the `/** ... */` comment directly above it, and pull out any `@deprecated` text.
6. **New version** (walk over `current_sources`): list the public methods each type declares directly (interface methods count without `public`; private ones and constructors are excluded), then:
   - say whether `toolName` still exists anywhere in the hierarchy
   - list methods sharing a word with the name (`tool`, `name`), e.g. `toolNames(...)`, `toolCallbacks(...)`, `toolContext(...)`, up to 20
   - list the 8 methods with the most similar names, labelled "may be unrelated"

## 4. What the LLM gets in step 2

```
### method toolName(java.lang.String) on org.springframework.ai.google.genai.GoogleGenAiChatOptions.Builder
In the old version, toolName was declared in ...GoogleGenAiChatOptions.Builder (spring-ai-google-genai 1.1.5): DEPRECATED: ... | <javadoc>
New type hierarchy: ...GoogleGenAiChatOptions.Builder (spring-ai-google-genai 2.0.2) -> ...ToolCallingChatOptions.Builder (spring-ai-model 2.0.2) -> ...
In the new version toolName no longer exists in ... or its supertypes.
Methods sharing a word with toolName (any type in the hierarchy): toolCallbacks(List<ToolCallback> toolCallbacks) [declared in ...ToolCallingChatOptions.Builder]; ...
Methods with similar names (may be unrelated; a similar name does not mean the same purpose): ...
```

These notes, the Java excerpts and the earlier attempts go into one structured-output LLM call. The LLM returns `{file, old, new}` edits; code applies an edit only if its `old` text appears exactly once in the file (pom.xml and files outside the project are refused).

## 5. What the search does not cover

| Gap | Effect |
|---|---|
| **No javadoc for the new methods.** Only the old declaration's comment is shown; new methods are listed as signatures. | The model can't read what `toolCallbacks` does, only its name and parameters. This is the gap behind the `allowedFunctionNames` mistake. |
| **Only the hierarchy of the error's class.** | A replacement in another class (a factory, a utility class, a new helper such as a `ToolCallback` implementation) is never found. |
| **Only methods.** | Missing fields, constants, enum values or classes (`symbol: class ...`) get only the hierarchy, no replacement. |
| **No release notes or migration guides.** These are used only for POM renames. | Notes such as "toolNames was replaced by toolCallbacks" in release notes aren't used. |
| **Old javadoc depends on the same artifact name.** | If the artifact was renamed, the old sources aren't found, so there's no `@deprecated` note. |
| **Regex parsing, not a Java parser.** | Braces in strings, unusual generics or annotations can make the walk miss a type or method. |
| **No usage examples.** | Code doesn't look at how the library's own code or tests call the new API, which is often the clearest example. |

## 6. Possible improvements (not decided)

1. **Javadoc of the candidate methods.** For the methods in the "sharing a word" list (and maybe the similar-name list), include the first sentences of their javadoc, so the model can choose by purpose rather than by name.
2. **Follow deprecation links.** When the old declaration is `@deprecated`, follow its `{@link ...}` / `@see` target and include that member's javadoc and signature in the new version.
3. **Usage examples.** Search the new version's sources jars for calls to the candidate methods and include one or two short examples.
4. **Release notes for code changes.** Reuse the GitHub release-notes lookup (from the POM migration step) and include lines that mention the missing symbol.
5. **Other symbol kinds.** Handle missing fields, constants and classes, not only methods.

## 7. Related observations from the last run (not yet addressed)

- **Edits not applied ("appears 0 times").** The `old` text probably differs from the file in whitespace or line breaks. Possible fix: retry the match ignoring whitespace differences; if it still fails, show the model the file's actual lines.
- **Identical notes and answers across attempts.** The notes are built by code, so they repeat; the next prompt could include the previous failed edit and why it failed.
- **unirest-java-core stayed at 4.8.1** although 4.10.2 is listed: the LLM version selection, probably comparing `4.10.2` and `4.9.0` as text. Deterministic selection (stable filter plus numeric comparison) would fix it.
