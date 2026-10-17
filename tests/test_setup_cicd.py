"""deploy/gke/setup-cicd.sh against fake gcloud, gh, kubectl, terraform and curl that keep state between runs.

The fakes log every call and keep what was "created", so the tests prove what --dry-run leaves alone, that a second
run changes nothing, that every secret travels on stdin (never an argument, never the output), and that the names it
sets are the ones deploy.yml and images.yml read."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "gke" / "setup-cicd.sh"
WORKFLOWS = REPO / ".github" / "workflows"
pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the fake gh applies --jq with jq")

ADMIN = "hgr_ADMIN_7a1c0ffee0ddba11"  # the operator's admin token
MINTED = "hgr_MINTED_5ecre7deadbeef"  # what the fake hangar mints
VAULT_KEY = "VAULTKEY_q9QmFzZTY0IGtleQ"  # the hangar Secret's HANGAR_SECRET_KEY
ACCEPT = "ghp_ACCEPTTOKEN0123456789"  # an acceptance fixture token
SECRETS = (ADMIN, MINTED, VAULT_KEY, ACCEPT)
PROJECT, NUMBER, GH_REPO = "sk-ml-inference", "123456789012", "Sean-Koval/hangar"
REPO_ID, OWNER_ID = 1414601836, 48071870

FAKE = textwrap.dedent('''\
    #!{python}
    import base64, json, os, subprocess, sys
    from pathlib import Path
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    cfg = json.loads(os.environ["FAKE_CFG"])
    state_path = Path(os.environ["FAKE_STATE"])
    st = json.loads(state_path.read_text())
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps([tool, *args]) + "\\n")
    VALUE = {{"--project", "--location", "--format", "--filter", "--flatten", "--repo", "--env", "--json", "--jq",
              "-X", "--input", "-o", "-n", "--body", "--data", "-H"}}
    if tool == "kubectl":
        VALUE |= {{"-f"}}
    words, flags, fields = [], {{}}, {{}}  # fields: gh api's -f key=value
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-f" and tool == "gh":
            k, v = args[i + 1].split("=", 1)
            fields[k] = v
            i += 1
        elif a.startswith("-") and a != "-":
            if "=" in a and a.startswith("--"):
                k, v = a.split("=", 1)
            elif a in VALUE and i + 1 < len(args):
                k, v = a, args[i + 1]
                i += 1
            else:
                k, v = a, True
            flags[k] = v
        else:
            words.append(a)
        i += 1

    def save():
        state_path.write_text(json.dumps(st))

    def out(obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        if flags.get("--jq"):
            text = subprocess.run(["jq", "-r", "-c", flags["--jq"]], input=text, capture_output=True, text=True,
                                  check=True).stdout
        sys.stdout.write(text if text.endswith("\\n") or not text else text + "\\n")
        sys.exit(0)

    def fail(msg="not found", code=1):
        sys.stderr.write(msg + "\\n")
        sys.exit(code)

    def unexpected():
        with open(os.environ["FAKE_UNEXPECTED"], "a") as f:
            f.write(json.dumps([tool, *args]) + "\\n")
        fail("fake: unexpected call", 3)

    w = tuple(words)
    if tool == "terraform":
        v = cfg["outputs"].get(w[-1])
        if v is None:
            fail("no output")
        sys.stdout.write(v)
    elif tool == "gcloud":
        if w[:2] == ("config", "get-value"):
            out({{"account": "owner@example.com", "project": cfg.get("gcloud_project", "")}}.get(w[2], ""))
        if w[:2] == ("projects", "describe"):
            out(cfg["number"])
        if w[:3] == ("container", "clusters", "get-credentials") or w[:2] == ("services", "enable"):
            sys.exit(0)
        if w[:3] == ("container", "clusters", "list"):
            out("hangar\\tus-central1-a")
        if w[:2] in (("compute", "regions"), ("compute", "project-info")):
            out("N2D_CPUS  0.0  24.0")
        if w[:4] == ("iam", "workload-identity-pools", "providers", "list"):
            out("\\n".join(([st["provider"]["name"]] if st.get("provider") else []) + st.get("other_providers", [])))
        if w[:3] == ("iam", "workload-identity-pools", "providers"):
            name, key = w[4], "provider"
            if w[3] == "describe":
                st.get(key) or fail()
                out(st[key])
            if w[3] in ("create-oidc", "update-oidc"):
                st[key] = {{"name": name, "state": "ACTIVE", "attributeCondition": flags["--attribute-condition"],
                           "attributeMapping": dict(x.split("=", 1) for x in flags["--attribute-mapping"].split(",")),
                           "oidc": {{"issuerUri": flags["--issuer-uri"]}}}}
                save()
                sys.exit(0)
            unexpected()
        if w[:2] == ("iam", "workload-identity-pools"):
            if w[2] == "describe":
                st.get("pool") or fail()
                out(st["pool"])
            if w[2] == "create":
                st["pool"] = "ACTIVE"
                save()
                sys.exit(0)
            unexpected()
        if w[:2] == ("iam", "roles"):  # --- infra-state: the state account's custom role
            roles, rid = st.setdefault("roles", {{}}), w[3]
            if w[2] == "describe":
                roles.get(rid) or fail()
                out(roles[rid])
            if w[2] == "create":
                roles[rid] = {{"name": f"projects/{{cfg['project']}}/roles/{{rid}}", "stage": flags.get("--stage"),
                              "includedPermissions": flags["--permissions"].split(",")}}
            elif w[2] == "update" and rid in roles:
                roles[rid]["includedPermissions"] = flags["--permissions"].split(",")
            elif w[2] == "undelete" and rid in roles:
                roles[rid]["deleted"] = False
            else:
                unexpected()
            save()
            sys.exit(0)
        if w[:2] == ("iam", "service-accounts") and w[2] in ("describe", "create"):
            email = w[3] if "@" in w[3] else f"{{w[3]}}@{{cfg['project']}}.iam.gserviceaccount.com"
            if w[2] == "describe":
                email in st["sas"] or fail()
                out(email)
            st["sas"].append(email)
            save()
            sys.exit(0)
        verbs = [v for v in ("get-iam-policy", "add-iam-policy-binding", "remove-iam-policy-binding") if v in w]
        if verbs:
            verb = verbs[0]
            res = " ".join(w[:w.index(verb)]) + " " + w[w.index(verb) + 1]
            binds = st["policies"].setdefault(res, [])
            if verb == "get-iam-policy":
                out({{"bindings": [{{"role": r, "members": [m]}} for r, m in binds]}})
            if verb == "remove-iam-policy-binding":
                flags.get("--all") is True or unexpected()
                st["policies"][res] = [b for b in binds if b != [flags["--role"], flags["--member"]]]
                save()
                sys.exit(0)
            if cfg.get("fail_binding"):
                fail("binding refused", 2)
            if flags.get("--condition") != "None":
                unexpected()
            [flags["--role"], flags["--member"]] in binds or binds.append([flags["--role"], flags["--member"]])
            save()
            sys.exit(0)
        if w[:2] == ("secrets", "describe"):
            w[2] in st["gcp_secrets"] or fail()
            out(w[2])
        if w[:2] == ("secrets", "create"):
            st["gcp_secrets"][w[2]] = sys.stdin.read()
            save()
            sys.exit(0)
        if w[:3] == ("artifacts", "repositories", "describe"):
            w[3] in st["ar"] or fail()
            out(w[3])
        if w[:3] == ("artifacts", "repositories", "create"):
            st["ar"].append(w[3])
            save()
            sys.exit(0)
        unexpected()
    elif tool == "gh":
        if w[:2] == ("auth", "status"):
            sys.exit(0)
        if w[:2] == ("repo", "view"):
            out({{"nameWithOwner": cfg["repo"]}})
        if w[:2] == ("secret", "set"):
            value = sys.stdin.read()
            if cfg.get("fail_secret") == w[2]:
                fail("HTTP 422", 1)
            st["gh_secrets"].setdefault(flags.get("--env", "repo"), {{}})[w[2]] = value
            save()
            sys.exit(0)
        if w[:2] == ("secret", "list"):
            out([{{"name": n}} for n in st["gh_secrets"].get(flags.get("--env", "repo"), {{}})])
        if w[:2] == ("secret", "delete"):
            st["gh_secrets"].get(flags.get("--env", "repo"), {{}}).pop(w[2], None)
            save()
            sys.exit(0)
        if w[:2] == ("variable", "set"):
            st["vars"][w[2]] = flags["--body"]
            save()
            sys.exit(0)
        if w[0] == "api":
            path, method = w[1].split("?")[0], flags.get("-X", "GET")
            base = f"repos/{{cfg['repo']}}/environments/production"
            if path == f"repos/{{cfg['repo']}}":
                out({{"id": cfg["repo_id"], "full_name": cfg["repo"], "name": cfg["repo"].split("/")[1],
                      "owner": {{"id": cfg["owner_id"], "login": cfg["repo"].split("/")[0], "type": "User"}}}})
            if path.startswith(f"repos/{{cfg['repo']}}/rulesets"):
                rs, rid = st.setdefault("rulesets", []), path.split("/rulesets")[1].strip("/")
                if method == "GET" and not rid:
                    out([{{"id": r["id"], "name": r["name"], "enforcement": r["enforcement"]}} for r in rs])
                if method == "GET":
                    out(next((r for r in rs if str(r["id"]) == rid), None) or fail("HTTP 404: Not Found"))
                body = json.loads(sys.stdin.read())
                if method == "POST":
                    rs.append({{**body, "id": 900 + len(rs)}})
                elif method == "PUT":
                    st["rulesets"] = [{{**body, "id": r["id"]}} if str(r["id"]) == rid else r for r in rs]
                else:
                    unexpected()
                save()
                sys.exit(0)
            if path.startswith("users/"):
                out({{"id": cfg["owner_id"], "login": path[6:]}})
            if path.endswith("/actions/oidc/customization/sub"):
                out({{"use_default": True}})
            if path == base:
                if method == "PUT":
                    body = json.loads(sys.stdin.read())
                    reviewers = [{{"type": r["type"], "reviewer": {{"id": r["id"]}}}} for r in body["reviewers"]]
                    st["env"] = {{"name": "production", "deployment_branch_policy": body["deployment_branch_policy"],
                                 "protection_rules": [{{"type": "required_reviewers", "reviewers": reviewers}}]}}
                    save()
                    sys.exit(0)
                st["env"] or fail("HTTP 404: Not Found")
                out(st["env"])
            if path.startswith(base + "/deployment-branch-policies"):
                st["env"] or fail("HTTP 404: Not Found")
                pols = st["policies_gh"]
                if method == "GET":
                    out({{"total_count": len(pols), "branch_policies": pols}})
                if method == "POST":
                    pols.append({{"id": len(pols) + 100, "name": fields["name"], "type": fields["type"]}})
                if method == "DELETE":
                    st["policies_gh"] = [p for p in pols if str(p["id"]) != path.rsplit("/", 1)[1]]
                save()
                sys.exit(0)
        unexpected()
    elif tool == "kubectl":
        if w[:2] == ("get", "configmap"):
            out(cfg["public_url"])
        if w[:3] == ("get", "secret", "hangar"):
            out(base64.b64encode(cfg["vault_key"].encode()).decode())
        if w[:1] == ("apply",):
            st["k8s"].append(sys.stdin.read())
            save()
            sys.exit(0)
        if w[:2] == ("auth", "can-i"):
            ok = bool(st["k8s"]) and w[3] == "deployments.apps/hangar"
            print("yes" if ok else "no")
            sys.exit(0 if ok else 1)
        unexpected()
    elif tool == "curl":
        header = sys.stdin.read().strip() if "@-" in args else ""
        if header != "Authorization: Bearer " + cfg["admin_token"]:
            fail("curl: (22) The requested URL returned error: 401", 22)
        url, method = args[-1], flags.get("-X", "GET")
        path = url[len(cfg["public_url"]):]
        if path == "/api/tokens" and method == "GET":
            out([{{"id": t["id"], "name": t["name"], "person": t.get("person", "owner")}} for t in st["tokens"]])
        if path == "/api/tokens" and method == "POST":
            body = json.loads(flags["--data"])
            tid = f"tok{{len(st['tokens']) + 1}}"
            st["tokens"].append({{"id": tid, "name": body["name"], "scopes": body["scopes"]}})
            save()
            out({{"id": tid, "name": body["name"], "person": "owner", "token": cfg["minted"]}})
        if path.startswith("/api/tokens/") and method == "DELETE":
            st["tokens"] = [t for t in st["tokens"] if t["id"] != path.rsplit("/", 1)[1]]
            st["revoked"].append(path.rsplit("/", 1)[1])
            save()
            out({{"revoked": path.rsplit("/", 1)[1]}})
        unexpected()
    ''')

MUTATING = re.compile(r"\b(create|create-oidc|update-oidc|undelete|add-iam-policy-binding|remove-iam-policy-binding|"
                      r"enable|apply|set|delete|update)\b")
# The interpreter the script runs under: SETUP_CICD_BASH=/path/to/bash-3.2 runs these tests as macOS's /bin/bash would.
BASH = os.environ.get("SETUP_CICD_BASH", "bash")
IMMUTABLE_SUBJECT = f"repo:Sean-Koval@{OWNER_ID}/hangar@{REPO_ID}:environment:production"


@pytest.fixture
def cicd(tmp_path):
    """A checkout (deploy/ only) and stateful fakes. `run(*flags, **env)` runs the script and returns the result."""
    root = tmp_path / "hangar"
    shutil.copytree(REPO / "deploy", root / "deploy")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("terraform", "gcloud", "kubectl", "gh", "curl"):
        (bin_ / tool).write_text(FAKE.format(python=sys.executable))
        (bin_ / tool).chmod(0o755)
    home = tmp_path / "home"
    (home / ".hangar").mkdir(parents=True)
    (home / ".hangar" / "cli.json").write_text(json.dumps({"url": "https://hangar.test", "token": ADMIN}))
    log, state, unexpected = tmp_path / "calls.log", tmp_path / "state.json", tmp_path / "unexpected.log"
    state.write_text(json.dumps({"pool": None, "provider": None, "sas": [], "policies": {}, "gcp_secrets": {},
                                 "ar": [], "env": None, "policies_gh": [], "gh_secrets": {}, "vars": {}, "k8s": [],
                                 "tokens": [], "revoked": [], "rulesets": []}))
    cfg = {"project": PROJECT, "number": NUMBER, "repo": GH_REPO, "repo_id": REPO_ID, "owner_id": OWNER_ID,
           "public_url": "https://hangar.test", "vault_key": VAULT_KEY, "admin_token": ADMIN, "minted": MINTED,
           "outputs": {"registry": f"us-central1-docker.pkg.dev/{PROJECT}/hangar",
                       "get_credentials": f"gcloud container clusters get-credentials hangar --zone us-central1-a "
                                          f"--project {PROJECT}",
                       "service_account": f"hangar@{PROJECT}.iam.gserviceaccount.com"}}

    class C:
        def __init__(self):
            self.cfg = cfg

        def run(self, *flags, shell_opts=(), **env):
            log.write_text("")
            e = {"PATH": f"{bin_}:{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(home),
                 "FAKE_CFG": json.dumps(self.cfg), "FAKE_STATE": str(state), "FAKE_LOG": str(log),
                 "FAKE_UNEXPECTED": str(unexpected), **env}
            r = subprocess.run([BASH, *shell_opts, str(root / "deploy/gke/setup-cicd.sh"), *flags], env=e,
                               capture_output=True, text=True, timeout=120)
            r.output = r.stdout + r.stderr
            return r

        def calls(self):
            return [json.loads(x) for x in log.read_text().splitlines()]

        def changes(self):
            """Calls that change something: what --dry-run must never make."""
            out = []
            for c in self.calls():
                tool, words = c[0], [a for a in c[1:] if not a.startswith("-")]
                if tool == "gh" and words[:1] == ["api"]:
                    if any(a in ("PUT", "POST", "DELETE") for a in c):
                        out.append(c)
                elif tool == "curl":
                    if any(a in ("POST", "DELETE") for a in c):
                        out.append(c)
                elif tool == "kubectl" and words[:2] == ["auth", "can-i"]:
                    continue  # a question ("may it create pods/exec?"), not a change
                elif tool == "kubectl" and "get" in c and "secret" in c:
                    out.append(c)  # reading the vault key is never part of a dry run
                elif tool in ("gcloud", "gh", "kubectl") and words[:3] != ["container", "clusters", "get-credentials"] \
                        and any(MUTATING.fullmatch(x) for x in words[:4]):
                    out.append(c)
            return out

        def state(self):
            return json.loads(state.read_text())

        def set_state(self, st):
            state.write_text(json.dumps(st))

        def unexpected(self):
            return unexpected.read_text() if unexpected.exists() else ""

    return C()


def _only_admins_make_release_tags(st):
    """One active tag ruleset: only repository admins create, move or delete v* tags, nested ones (v1/x) included,
    since a v* tag starts deploy.yml and its runs sign in to Google Cloud."""
    (rs,) = st["rulesets"]
    assert rs["target"] == "tag" and rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"] == {"include": ["refs/tags/v*", "refs/tags/v*/**/*"], "exclude": []}
    assert sorted(r["type"] for r in rs["rules"]) == ["creation", "deletion", "update"]
    assert rs["bypass_actors"] == [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]


def _no_secret_anywhere(c, r):
    for s in SECRETS:
        assert s not in r.output, f"a secret value reached the output: {s[:8]}..."
        for call in c.calls():
            assert all(s not in a for a in call), f"a secret value was a command-line argument: {call[:4]}"


def test_dry_run_prints_every_change_and_makes_none(cicd):
    r = cicd.run("--dry-run", HANGAR_ACCEPT_GITHUB_TOKEN=ACCEPT)
    assert r.returncode == 0, r.output
    assert cicd.changes() == [], cicd.changes()
    assert not cicd.unexpected(), cicd.unexpected()
    st = cicd.state()
    assert st["pool"] is None and not st["sas"] and not st["gh_secrets"] and not st["tokens"] and not st["k8s"]
    _no_secret_anywhere(cicd, r)
    out = r.output
    for line in (
        "+ gcloud iam workload-identity-pools create github --location=global",
        "+ gcloud iam workload-identity-pools providers create-oidc hangar --workload-identity-pool=github",
        "--issuer-uri=https://token.actions.githubusercontent.com",
        f"assertion.repository == '{GH_REPO}' && assertion.repository_id == '{REPO_ID}'",
        f"assertion.repository_owner_id == '{OWNER_ID}'",
        "+ gcloud iam service-accounts create hangar-ci",
        "+ gcloud iam service-accounts create hangar-deploy",
        "+ gcloud iam service-accounts create hangar-state",  # --- infra-state
        "+ gcloud iam roles create hangarClusterState --project sk-ml-inference",
        "--permissions=container.clusters.get --stage=GA",
        f"--role=projects/{PROJECT}/roles/hangarClusterState",
        "+ gh secret set GCP_STATE_SERVICE_ACCOUNT --repo Sean-Koval/hangar",
        f"+ gh secret set HANGAR_CLUSTER --repo Sean-Koval/hangar  # projects/{PROJECT}/locations/us-central1-a/"
        "clusters/hangar",
        "+ gcloud artifacts repositories add-iam-policy-binding hangar --location=us-central1",
        "--role=roles/artifactregistry.writer",
        "--role=roles/container.clusterViewer",
        f"principal://iam.googleapis.com/projects/{NUMBER}/locations/global/workloadIdentityPools/github/subject/"
        f"repo:{GH_REPO}:environment:production",
        f"principal://iam.googleapis.com/projects/{NUMBER}/locations/global/workloadIdentityPools/github/subject/"
        f"{IMMUTABLE_SUBJECT}",
        f"principalSet://iam.googleapis.com/projects/{NUMBER}/locations/global/workloadIdentityPools/github/"
        f"attribute.repository/{GH_REPO}",
        "verbs: [get, list, watch, patch]",
        f"+ gh api -X PUT repos/{GH_REPO}/environments/production --input -",
        f"+ gh api -X POST repos/{GH_REPO}/rulesets --input -", '"refs/tags/v*"',
        "-f name=main -f type=branch", "-f 'name=v*' -f type=tag",
        "+ gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER",
        f"# projects/{NUMBER}/locations/global/workloadIdentityPools/github/providers/hangar",
        "| gh secret set HANGAR_DEPLOY_TOKEN --env production --repo Sean-Koval/hangar  # the new token on stdin",
        "+ gh secret set HANGAR_ACCEPT_GITHUB_TOKEN --repo Sean-Koval/hangar --env production  # value on stdin",
        "| gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic",
        "+ gcloud artifacts repositories create pypi-remote --location=us",
        "--mode=remote-repository --remote-python-repo=pypi",
        "--remote-npm-repo=npmjs",
        "N2D_CPUS >= 160", "https://console.cloud.google.com/iam-admin/quotas?project=sk-ml-inference",
        "terraform -chdir=deploy/terraform/gke apply",
        "gh workflow run deploy.yml --repo Sean-Koval/hangar --ref main",
        "Dry run: nothing was changed",
    ):
        assert line in out, line + "\n" + out
    for name in ("GCP_DEPLOY_SERVICE_ACCOUNT", "GKE_CLUSTER", "GKE_LOCATION", "HANGAR_URL"):
        assert f"+ gh secret set {name} --repo {GH_REPO} --env production" in out, name
    # What it prints can be pasted: quoted so a shell reads back the exact arguments (macOS's bash 3.2 too)
    printed = shlex.split(next(x for x in out.splitlines() if "create-oidc" in x).split("+ ", 1)[1])
    cond = next(a for a in printed if a.startswith("--attribute-condition="))
    assert cond == (f"--attribute-condition=assertion.repository == '{GH_REPO}' && assertion.repository_id == "
                    f"'{REPO_ID}' && assertion.repository_owner_id == '{OWNER_ID}' && (assertion.ref == "
                    "'refs/heads/main' || assertion.ref.startsWith('refs/tags/v')) && (assertion.event_name == "
                    "'push' || assertion.event_name == 'workflow_dispatch')")
    assert "-d '{\"name\": \"github-deploy\", \"scopes\": [\"admin\"]}' | gh secret set HANGAR_DEPLOY_TOKEN" in out


def test_setup_from_nothing_then_a_rerun_changes_nothing(cicd):
    r = cicd.run(HANGAR_ACCEPT_GITHUB_TOKEN=ACCEPT, HANGAR_ACCEPT_TEST_REPO="Sean-Koval/accept-target")
    assert r.returncode == 0, r.output
    assert not cicd.unexpected(), cicd.unexpected()
    made = cicd.changes()  # what a dry run must not do, so the dry-run test's check means something
    assert any("create-oidc" in c for c in made) and any(c[0] == "curl" and "POST" in c for c in made)
    assert any(c[:2] == ["kubectl", "-n"] and "secret" in c for c in made)
    _no_secret_anywhere(cicd, r)
    st = cicd.state()
    assert st["pool"] == "ACTIVE" and st["provider"]["oidc"]["issuerUri"] == "https://token.actions.githubusercontent.com"
    cond = st["provider"]["attributeCondition"]
    assert f"assertion.repository == '{GH_REPO}'" in cond and "assertion.ref == 'refs/heads/main'" in cond
    assert "assertion.ref.startsWith('refs/tags/v')" in cond
    # only the events the trusted workflows run on: not pull_request_target, workflow_run, schedule or a comment,
    # which run on main's ref too
    assert "(assertion.event_name == 'push' || assertion.event_name == 'workflow_dispatch')" in cond
    assert st["provider"]["attributeMapping"]["google.subject"] == "assertion.sub"
    ci, deploy = (f"{n}@{PROJECT}.iam.gserviceaccount.com" for n in ("hangar-ci", "hangar-deploy"))
    pool = f"projects/{NUMBER}/locations/global/workloadIdentityPools/github"
    writer = ["roles/artifactregistry.writer", f"serviceAccount:{ci}"]
    assert st["policies"]["artifacts repositories hangar"] == [writer]
    # Both subject formats of the production environment: repositories created after 2026-07-15 (this one was
    # created 2026-10-11) get GitHub's immutable `repo:OWNER@ID/REPO@ID:...` subject; older ones the name-only one.
    assert st["policies"][f"iam service-accounts {deploy}"] == [
        ["roles/iam.workloadIdentityUser",
         f"principal://iam.googleapis.com/{pool}/subject/repo:{GH_REPO}:environment:production"],
        ["roles/iam.workloadIdentityUser", f"principal://iam.googleapis.com/{pool}/subject/{IMMUTABLE_SUBJECT}"]]
    assert st["policies"][f"iam service-accounts {ci}"] == [
        ["roles/iam.workloadIdentityUser", f"principalSet://iam.googleapis.com/{pool}/attribute.repository/{GH_REPO}"]]
    # --- infra-state: deploy.yml's state check, before any approval: any run the provider admits, reading the
    # cluster's metadata and labels and nothing else
    state_sa = f"hangar-state@{PROJECT}.iam.gserviceaccount.com"
    state_role = f"projects/{PROJECT}/roles/hangarClusterState"
    assert st["policies"][f"iam service-accounts {state_sa}"] == st["policies"][f"iam service-accounts {ci}"]
    assert st["roles"]["hangarClusterState"]["includedPermissions"] == ["container.clusters.get"]
    assert st["policies"][f"projects {PROJECT}"] == [["roles/container.clusterViewer", f"serviceAccount:{deploy}"],
                                                      [state_role, f"serviceAccount:{state_sa}"]]
    # the deploy account gets nothing else in Google Cloud, and the images account no cluster access
    assert not any(deploy in m for k, b in st["policies"].items() if k != f"projects {PROJECT}" for _, m in b)
    assert "resourceNames: [hangar]" in st["k8s"][0] and "secrets" not in st["k8s"][0]

    assert st["env"]["deployment_branch_policy"] == {"protected_branches": False, "custom_branch_policies": True}
    assert st["env"]["protection_rules"][0]["reviewers"] == [{"type": "User", "reviewer": {"id": OWNER_ID}}]
    assert sorted((p["type"], p["name"]) for p in st["policies_gh"]) == [("branch", "main"), ("tag", "v*")]
    _only_admins_make_release_tags(st)
    repo_s, env_s = st["gh_secrets"]["repo"], st["gh_secrets"]["production"]
    assert repo_s == {"GCP_WORKLOAD_IDENTITY_PROVIDER": f"{pool}/providers/hangar", "GCP_SERVICE_ACCOUNT": ci,
                      "HANGAR_REGISTRY": f"us-central1-docker.pkg.dev/{PROJECT}/hangar",
                      "GCP_STATE_SERVICE_ACCOUNT": state_sa,
                      "HANGAR_CLUSTER": f"projects/{PROJECT}/locations/us-central1-a/clusters/hangar"}
    assert env_s == {"GCP_DEPLOY_SERVICE_ACCOUNT": deploy, "GKE_CLUSTER": "hangar", "GKE_LOCATION": "us-central1-a",
                     "HANGAR_URL": "https://hangar.test", "HANGAR_DEPLOY_TOKEN": MINTED,
                     "HANGAR_ACCEPT_GITHUB_TOKEN": ACCEPT}
    assert st["vars"] == {"HANGAR_ACCEPT_TEST_REPO": "Sean-Koval/accept-target"}
    assert st["tokens"] == [{"id": "tok1", "name": "github-deploy", "scopes": ["admin"]}]
    assert st["gcp_secrets"] == {"hangar-vault-key": VAULT_KEY}  # decoded, piped, never a file or an argument
    assert sorted(st["ar"]) == ["npm-remote", "pypi-remote"]
    hangar_sa = f"serviceAccount:hangar@{PROJECT}.iam.gserviceaccount.com"
    assert st["policies"]["artifacts repositories pypi-remote"] == [["roles/artifactregistry.reader", hangar_sa]]
    assert "FAIL" not in r.output and "ok    hangar-deploy@" in r.output

    again = cicd.run()
    assert again.returncode == 0, again.output
    # Secrets can't be read back, so the public ones are set again; enabling an API and applying the Role are no-ops
    # when nothing changed. Nothing else happens.
    again_ok = (["gh", "secret", "set"], ["gcloud", "services", "enable"], ["kubectl", "apply", "-f"])
    changed = [c for c in cicd.changes() if c[:3] not in again_ok]
    assert changed == [], changed
    assert "HANGAR_DEPLOY_TOKEN is set" in again.output and cicd.state()["tokens"] == st["tokens"]
    assert "provider hangar exists with this condition and mapping" in again.output
    _no_secret_anywhere(cicd, again)


def test_rotation_sets_the_new_token_before_revoking_the_old(cicd):
    assert cicd.run().returncode == 0
    st = cicd.state()  # an admin lists everyone's tokens: a teammate's of the same name isn't ours to revoke
    st["tokens"].append({"id": "tokA", "name": "github-deploy", "scopes": ["admin"], "person": "alice"})
    cicd.set_state(st)
    r = cicd.run("--rotate-deploy-token")
    assert r.returncode == 0, r.output
    calls = cicd.calls()
    set_at = next(i for i, c in enumerate(calls) if c[:4] == ["gh", "secret", "set", "HANGAR_DEPLOY_TOKEN"])
    revoke_at = next(i for i, c in enumerate(calls) if c[0] == "curl" and "DELETE" in c)
    assert set_at < revoke_at and calls[revoke_at][-1].endswith("/api/tokens/tok1")
    assert [t["id"] for t in cicd.state()["tokens"]] == ["tokA", "tok3"]
    _no_secret_anywhere(cicd, r)


def test_a_failed_secret_set_revokes_the_token_it_minted(cicd):
    cicd.cfg["fail_secret"] = "HANGAR_DEPLOY_TOKEN"
    r = cicd.run("--skip", "vault,mirrors")
    assert r.returncode != 0 and "was revoked" in r.output
    assert cicd.state()["tokens"] == [] and cicd.state()["revoked"] == ["tok1"]
    _no_secret_anywhere(cicd, r)


def test_an_earlier_wider_setup_is_narrowed(cicd):
    """What gke.md and the first deploy.yml setup made: a provider any ref could use, roles/container.developer on
    the deploy account, the deploy token as a repository secret (any branch's workflow reads those: the #95 review),
    and a branch policy that lets any branch deploy. A run narrows each."""
    assert cicd.run().returncode == 0
    st = cicd.state()
    ci_email, deploy_email = (f"{n}@{PROJECT}.iam.gserviceaccount.com" for n in ("hangar-ci", "hangar-deploy"))
    ci, deploy = f"serviceAccount:{ci_email}", f"serviceAccount:{deploy_email}"
    pool = f"principalSet://iam.googleapis.com/projects/{NUMBER}/locations/global/workloadIdentityPools/github"
    any_ref = f"{pool}/attribute.repository/{GH_REPO}"  # every run the provider admits, no approval
    st["provider"]["attributeCondition"] = f"assertion.repository == '{GH_REPO}'"
    st["policies"][f"projects {PROJECT}"] += [
        ["roles/container.developer", deploy], ["roles/editor", deploy],
        ["roles/container.developer", ci],  # the first deploy.yml: "without it GCP_SERVICE_ACCOUNT deploys"
        ["roles/viewer", "user:owner@example.com"]]  # not ours to touch
    st["policies"][f"iam service-accounts {deploy_email}"] += [
        ["roles/iam.workloadIdentityUser", any_ref], ["roles/iam.serviceAccountTokenCreator", any_ref]]
    st["policies"][f"iam service-accounts {ci_email}"] += [
        ["roles/iam.workloadIdentityUser", f"{pool}/attribute.repository_owner/Sean-Koval"],
        ["roles/iam.serviceAccountTokenCreator", "user:owner@example.com"]]  # a person, not a federated identity
    # --- infra-state: the state account given more than its one permission, and its role widened
    state_email = f"hangar-state@{PROJECT}.iam.gserviceaccount.com"
    st["policies"][f"projects {PROJECT}"] += [["roles/container.viewer", f"serviceAccount:{state_email}"]]
    st["roles"]["hangarClusterState"]["includedPermissions"] += ["container.clusters.update"]
    (rs,) = st["rulesets"]
    rs.update(enforcement="disabled",
              bypass_actors=rs["bypass_actors"] + [{"actor_id": 4, "actor_type": "RepositoryRole",
                                                    "bypass_mode": "always"}])
    st["policies_gh"].append({"id": 7, "name": "*", "type": "branch"})
    st["gh_secrets"]["repo"]["HANGAR_DEPLOY_TOKEN"] = "old"
    cicd.set_state(st)
    r = cicd.run()
    assert r.returncode == 0, r.output
    assert not cicd.unexpected(), cicd.unexpected()
    st = cicd.state()
    assert "refs/heads/main" in st["provider"]["attributeCondition"] and "updating it" in r.output
    assert st["policies"][f"projects {PROJECT}"] == [
        ["roles/container.clusterViewer", deploy],
        [f"projects/{PROJECT}/roles/hangarClusterState", f"serviceAccount:{state_email}"],
        ["roles/viewer", "user:owner@example.com"]]
    assert st["roles"]["hangarClusterState"]["includedPermissions"] == ["container.clusters.get"]
    subject = pool.replace("principalSet://", "principal://") + "/subject/"
    assert st["policies"][f"iam service-accounts {deploy_email}"] == [
        ["roles/iam.workloadIdentityUser", f"{subject}repo:{GH_REPO}:environment:production"],
        ["roles/iam.workloadIdentityUser", f"{subject}{IMMUTABLE_SUBJECT}"]]
    assert st["policies"][f"iam service-accounts {ci_email}"] == [
        ["roles/iam.workloadIdentityUser", any_ref], ["roles/iam.serviceAccountTokenCreator", "user:owner@example.com"]]
    _only_admins_make_release_tags(st)
    assert sorted((p["type"], p["name"]) for p in st["policies_gh"]) == [("branch", "main"), ("tag", "v*")]
    assert "HANGAR_DEPLOY_TOKEN" not in st["gh_secrets"]["repo"] and "HANGAR_URL" not in st["gh_secrets"]["repo"]
    assert "FAIL" not in r.output


def test_another_provider_in_the_pool_fails_the_check(cicd):
    """The accounts trust the pool's principals, which every provider in it can mint: one with a looser condition
    (gke.md's any-ref one under another name, or one for another repository) would undo the ref restriction."""
    assert cicd.run().returncode == 0
    st = cicd.state()
    st["other_providers"] = ["legacy"]
    cicd.set_state(st)
    r = cicd.run()
    assert r.returncode == 1 and "FAIL" in r.output and "legacy" in r.output, r.output
    assert not cicd.unexpected(), cicd.unexpected()


def test_no_secret_under_xtrace_or_in_a_here_string(cicd):
    """`bash -x setup-cicd.sh` while debugging must not trace the tokens or the vault key; and secrets never go
    through a here-string, which bash before 5.1 (macOS's 3.2) writes to a temporary file."""
    r = cicd.run(shell_opts=("-x",), HANGAR_ACCEPT_GITHUB_TOKEN=ACCEPT)
    assert r.returncode == 0, r.output
    assert cicd.state()["gcp_secrets"] == {"hangar-vault-key": VAULT_KEY}
    _no_secret_anywhere(cicd, r)
    text = SCRIPT.read_text()
    assert not re.search(r'<<<\s*"?\$\{?(minted|key64|ADMIN_TOKEN|value|listing)\b', text)


def test_no_admin_token_is_a_clear_error(cicd, tmp_path):
    r = cicd.run("--skip", "vault,mirrors", HANGAR_CLI_CONFIG=str(tmp_path / "none.json"),
                 HANGAR_URL="https://hangar.test")
    assert r.returncode != 0 and "no admin token for https://hangar.test" in r.output
    _no_secret_anywhere(cicd, r)


def _secrets_read(job: dict) -> set[str]:
    return set(re.findall(r"secrets\.([A-Z_]+)", json.dumps(job)))


def test_the_workflows_read_what_the_script_sets_at_the_level_it_sets_them():
    """Repository secrets for every job outside the production environment (images.yml, deploy.yml's setup and
    image build), environment secrets for the deploy job; the canary needs no Google Cloud; `id-token: write` only on
    jobs that sign in with Workload Identity Federation, which keep the provider's default audience."""
    text = SCRIPT.read_text()
    repo_level = set(re.findall(r"^  set_secret ([A-Z_]+) \S+ public$", text, re.M))
    env_level = set(re.findall(r"^  set_secret ([A-Z_]+) \S+ public --env", text, re.M)) | {"HANGAR_DEPLOY_TOKEN"}
    optional = {"HANGAR_ACCEPT_GITHUB_TOKEN", "HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET"}
    assert repo_level == {"GCP_WORKLOAD_IDENTITY_PROVIDER", "GCP_SERVICE_ACCOUNT", "HANGAR_REGISTRY",
                          "GCP_STATE_SERVICE_ACCOUNT", "HANGAR_CLUSTER"}  # --- infra-state: names, not credentials

    deploy = yaml.safe_load((WORKFLOWS / "deploy.yml").read_text())
    images = yaml.safe_load((WORKFLOWS / "images.yml").read_text())
    outside = set().union(*(_secrets_read(j) for n, j in deploy["jobs"].items() if n != "deploy"),
                          *(_secrets_read(j) for j in images["jobs"].values()))
    assert outside == repo_level
    assert _secrets_read(deploy["jobs"]["deploy"]) - optional == env_level | {"GCP_WORKLOAD_IDENTITY_PROVIDER",
                                                                            "HANGAR_REGISTRY"}
    assert deploy["jobs"]["deploy"]["environment"] == "production"
    # The deploy Role grants get, list, watch and patch on deployments/hangar: enough for `set image` (get, patch)
    # and `rollout status` (get, then list and watch by name), not for `rollout undo`, which lists the namespace's
    # ReplicaSets. The rollback sets the previous image instead.
    assert not any("rollout undo" in s.get("run", "") for s in deploy["jobs"]["deploy"]["steps"])

    for wf in WORKFLOWS.glob("*.yml"):
        doc = yaml.safe_load(wf.read_text())
        assert (doc.get("permissions") or {}).get("id-token") is None, f"{wf.name}: id-token at the workflow level"
        for name, job in doc["jobs"].items():
            steps = job.get("steps", [])
            auth = [s for s in steps if str(s.get("uses", "")).startswith("google-github-actions/auth@")]
            calls_images = job.get("uses") == "./.github/workflows/images.yml"
            # GitHub Pages' deploy-pages proves the run to Pages with an OIDC token too (docs.yml)
            pages = any(str(s.get("uses", "")).startswith("actions/deploy-pages@") for s in steps)
            wants = (job.get("permissions") or {}).get("id-token") == "write"
            assert wants == bool(auth or calls_images or pages), f"{wf.name}:{name}"
            for s in auth:
                assert "audience" not in s.get("with", {}), f"{wf.name}:{name}: the provider accepts its own name"
            if wf.name.startswith("harness-canary"):
                assert not auth and "GCP_" not in json.dumps(job), f"{wf.name}:{name}"


@pytest.mark.parametrize("ref, images, deploy", [
    ("refs/heads/main", "true", "true"),
    ("refs/tags/v1.4.0", "true", "true"),
    ("refs/heads/feature", "false", "refused"),
    ("refs/pull/7/merge", "false", "refused"),
])
def test_runs_outside_the_trusted_refs_stop_before_signing_in(tmp_path, ref, images, deploy):
    """The provider admits only main and v* tags: images.yml skips elsewhere (green, with a notice) instead of failing
    to sign in, and deploy.yml refuses before it builds anything. Missing secrets still skip, on any ref."""
    def check(wf, job, step_id, **env):
        doc = yaml.safe_load((WORKFLOWS / wf).read_text())
        step = next(s for s in doc["jobs"][job]["steps"] if s.get("id") == step_id)
        out = tmp_path / "out"
        out.write_text("")
        r = subprocess.run(["bash", "-e", "-c", step["run"]], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out), "GITHUB_REF": ref, **env})
        return "refused" if r.returncode else out.read_text().strip().removeprefix("ready=")

    secrets = {"WIF_PROVIDER": "p", "WIF_SERVICE_ACCOUNT": "sa", "HANGAR_REGISTRY": "r"}
    assert check("images.yml", "build", "setup", **secrets) == images
    assert check("deploy.yml", "setup", "check", **secrets) == deploy
    assert check("images.yml", "build", "setup") == "false" and check("deploy.yml", "setup", "check") == "false"


def test_shellcheck_is_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck isn't installed (CI's lint job runs it)")
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
