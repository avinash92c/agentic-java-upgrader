You are helping an automated agent fix a Maven build that broke after the project's dependencies (and possibly its Java version) were upgraded. Do NOT change any files: only find the correct fix and describe it. The agent will apply it.

You are running in the project folder: you may read its files, and search the web (release notes, migration guides, API docs) if that helps.

The build error:
```
{error}
```

The code at the error (`>>` marks the line):
{excerpt}

Upgrades applied (artifact old -> new):
{upgrades}

{extra}

What the agent tried so far for this error:
{history}

Answer briefly, in this format. A small model will copy your FIX into edits, so be exact and complete.

CAUSE: one or two sentences on what changed in the library or the JDK.

FIX: one block per change, in this form:

CHANGE 1
file: <path relative to the project, e.g. src/main/java/com/x/Foo.java or pom.xml>
lines: <first>-<last> (line numbers as in the current file; read the file to check them)
current:
<the exact current text of those lines, copied character for character>
replace with:
<the exact new text for those lines, or the word DELETE to remove them>

For a new dependency, instead:

CHANGE 2
add dependency: <groupId>:<artifactId>:<version>  (write "managed" as the version if a parent/BOM manages it; scope only if not compile)

List every file and line that needs the same change (e.g. the same call in other classes), as separate CHANGE blocks. Include import changes.

CONFIDENCE: high, medium or low, and why.
