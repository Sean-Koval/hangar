"""Regenerate hidden_test_semver.py from oracle.json (node oracle.js > oracle.json; needs `npm i semver@7`)."""

import json
import re
import sys

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "oracle.json"))


def cat(v: str, r: str) -> str:
    if "-" in v.split("+")[0] and not v.startswith("v"):
        return "range_prerelease"
    if " - " in r:
        return "range_hyphen"
    if "^" in r:
        return "range_caret"
    if "~" in r:
        return "range_tilde"
    toks = [t.lstrip("<>=") for t in r.replace("||", " ").split()]
    if "x" in r or "*" in r or r == "" or any(t and t.count(".") < 2 for t in toks):
        return "range_xrange"
    return "range_basic"


lines = ['"""Hidden grader for the semver harness eval. Expected values come from node-semver 7.8.5."""',
         "import os, subprocess, sys, unittest", "sys.path.insert(0, os.getcwd())", "import semver as S", "",
         "HERE = os.getcwd()", "", "class Hidden(unittest.TestCase):", "    pass", "",
         "def add(name, fn):", "    setattr(Hidden, name, fn)", ""]
n = 0


def t(category: str, body: list[str]) -> None:
    global n
    n += 1
    lines.append(f"def _t{n}(self):")
    lines.extend("    " + b for b in body)
    lines.append(f'add("test_{category}__{n:03d}", _t{n})')
    lines.append("")


for v, ok in d["valid"]:
    if ok:
        t("parse_valid", [f"S.Version.parse({v!r})"])
    else:
        t("parse_invalid", ["with self.assertRaises(ValueError):", f"    S.Version.parse({v!r})"])
t("parse_fields", ["v = S.Version.parse('v1.2.3-alpha.1+build.7')", "self.assertEqual((v.major, v.minor, v.patch), (1, 2, 3))",
                   "self.assertEqual(tuple(str(x) for x in v.prerelease), ('alpha', '1'))",
                   "self.assertEqual(tuple(v.build), ('build', '7'))", "self.assertEqual(str(v), '1.2.3-alpha.1+build.7')"])
srt, inp = d["sorted"], d["sortIn"]
t("ordering", [f"vs = [S.Version.parse(x) for x in {inp!r}]", f"self.assertEqual([str(x) for x in sorted(vs)], {srt!r})"])
for a, b in zip(srt, srt[1:]):
    t("ordering", [f"self.assertLess(S.Version.parse({a!r}), S.Version.parse({b!r}))",
                   f"self.assertGreater(S.Version.parse({b!r}), S.Version.parse({a!r}))"])
for a, b, e in d["eq"]:
    t("equality", [f"self.assertEqual(S.Version.parse({a!r}) == S.Version.parse({b!r}), {e!r})"])
for v, p, e in d["inc"]:
    t("bump", [f"self.assertEqual(str(S.Version.parse({v!r}).bump({p!r})), {e!r})"])
for v, r, e in d["satisfies"]:
    t(cat(v, r), [f"self.assertIs(bool(S.satisfies({v!r}, {r!r})), {e!r}, msg={(v + ' in ' + repr(r))!r})"])
cli = '[sys.executable, os.path.join(HERE, "semver.py")]'
t("cli", [f"out = subprocess.run({cli} + ['sort', '1.10.0', '1.2.0', '1.0.0-rc.1', '1.0.0'], capture_output=True, text=True, timeout=30)",
          "self.assertEqual(out.returncode, 0)", "self.assertEqual(out.stdout.split(), ['1.0.0-rc.1', '1.0.0', '1.2.0', '1.10.0'])"])
t("cli", [f"r = subprocess.run({cli} + ['satisfies', '1.2.9', '~1.2.3'], capture_output=True, text=True, timeout=30)",
          "self.assertEqual(r.returncode, 0)"])
t("cli", [f"r = subprocess.run({cli} + ['satisfies', '1.3.0', '~1.2.3'], capture_output=True, text=True, timeout=30)",
          "self.assertEqual(r.returncode, 1)"])
lines.append('if __name__ == "__main__":\n    unittest.main()')
open("hidden_test_semver.py", "w").write("\n".join(lines) + "\n")
print(n, "tests")
