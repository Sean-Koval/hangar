import copy
import json
import unittest
from pathlib import Path

from hangar.release_readiness import Observation, ReleaseProfile, evaluate, profile_digest

NOW = "2026-10-18T12:00:00Z"


def inputs():
    profile = {
        "version": 1,
        "release": "service/release-7",
        "environment": "production",
        "max_age_seconds": 3600,
        "stages": [
            {
                "id": "code",
                "depends_on": [],
                "requirements": [
                    {
                        "id": "code-merge",
                        "kind": "merge",
                        "repository": "example/code",
                        "revision": "a" * 40,
                        "bindings": {"target": "main", "pull_request": "42", "reviewed_head": "a" * 40},
                    }
                ],
            },
            {
                "id": "build",
                "depends_on": ["code"],
                "requirements": [
                    {
                        "id": "build",
                        "kind": "build",
                        "repository": "example/code",
                        "revision": "a" * 40,
                        "bindings": {"artifact": "sha256:" + "b" * 64, "build_id": "build-42"},
                    }
                ],
            },
        ],
    }
    observations = [
        dict(
            r,
            release=profile["release"],
            profile_digest=profile_digest(profile),
            status="observed",
            observed_at=NOW,
            source="https://ci.example/evidence/42",
        )
        for s in profile["stages"]
        for r in s["requirements"]
    ]
    return profile, observations


class DeliveryPlanTests(unittest.TestCase):
    def test_committed_schemas_match_models(self):
        root = Path(__file__).parents[1]
        for name, model in [("release-profile", ReleaseProfile), ("release-observation", Observation)]:
            schema = json.loads((root / f"docs/api/{name}.schema.json").read_text())
            self.assertEqual(schema, model.model_json_schema())

    def test_infrastructure_approval_cannot_authorize_other_environment(self):
        p = json.loads((Path(__file__).parents[1] / "examples/release-profile.json").read_text())
        p["stages"][2]["requirements"][1]["bindings"]["environment"] = "staging"
        with self.assertRaises(ValueError):
            profile_digest(p)

    def test_complete_is_advisory_not_authorization(self):
        p, o = inputs()
        result = evaluate(p, o, as_of=NOW)
        self.assertEqual(result["status"], "satisfied_by_observation")
        self.assertFalse(result["authorization_granted"])
        self.assertFalse(result["external_verification"])
        self.assertEqual(result["release_status"], "unknown")
        self.assertEqual(result["rollout_status"], "unknown")

    def test_missing_blocks_dependants(self):
        p, o = inputs()
        result = evaluate(p, o[1:], as_of=NOW)
        self.assertEqual([s["status"] for s in result["stages"]], ["missing", "blocked"])

    def test_unknown_and_negative(self):
        for state, expected in [("unknown", "unknown"), ("not_satisfied", "blocked")]:
            p, o = inputs()
            o[0]["status"] = state
            self.assertEqual(evaluate(p, o, as_of=NOW)["stages"][0]["status"], expected)

    def test_bound_identity_mismatches_block(self):
        for key, value in [
            ("release", "other/release"),
            ("repository", "example/other"),
            ("revision", "c" * 40),
            ("bindings", {"target": "production", "pull_request": "42", "reviewed_head": "a" * 40}),
        ]:
            p, o = inputs()
            o[0][key] = value
            self.assertEqual(evaluate(p, o, as_of=NOW)["stages"][0]["status"], "blocked", key)

    def test_stale_and_future_are_unknown(self):
        for date in ["2026-10-18T10:00:00Z", "2026-10-18T12:01:00Z"]:
            p, o = inputs()
            o[0]["observed_at"] = date
            self.assertEqual(evaluate(p, o, as_of=NOW)["stages"][0]["status"], "unknown")

    def test_conflicting_observations_block(self):
        p, o = inputs()
        other = dict(o[0], status="not_satisfied")
        self.assertEqual(evaluate(p, o + [other], as_of=NOW)["stages"][0]["status"], "blocked")

    def test_repeat_is_deterministic_and_does_not_mutate_inputs(self):
        p, o = inputs()
        before = copy.deepcopy((p, o))
        self.assertEqual(evaluate(p, o, as_of=NOW), evaluate(p, list(reversed(o)), as_of=NOW))
        self.assertEqual((p, o), before)

    def test_validation_fails_closed(self):
        mutations = [
            lambda p: p.update(version=2),
            lambda p: p.update(version=True),
            lambda p: p.update(version=1.0),
            lambda p: p.update(max_age_seconds=0),
            lambda p: p["stages"][0].update(depends_on=["build"]),
            lambda p: p["stages"].append(copy.deepcopy(p["stages"][0])),
            lambda p: p["stages"][0]["requirements"][0].update(revision="main"),
            lambda p: p["stages"][0]["requirements"][0].update(bindings={}),
            lambda p: p["stages"][0]["requirements"][0].update(kind="execute"),
        ]
        for mutate in mutations:
            p, o = inputs()
            mutate(p)
            with self.assertRaises(ValueError):
                evaluate(p, o, as_of=NOW)

    def test_claimed_verification_is_rejected(self):
        p, o = inputs()
        o[0]["verified"] = True
        with self.assertRaises(ValueError):
            evaluate(p, o, as_of=NOW)

    def test_changed_profile_requires_new_observations(self):
        p, o = inputs()
        p["max_age_seconds"] = 300
        self.assertEqual(evaluate(p, o, as_of=NOW)["stages"][0]["status"], "blocked")

    def test_bad_timestamp_unknown_requirement_and_extra_fields(self):
        for key, value in [
            ("observed_at", "yesterday"),
            ("observed_at", "2026-10-18T12:00:00"),
            ("id", "unknown-requirement"),
            ("source", ""),
            ("status", "approved"),
        ]:
            p, o = inputs()
            o[0][key] = value
            with self.assertRaises(ValueError):
                evaluate(p, o, as_of=NOW)

    def test_wrong_build_commit_and_missing_artifact_are_invalid_profiles(self):
        for change in [
            lambda r: r.update(revision="c" * 40),
            lambda r: r.update(bindings={"build_id": "42"}),
            lambda r: r["bindings"].update(artifact="latest"),
        ]:
            p, _ = inputs()
            change(p["stages"][1]["requirements"][0])
            with self.assertRaises(ValueError):
                profile_digest(p)

    def test_full_example_and_cross_stage_rejection(self):
        p = json.loads((Path(__file__).parents[1] / "examples/release-profile.json").read_text())

        def observe(profile):
            return [
                dict(
                    r,
                    release=profile["release"],
                    profile_digest=profile_digest(profile),
                    status="observed",
                    observed_at=NOW,
                    source="https://evidence.example/1",
                )
                for s in profile["stages"]
                for r in s["requirements"]
            ]

        result = evaluate(p, observe(p), as_of=NOW)
        self.assertEqual(result["release_status"], "satisfied_by_observation")
        self.assertEqual(result["omitted_stages"], [])
        self.assertFalse(result["authorization_granted"])
        mutations = [
            lambda q: q["stages"][5]["requirements"].pop(),
            lambda q: q["stages"][5]["requirements"][1].update(revision="f" * 40),
            lambda q: q["stages"][4]["requirements"][0]["bindings"].update(artifact="sha256:" + "1" * 64),
            lambda q: q["stages"][5]["requirements"][0]["bindings"].update(environment="staging"),
            lambda q: q["stages"][3]["requirements"][0]["bindings"].update(decision="required"),
            lambda q: q["stages"][6]["requirements"][0].update(revision="a" * 40),
        ]
        for change in mutations:
            bad = copy.deepcopy(p)
            change(bad)
            with self.assertRaises(ValueError):
                profile_digest(bad)


if __name__ == "__main__":
    unittest.main()
