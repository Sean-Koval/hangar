You are in an empty git repository. Build a small Python library. Use Python 3 and the standard library only; do not install packages.

Create `semver.py` implementing Semantic Versioning 2.0.0 (semver.org), with range matching that follows npm's node-semver (`semver.satisfies`, default options).

Required API:

- `Version.parse(s: str) -> Version`. Accept an optional leading `v`. Fields: `major`, `minor`, `patch` (ints), `prerelease` (tuple of identifiers; numeric ones as int, others as str), `build` (tuple of str). Raise `ValueError` for anything that is not valid semver 2.0.0: missing parts, leading zeros in numeric identifiers, empty identifiers, invalid characters, extra parts.
- `str(version)` returns the canonical form without the `v`, including prerelease and build.
- Comparison operators (`<`, `<=`, `>`, `>=`, `==`, `!=`) use semver precedence, so `sorted()` works. Build metadata is ignored, including by `==`.
- `Version.bump(part) -> Version`, where `part` is `major`, `minor`, `patch` or `prerelease`. It follows node-semver's `semver.inc`. For example: `1.2.3` → major `2.0.0`; `1.2.3-alpha` → patch `1.2.3`; `1.2.3` → prerelease `1.2.4-0`; `1.2.3-alpha.1` → prerelease `1.2.3-alpha.2`.
- `satisfies(version, range_str) -> bool`. `version` may be a `str` or a `Version`. It supports:
  - exact versions and `=`;
  - the `>`, `>=`, `<`, `<=` operators;
  - space-separated AND and `||` OR;
  - `^` and `~`;
  - x-ranges (`1.x`, `1.2.*`, `*`, the empty string, partial versions such as `1.2` or `<2`);
  - hyphen ranges (`1.2.3 - 2.3.4`).

  Follow node-semver's rule for prereleases: a prerelease version satisfies a range only if some comparator in the same AND-set has the same `[major, minor, patch]` and also has a prerelease.
- A CLI:
  - `python3 semver.py sort V1 V2 ...` prints the versions in ascending order, one per line.
  - `python3 semver.py satisfies VERSION RANGE` exits 0 if VERSION satisfies RANGE, otherwise 1.

Write your own tests in `test_semver.py` (unittest) and make them pass with `python3 -m unittest`. When you are done, commit everything with git and reply with a short summary of what you built and the test result.
