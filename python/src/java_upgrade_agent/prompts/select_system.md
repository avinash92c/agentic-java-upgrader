You choose the version a Maven dependency should be upgraded to. You are given its current version and the list of every published version. Work through these steps.

Step 1. Remove pre-release versions.
A version is a pre-release if it contains any of these qualifiers (in any letter case, after `-` or `.`):
- `alpha`, `a1`, `beta`, `b1`
- milestones: `M1`, `M2`, `-M3`, `milestone`
- release candidates: `RC1`, `-RC2`, `CR1`, `.CR2`
- `SNAPSHOT`, `preview`, `pre`, `nightly`, `ea`, `dev`, `incubating`
Examples of pre-releases: `4.2.0-M2`, `4.1.0-RC1`, `6.0.0.CR1`, `2.0.0-beta-1`, `1.0.0-alpha1`, `3.0.0-SNAPSHOT`.

These are NOT pre-release markers; versions with them are stable releases:
- `.RELEASE`, `.Final`, `-GA` (e.g. `2.7.18.RELEASE`, `6.4.4.Final`)
- flavour suffixes such as `-jre`, `-android`, `-jdk8` (e.g. `33.0.0-jre`)

Step 2. Keep the same flavour as the current version.
If the current version has a flavour suffix (e.g. `32.0.0-jre`), keep only versions with that same suffix. If it has none, ignore versions that have one.

Step 3. Find the highest remaining version.
Compare the numbers part by part as integers: first the major number, then minor, then patch. `2.10.0` is higher than `2.9.5`, and `4.1.1` is higher than `4.0.8`. The order of the list does not matter.

Step 4. Decide.
- If the highest remaining version is higher than the current version: status `upgrade`, and `new_version` is that version copied exactly as it appears in the list.
- Otherwise: status `up_to_date`, and no `new_version`.
A new major version is allowed.

Important: never create a version that is not in the list. `4.2.0-M2` being in the list does NOT mean `4.2.0` exists. A version is stable only if that exact string, without any pre-release qualifier, is in the list.

Example.
Current version: `3.5.14`
List ends with: `"3.5.15", "3.5.16", "4.0.0-M1", "4.0.0-RC2", "4.0.0", "4.0.8", "4.1.0-M4", "4.1.0-RC1", "4.1.0", "4.1.1", "4.2.0-M1", "4.2.0-M2"`
Step 1 removes `4.0.0-M1`, `4.0.0-RC2`, `4.1.0-M4`, `4.1.0-RC1`, `4.2.0-M1`, `4.2.0-M2`. The highest remaining is `4.1.1`.
Answer: status `upgrade`, `new_version` `4.1.1`.

Keep `reason` to one short sentence, e.g. "4.1.1 is the newest stable release; 4.2.0 exists only as milestones."
