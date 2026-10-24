Repair attempt {attempt}.

Upgrades applied (module: artifact old -> new):
{upgrades}

Errors from the latest build:
```
{errors}
```

pom.xml lines the errors point to (`>>` marks the line):
{pom_context}

Java lines the compile errors point to (`>>` marks the line; the missing symbol and its class follow the file name):
{java_context}

What replaced the missing dependencies (from comparing the old and new parent/BOM, or from release notes):
{hints}

Advice from an expert consultant for this error ({advice_source}; not yet verified by a build - follow it unless it clearly doesn't fit the code):
{advice}

Known fixes for this error from earlier runs (each verified by a successful build; reuse them when they fit):
{known_fixes}

Earlier attempts:
{history}

Fix the errors above.
