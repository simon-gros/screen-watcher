# Screen Watcher versioning policy

Screen Watcher uses a milestone-oriented form of **Semantic Versioning
(SemVer)**:

```text
MAJOR.MINOR.PATCH
```

The canonical current version is stored in the repository-root `VERSION`
file. All other version displays, packages, release metadata, tags, and
documentation must derive from or agree with that value.

Current version:

```text
0.0.1
```

## Why this scheme

This follows conventions already common in programming and game-development
tooling:

- Semantic Versioning defines `MAJOR.MINOR.PATCH`, reserves major version
  zero for initial development, and defines `1.0.0` as the point where the
  public compatibility contract becomes stable.
- Unity's package versioning explicitly uses Semantic Versioning and keeps
  packages at major version `0` while they are still in initial development.
- Godot uses a `major.minor.patch` release policy, with minor releases for
  feature development and major releases for compatibility-breaking changes.
- Steam separates test/beta builds from the default release branch, reinforcing
  the distinction between a build's maturity/channel and its product version.

References:

- https://semver.org/
- https://docs.unity3d.com/Manual/upm-semver.html
- https://docs.godotengine.org/en/stable/about/release_policy.html
- https://partner.steamgames.com/doc/store/application/builds
- https://partner.steamgames.com/doc/store/application/branches

## Screen Watcher interpretation before 1.0

While Screen Watcher remains in initial development, the version has the form:

```text
0.MINOR.PATCH
```

The leading zero is intentional. It signals that interfaces, profile schemas,
storage formats, command-line behavior, and internal architecture may still
change substantially before the first stable release.

Screen Watcher applies a stricter project rule inside that development period:

### PATCH — corrective development release

Increment the PATCH number when producing a new declared release/snapshot that
does **not** represent a major coding milestone.

Examples:

```text
0.0.1 -> 0.0.2
0.3.0 -> 0.3.1
0.3.1 -> 0.3.2
```

Typical reasons:

- bug fixes;
- detector correctness fixes;
- false-positive/false-negative corrections;
- performance corrections;
- documentation/release corrections accompanying a build;
- packaging fixes;
- small backward-compatible refinements;
- practical-validation fixes that do not complete a new major milestone.

A commit does not automatically require a new PATCH number. Version numbers
identify deliberately declared software releases/snapshots, not commit counts.

### MINOR — completed major coding milestone

Increment the MINOR number when a substantial roadmap milestone has been
**implemented and practically validated**, then reset PATCH to zero.

Examples:

```text
0.0.7 -> 0.1.0
0.1.4 -> 0.2.0
0.8.3 -> 0.9.0
```

A milestone bump is appropriate for work such as:

- completion of a major reusable architecture stage;
- a materially new supported subsystem;
- first usable GUI milestone;
- first usable Windows platform milestone;
- a major new reader/event framework;
- a major persistence/schema generation;
- a major profile/coverage milestone;
- a release-quality packaging/distribution milestone.

The exact roadmap item does not dictate the number in advance. The next MINOR
number is sequential and is assigned when the milestone is actually complete.

**Documentation, research, planning, or partially implemented work never
qualifies by itself for a MINOR bump.**

A milestone must normally satisfy all of the following:

1. the implementation is merged to `main`;
2. repository CI is green;
3. the affected current functionality has passed the appropriate practical
   validation from `docs/practical-validation-plan.md`;
4. no unresolved critical/high defect invalidates the milestone claim;
5. regression tests or replay/manual reproduction records exist for significant
   failures encountered during implementation;
6. release/migration notes accurately describe what changed.

This prevents the version number from advancing faster than the actual
software.

### MAJOR zero remains until stability

Do not change `0.x.y` to `1.0.0` merely because many milestones have been
completed.

Version `1.0.0` means Screen Watcher has reached its first stable public
compatibility contract.

At minimum, `1.0.0` should require:

- a practically validated, release-quality application;
- a clearly documented set of supported operating systems/platforms;
- stable installation/update/migration behavior;
- documented profile/configuration schema expectations;
- documented CLI behavior and public extension/event interfaces where exposed;
- migration handling for persistent user data;
- release packaging suitable for normal users rather than source-tree-only use;
- no known critical/high defect that contradicts the stable-release claim.

The exact 1.0 feature set may evolve, but the stability requirement must not be
weakened simply to reach the number sooner.

## Rules after 1.0.0

Once Screen Watcher reaches `1.0.0`, normal Semantic Versioning rules apply
to its documented public compatibility surface.

### PATCH: x.y.Z

Increment PATCH for backward-compatible bug fixes and internal corrections.

Example:

```text
1.2.0 -> 1.2.1
```

### MINOR: x.Y.0

Increment MINOR for backward-compatible new functionality.

Example:

```text
1.2.4 -> 1.3.0
```

### MAJOR: X.0.0

Increment MAJOR for intentional incompatible changes to the documented public
compatibility surface.

Example:

```text
1.8.3 -> 2.0.0
```

For Screen Watcher, the public compatibility surface can include more than a
traditional library API. It may include:

- profile/configuration schemas;
- CLI commands and documented flags;
- normalized event/export schemas;
- extension/plugin APIs;
- local API protocols;
- persistent database/migration guarantees;
- supported package/update behavior.

A GUI redesign by itself does not automatically require a major-version bump
unless it also breaks a documented compatibility contract.

## Pre-release labels

Candidate builds may use standard SemVer pre-release identifiers:

```text
0.2.0-alpha.1
0.2.0-alpha.2
0.2.0-beta.1
0.2.0-rc.1
0.2.0
```

Meanings:

- `alpha` — milestone functionality is incomplete or still changing;
- `beta` — intended functionality is substantially present but requires
  broader practical validation;
- `rc` — release candidate; no known blocker is expected to require design
  changes;
- no suffix — declared normal release/snapshot for that version.

Pre-release labels describe maturity. They do **not** replace PATCH/MINOR/MAJOR
semantics.

Do not use labels such as `final-final2`, `new`, `latest`, or dates as the
primary software version.

## Build metadata and internal builds

SemVer build metadata may be appended when useful:

```text
0.2.0-beta.1+git.abcdef0
0.2.0+build.417
```

Build metadata identifies a particular build of the same version and does not
create a new compatibility/release version.

CI run numbers, Git commit hashes, Steam Build IDs, package revisions, or
platform-specific installer revisions should remain **build identifiers**, not
be substituted for the product version.

## Linux and Windows use the same product version

Screen Watcher has one product version across supported platforms.

Do not create independent version sequences such as:

```text
Linux 0.4.0
Windows 0.2.7
```

Instead, a release is Screen Watcher `0.4.0`, with platform packages/build
metadata indicating which artifacts exist.

If a milestone is Linux-only or Windows-only during development, the product
version still advances once according to this policy. Platform support status
belongs in release notes/capability metadata.

## Version-change procedure

A version change must be deliberate.

For every declared version bump:

1. decide whether the change is PATCH, MINOR, MAJOR, or pre-release;
2. confirm that the required validation gate has been met;
3. update the root `VERSION` file in the release/version-bump pull request;
4. update release notes/changelog metadata as applicable;
5. ensure `watcher.py --version` reports exactly the same value;
6. ensure package metadata/installers use exactly the same product version;
7. merge only with green CI;
8. create an immutable Git tag named `v<version>` for an actual release;
9. never move or rewrite an existing release tag to point at different code.

Once a version has been released, its contents are immutable. Corrections require
a new version.

## Current 0.0.1 status

`0.0.1` is the current initial-development version and remains valid until a
new release/snapshot is deliberately declared.

The repository has already accumulated substantial work, but roadmap documents,
commit count, or code volume do not retroactively force a version increase.

The next number depends on what is being declared:

- a corrective development snapshot without a completed major milestone:
  `0.0.2`;
- the first subsequently declared, completed and practically validated major
  coding milestone: `0.1.0`.

No future milestone number should be assigned merely because the milestone is
planned.
