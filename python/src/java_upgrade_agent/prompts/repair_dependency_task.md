Repair round {attempt}: a package is missing from the classpath.

Error:
```
{errors}
```

The Java lines (`>>` marks the line):
{java_context}

Artifacts on Maven Central that provide the missing classes:
{candidates}

Upgrades applied (module: artifact old -> new):
{upgrades}

Advice from an expert consultant for this error ({advice_source}; not yet verified by a build - follow it unless it clearly doesn't fit the code):
{advice}

Known fixes for this error from earlier runs (each verified by a successful build; reuse them when they fit):
{known_fixes}

Earlier tries for this issue:
{history}

Add the dependency that provides this package to pom.xml (and adjust the import only if you switch to a different namespace).
