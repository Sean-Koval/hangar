"""semver-mini's hidden tests: five cases of the semver smoke task (flightdeck tasks/smoke/semver has all 116).
Expected values are node-semver 7.8.5's. The grader copies the agent's semver.py beside this file."""
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
import semver as S  # noqa: E402


class Hidden(unittest.TestCase):
    def test_parse_fields(self):
        v = S.Version.parse("v1.2.3-alpha.1+build.7")
        self.assertEqual((v.major, v.minor, v.patch), (1, 2, 3))
        self.assertEqual(str(v), "1.2.3-alpha.1+build.7")

    def test_parse_invalid(self):
        with self.assertRaises(ValueError):
            S.Version.parse("01.2.3")

    def test_ordering(self):
        vs = ["1.0.0", "1.0.0-rc.1", "1.0.0-alpha", "0.9.9", "1.10.0", "1.2.0"]
        self.assertEqual([str(x) for x in sorted(S.Version.parse(v) for v in vs)],
                         ["0.9.9", "1.0.0-alpha", "1.0.0-rc.1", "1.0.0", "1.2.0", "1.10.0"])

    def test_range_caret(self):
        self.assertTrue(S.satisfies("1.9.9", "^1.2.3"))
        self.assertFalse(S.satisfies("2.0.0", "^1.2.3"))

    def test_range_prerelease(self):
        self.assertFalse(S.satisfies("1.0.0-alpha", ">=0.9.0"))
        self.assertTrue(S.satisfies("1.2.4-beta.2", "^1.2.4-beta.1"))


if __name__ == "__main__":
    unittest.main()
