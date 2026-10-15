#!/usr/bin/env bash
# One-time setup for deploying hangar from GitHub Actions instead of a laptop (docs/cicd.md): the Google Cloud and
# GitHub identity that .github/workflows/deploy.yml and images.yml sign in with, the `production` environment that
# gates deploys on your approval, and the upgrade steps a script can do safely (docs/upgrading.md). Safe to re-run:
# each step looks first and changes only what is missing or different.
#
#   bash deploy/gke/setup-cicd.sh --dry-run    # look (read-only calls), and print every change it would make
#   bash deploy/gke/setup-cicd.sh              # make them
#   bash deploy/gke/setup-cicd.sh --rotate-deploy-token   # also replace HANGAR_DEPLOY_TOKEN (and revoke the old one)
#   bash deploy/gke/setup-cicd.sh --skip vault,mirrors    # leave steps out (apis wif accounts github vault mirrors)
#
# Run it from the checkout, as the project's owner, with gcloud, kubectl and gh logged in (and python3 and curl).
# Every value has a default, detected in this order; set the variable to override it:
#   PROJECT, REGION, REGISTRY     deploy/terraform/gke's outputs, else `gcloud config` (core/project, compute/region)
#   CLUSTER, GKE_LOCATION         terraform's get_credentials output, else the project's only GKE cluster
#   HANGAR_SA                     terraform's service_account output, else hangar@PROJECT.iam.gserviceaccount.com
#   REPO                          `gh repo view` (OWNER/REPO)
#   REVIEWER                      the repository's owner (a user), else you (`gh api user`)
#   HANGAR_URL                    $HANGAR_URL, else `hangar login`'s saved URL, else the hangar config map's
#   HANGAR_TOKEN                  an admin token to mint the deploy token with: $HANGAR_TOKEN, else `hangar login`'s
#                                 saved one for HANGAR_URL, else ~/.hangar/gke-admin-token (deploy/gke/up.sh's)
#   POOL, PROVIDER                the workload identity pool and provider (github, hangar: docs/gke.md's names)
#   MIRROR_LOCATION               where the package mirrors' remote repositories go (us)
# Optional acceptance fixtures (docs/acceptance.md), set in `production` when present in the environment:
#   HANGAR_ACCEPT_TEST_REPO, HANGAR_ACCEPT_FIXTURE_REPO (variables), HANGAR_ACCEPT_GITHUB_TOKEN and
#   HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET (secrets, piped to gh; never arguments).
#
# Secret values (the deploy token, the vault key, acceptance tokens) go from one program to the next on stdin. They
# are never printed, never on a command line, and never in a file (no here-strings: bash before 5.1, macOS's 3.2
# included, writes those to a temporary file). Tracing is off even under `bash -x`: it would print them.
set +x
set -euo pipefail
cd "$(dirname "$0")/../.."

TF=deploy/terraform/gke
NS=${NAMESPACE:-hangar}
ENVIRONMENT=production
POOL=${POOL:-github}
PROVIDER=${PROVIDER:-hangar}
CI_SA_NAME=${CI_SA_NAME:-hangar-ci}
DEPLOY_SA_NAME=${DEPLOY_SA_NAME:-hangar-deploy}
STATE_SA_NAME=${STATE_SA_NAME:-hangar-state}  # --- infra-state: deploy.yml's read-only infra check
STATE_ROLE_ID=${STATE_ROLE_ID:-hangarClusterState}
STATE_PERMISSIONS=container.clusters.get
TOKEN_NAME=${TOKEN_NAME:-github-deploy}
MIRROR_LOCATION=${MIRROR_LOCATION:-us}
VAULT_SECRET=${VAULT_SECRET:-hangar-vault-key}
ISSUER=https://token.actions.githubusercontent.com

DRY=0
ROTATE=0
SKIP=,
usage() { sed -n '2,/^set +x/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --rotate-deploy-token) ROTATE=1 ;;
    --skip) SKIP=",${2:?--skip needs a list},"; shift ;;
    --skip=*) SKIP=",${1#--skip=}," ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (--help)" >&2; exit 2 ;;
  esac
  shift
done
for s in ${SKIP//,/ }; do
  case $s in apis|wif|accounts|github|vault|mirrors) ;; *) echo "--skip: no step '$s' (apis wif accounts github vault mirrors)" >&2; exit 2 ;; esac
done

# ------------------------------------------------------------------------------------------------- helpers
step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
skipped() { case $SKIP in *",$1,"*) note "skipped (--skip $1)"; return 0 ;; esac; return 1; }

# A command line as you could paste it: plain words as they are, the rest single-quoted.
show() {
  local out="" a q="'\\''"  # q: a quote inside single quotes. As a variable: bash 3.2 quotes ${a//…/…} differently.
  for a in "$@"; do
    case $a in
      *[!A-Za-z0-9_./:=@,+%-]*|"") a=${a//\'/$q}; a="'$a'" ;;
    esac
    out="$out $a"
  done
  printf '%s' "${out# }"
}

# Changes something: printed, then run unless --dry-run.
run() {
  printf '   + %s\n' "$(show "$@")"
  [ "$DRY" = 1 ] || "$@"
}

# IAM is eventually consistent: a service account made a moment ago can be "not found" by the next call.
run_retry() {
  printf '   + %s\n' "$(show "$@")"
  [ "$DRY" = 1 ] && return 0
  local n=1
  until "$@"; do
    [ "$n" -ge 6 ] && return 1
    note "retrying in $((n * 5)) s (IAM changes take a moment to propagate)"
    sleep $((n * 5)); n=$((n + 1))
  done
}

# Reads something (run even with --dry-run); prints its output, nothing on failure.
look() { "$@" 2>/dev/null || true; }

# `gh secret set` with the value on stdin. Only values marked public are shown.
set_secret() {  # NAME VALUE public|secret [gh flags...]
  local name=$1 value=$2 kind=$3; shift 3
  if [ "$kind" = public ]; then
    printf '   + %s  # %s\n' "$(show gh secret set "$name" --repo "$REPO" ${1+"$@"})" "$value"
  else
    printf '   + %s  # value on stdin, not shown\n' "$(show gh secret set "$name" --repo "$REPO" ${1+"$@"})"
  fi
  [ "$DRY" = 1 ] || printf '%s' "$value" | gh secret set "$name" --repo "$REPO" ${1+"$@"}
}

# EXPR [ARGS...] < JSON: a python expression over `d` (the parsed input) and `a` (ARGS, so values are never pasted
# into the code); prints a str as it is, anything else as JSON.
json() {
  python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
v = eval(sys.argv[1], {"d": d, "a": sys.argv[2:]})
print(v if isinstance(v, str) else json.dumps(v))' "$@"
}

# Hangar's REST as an admin. The token goes in a header curl reads from stdin, so it is never an argument.
hangar_api() {  # METHOD PATH [BODY]
  local method=$1 path=$2 body=${3:-}
  if [ -n "$body" ]; then
    printf 'Authorization: Bearer %s\n' "$ADMIN_TOKEN" | curl -fsS --max-time 30 -X "$method" -H @- \
      -H 'Content-Type: application/json' --data "$body" "$HANGAR_URL$path"
  else
    printf 'Authorization: Bearer %s\n' "$ADMIN_TOKEN" | curl -fsS --max-time 30 -X "$method" -H @- "$HANGAR_URL$path"
  fi
}

# Does the IAM policy (JSON on stdin) bind MEMBER to ROLE without a condition?
has_binding() {  # ROLE MEMBER
  python3 -c '
import json, sys
try:
    p = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
ok = any(b.get("role") == sys.argv[1] and sys.argv[2] in b.get("members", []) and not b.get("condition")
         for b in p.get("bindings", []))
sys.exit(0 if ok else 1)' "$1" "$2"
}

# What an IAM policy (JSON on stdin) grants the members PATTERNS match (space-separated, * wildcards) beyond the
# ROLE=MEMBER pairs this setup grants: "ROLE MEMBER" lines, for narrowing what an earlier setup left.
unwanted() {  # PATTERNS [ROLE=MEMBER...]
  python3 -c '
import fnmatch, json, sys
try:
    p = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
keep, seen = set(sys.argv[2:]), set()
for b in p.get("bindings", []):
    for m in b.get("members", []):
        pair = b.get("role", "") + "=" + m
        if any(fnmatch.fnmatchcase(m, x) for x in sys.argv[1].split()) and pair not in keep | seen:
            seen.add(pair)
            print(b.get("role", ""), m)' "$@"
}

KCFG=$(mktemp)
cleanup() { rm -f "$KCFG"; }
trap cleanup EXIT
kc() { KUBECONFIG=$KCFG kubectl "$@"; }

# ------------------------------------------------------------------------------------------------- what and where
step "What this sets up, and where$([ "$DRY" = 1 ] && echo ' (dry run: nothing is changed)')"
for t in gcloud gh kubectl python3 curl; do command -v "$t" >/dev/null || die "$t isn't installed"; done
gh auth status >/dev/null 2>&1 || die "gh isn't logged in: gh auth login"
[ -n "$(look gcloud config get-value account)" ] || die "gcloud isn't logged in: gcloud auth login"

tfo() { command -v terraform >/dev/null || return 0; terraform -chdir="$TF" output -raw "$1" 2>/dev/null || true; }
REGISTRY=${REGISTRY:-$(tfo registry)}
CREDS=$(tfo get_credentials)  # gcloud container clusters get-credentials NAME --zone ZONE --project PROJECT
PROJECT=${PROJECT:-}
[ -n "$PROJECT" ] || PROJECT=$(cut -s -d/ -f2 <<<"$REGISTRY")
[ -n "$PROJECT" ] || PROJECT=$(sed -nE 's/.*--project ([^ ]+).*/\1/p' <<<"$CREDS")
[ -n "$PROJECT" ] || PROJECT=$(look gcloud config get-value project)
[ -n "$PROJECT" ] || die "no project: set PROJECT, or gcloud config set project <id>"
REGION=${REGION:-}
case $REGISTRY in *-docker.pkg.dev/*) [ -n "$REGION" ] || REGION=${REGISTRY%%-docker.pkg.dev*} ;; esac
[ -n "$REGION" ] || REGION=$(look gcloud config get-value compute/region)
REGION=${REGION:-us-central1}
REGISTRY=${REGISTRY:-$REGION-docker.pkg.dev/$PROJECT/hangar}
AR_REPO=${REGISTRY##*/}
CLUSTER=${CLUSTER:-$(awk '{print $5}' <<<"$CREDS")}
GKE_LOCATION=${GKE_LOCATION:-$(sed -nE 's/.*--(zone|location|region) ([^ ]+).*/\2/p' <<<"$CREDS")}
if [ -z "$CLUSTER" ] || [ -z "$GKE_LOCATION" ]; then
  clusters=$(look gcloud container clusters list --project "$PROJECT" --format='value(name,location)')
  [ "$(grep -c . <<<"$clusters")" = 1 ] || die "set CLUSTER and GKE_LOCATION: the project has $(grep -c . <<<"$clusters") GKE clusters"
  CLUSTER=${CLUSTER:-$(awk '{print $1}' <<<"$clusters")}
  GKE_LOCATION=${GKE_LOCATION:-$(awk '{print $2}' <<<"$clusters")}
fi
HANGAR_SA=${HANGAR_SA:-$(tfo service_account)}
HANGAR_SA=${HANGAR_SA:-hangar@$PROJECT.iam.gserviceaccount.com}
PROJECT_NUMBER=$(look gcloud projects describe "$PROJECT" --format='value(projectNumber)')
[ -n "$PROJECT_NUMBER" ] || die "can't read project $PROJECT (gcloud projects describe): is it the right one, and are you its owner?"

REPO=${REPO:-$(look gh repo view --json nameWithOwner --jq .nameWithOwner)}
[ -n "$REPO" ] || die "no repository: run this in the checkout, or set REPO=OWNER/REPO"
REPO_JSON=$(look gh api "repos/$REPO")
REPO_ID=$(json 'd["id"]' <<<"$REPO_JSON" || true)
OWNER_ID=$(json 'd["owner"]["id"]' <<<"$REPO_JSON" || true)
FULL_NAME=$(json 'd["full_name"]' <<<"$REPO_JSON" || true)
if [ -z "$REPO_ID" ] || [ -z "$OWNER_ID" ] || [ -z "$FULL_NAME" ]; then die "can't read $REPO with gh (gh api repos/$REPO)"; fi
# GitHub's own spelling: the claims and subjects the bindings match are case-sensitive. These go into the provider's
# condition, so anything but a plain OWNER/REPO and numeric ids is refused.
REPO=$FULL_NAME
case $REPO in ?*/?*) ;; *) die "unexpected repository name '$REPO'" ;; esac
case $REPO in */*/*|*[!A-Za-z0-9._/-]*) die "unexpected repository name '$REPO'" ;; esac
case $REPO_ID$OWNER_ID in *[!0-9]*) die "unexpected ids for $REPO: $REPO_ID, $OWNER_ID" ;; esac
if [ -z "${REVIEWER:-}" ]; then
  if [ "$(json 'd["owner"]["type"]' <<<"$REPO_JSON")" = User ]; then REVIEWER=$(json 'd["owner"]["login"]' <<<"$REPO_JSON")
  else REVIEWER=$(look gh api user --jq .login); fi
fi
REVIEWER_ID=$(look gh api "users/$REVIEWER" --jq .id)
[ -n "$REVIEWER_ID" ] || die "can't find the GitHub user $REVIEWER"

# A kubeconfig of its own, so this never changes (or acts on) your current kubectl context.
look env KUBECONFIG="$KCFG" gcloud container clusters get-credentials "$CLUSTER" --location "$GKE_LOCATION" \
  --project "$PROJECT" >/dev/null

CLI_JSON=${HANGAR_CLI_CONFIG:-$HOME/.hangar/cli.json}
saved() { if [ -f "$CLI_JSON" ]; then json 'd.get(a[0]) or ""' "$1" < "$CLI_JSON" 2>/dev/null || true; fi; }
HANGAR_URL=${HANGAR_URL:-$(saved url)}
[ -n "$HANGAR_URL" ] || HANGAR_URL=$(look kc -n "$NS" get configmap hangar -o jsonpath='{.data.HANGAR_PUBLIC_URL}')
HANGAR_URL=${HANGAR_URL%/}
case $HANGAR_URL in
  https://hangar.example.com|"") die "no hangar URL: set HANGAR_URL=https://<your hangar> (or hangar login first)" ;;
  https://*) ;;
  *) die "HANGAR_URL must be https:// (it is $HANGAR_URL): the deploy talks to hangar over the internet" ;;
esac

WIF_POOL=projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL
WIF_PROVIDER=$WIF_POOL/providers/$PROVIDER
CI_SA=$CI_SA_NAME@$PROJECT.iam.gserviceaccount.com
DEPLOY_SA=$DEPLOY_SA_NAME@$PROJECT.iam.gserviceaccount.com
STATE_SA=$STATE_SA_NAME@$PROJECT.iam.gserviceaccount.com
STATE_ROLE=projects/$PROJECT/roles/$STATE_ROLE_ID
HANGAR_CLUSTER=projects/$PROJECT/locations/$GKE_LOCATION/clusters/$CLUSTER
# Who may sign in through the provider at all: this repository, by name and by id (ids don't come back if the repo or
# its owner is deleted and the name taken: Google's guidance), only runs of main or a v* release tag, the refs the
# production environment deploys from, and only on the events the deploy and images workflows run on (a push, or
# started by hand). Pull requests, other branches and forks get no Google token, nor do pull_request_target,
# workflow_run, schedule or comment runs, which run on main's ref too.
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref"
CONDITION="assertion.repository == '$REPO' && assertion.repository_id == '$REPO_ID' && assertion.repository_owner_id == '$OWNER_ID' && (assertion.ref == 'refs/heads/main' || assertion.ref.startsWith('refs/tags/v')) && (assertion.event_name == 'push' || assertion.event_name == 'workflow_dispatch')"
# images.yml (and deploy.yml's image build) as the images account: any run the provider lets in.
CI_MEMBER="principalSet://iam.googleapis.com/$WIF_POOL/attribute.repository/$REPO"
# The deploy account: only jobs in the production environment, which run after your approval. GitHub's subject for
# them is repo:OWNER/REPO:environment:production, or, for repositories created (or renamed, or transferred) after
# 2026-07-15 and those that opted in, the immutable repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:production. Both
# name this repository's production environment, so both are bound.
DEPLOY_MEMBER="principal://iam.googleapis.com/$WIF_POOL/subject/repo:$REPO:environment:$ENVIRONMENT"
DEPLOY_MEMBER_IDS="principal://iam.googleapis.com/$WIF_POOL/subject/repo:${REPO%%/*}@$OWNER_ID/${REPO#*/}@$REPO_ID:environment:$ENVIRONMENT"
FEDERATED="principal://* principalSet://*"
# Only repository admins create, move or delete v* tags: a v* tag starts deploy.yml, whose builds sign in to Google
# Cloud as the images account before your approval, so whoever could push one would run that commit's workflows with
# it. Nested ones (v1/x) too: the provider's condition admits refs/tags/v1/x, and `*` stops at a slash.
RULESET_NAME="release tags (deploy/gke/setup-cicd.sh)"
RULESET="{\"name\": \"$RULESET_NAME\", \"target\": \"tag\", \"enforcement\": \"active\", \"bypass_actors\": [{\"actor_id\": 5, \"actor_type\": \"RepositoryRole\", \"bypass_mode\": \"always\"}], \"conditions\": {\"ref_name\": {\"include\": [\"refs/tags/v*\", \"refs/tags/v*/**/*\"], \"exclude\": []}}, \"rules\": [{\"type\": \"creation\"}, {\"type\": \"update\"}, {\"type\": \"deletion\"}]}"
RULESET_SAME='d.get("target") == "tag" and d.get("enforcement") == "active" and sorted(d.get("conditions", {}).get("ref_name", {}).get("include", [])) == ["refs/tags/v*", "refs/tags/v*/**/*"] and not d.get("conditions", {}).get("ref_name", {}).get("exclude") and sorted(r["type"] for r in d.get("rules", [])) == ["creation", "deletion", "update"] and [(b.get("actor_type"), b.get("actor_id"), b.get("bypass_mode", "always")) for b in d.get("bypass_actors") or []] == [("RepositoryRole", 5, "always")]'
# Over GET repos/R/environments/E: who must approve, and whether only named branches and tags may deploy
REVIEWER_IDS='[r["reviewer"]["id"] for x in d.get("protection_rules", []) if x.get("type") == "required_reviewers" for r in x.get("reviewers", [])]'
CUSTOM_POLICIES='d.get("deployment_branch_policy") == {"protected_branches": False, "custom_branch_policies": True}'

cat <<EOF
   project         $PROJECT (number $PROJECT_NUMBER)
   cluster         $CLUSTER in $GKE_LOCATION
   registry        $REGISTRY
   hangar          $HANGAR_URL (service account $HANGAR_SA)
   repository      $REPO (id $REPO_ID); deploys approved by $REVIEWER
   identities      $CI_SA (images), $DEPLOY_SA (deploys), $STATE_SA (is the infra up?); the canary needs none
EOF

if [ "$(look gh api "repos/$REPO/actions/oidc/customization/sub" --jq .use_default)" = false ]; then
  note "warning: $REPO customizes its OIDC subject claim; the deploy binding expects GitHub's default"
  note "  (repo:$REPO:environment:$ENVIRONMENT). Reset it, or bind $DEPLOY_SA to your template's subject."
fi

# ------------------------------------------------------------------------------------------------- 1. APIs
step "1. APIs: STS and IAM Credentials (Workload Identity Federation), Secret Manager"
if ! skipped apis; then
  run gcloud services enable sts.googleapis.com iamcredentials.googleapis.com iam.googleapis.com \
    secretmanager.googleapis.com artifactregistry.googleapis.com container.googleapis.com --project "$PROJECT"
fi

# ------------------------------------------------------------------------------------------------- 2. WIF
step "2. Workload Identity Federation: pool $POOL, provider $PROVIDER (GitHub's OIDC tokens)"
if ! skipped wif; then
  wif=(--location=global --project="$PROJECT")
  case $(look gcloud iam workload-identity-pools describe "$POOL" "${wif[@]}" --format='value(state)') in
    ACTIVE) note "pool $POOL exists" ;;
    DELETED) run gcloud iam workload-identity-pools undelete "$POOL" "${wif[@]}" ;;
    *) run gcloud iam workload-identity-pools create "$POOL" "${wif[@]}" --display-name="GitHub Actions" \
         --description="GitHub Actions runs of $REPO (deploy/gke/setup-cicd.sh)" ;;
  esac
  prov=(--workload-identity-pool="$POOL" "${wif[@]}")
  current=$(look gcloud iam workload-identity-pools providers describe "$PROVIDER" "${prov[@]}" --format=json)
  state=$(json 'd.get("state", "")' <<<"$current" || true)
  [ "$state" != DELETED ] || run gcloud iam workload-identity-pools providers undelete "$PROVIDER" "${prov[@]}"
  if [ -z "$state" ]; then
    run gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" "${prov[@]}" \
      --display-name="GitHub Actions" --issuer-uri="$ISSUER" --attribute-mapping="$MAPPING" \
      --attribute-condition="$CONDITION"
  elif [ "$(json 'd.get("attributeCondition", "")' <<<"$current")" = "$CONDITION" ] &&
       [ "$(json 'sorted(d.get("attributeMapping", {}).items())' <<<"$current")" = \
         "$(python3 -c 'import json,sys; print(json.dumps(sorted(dict(x.split("=",1) for x in sys.argv[1].split(",")).items())))' "$MAPPING")" ] &&
       [ "$(json 'd.get("oidc", {}).get("issuerUri", "")' <<<"$current")" = "$ISSUER" ]; then
    note "provider $PROVIDER exists with this condition and mapping"
  else
    note "provider $PROVIDER exists with another condition or mapping: updating it"
    run gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" "${prov[@]}" \
      --issuer-uri="$ISSUER" --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
  fi
  note "condition: $CONDITION"
  # The accounts trust the pool's identities, which any provider in the pool can mint, whatever its condition.
  others=$(look gcloud iam workload-identity-pools providers list "${prov[@]}" --format='value(name.basename())' |
    grep -vxF -- "$PROVIDER" || true)
  if [ -n "$others" ]; then
    note "warning: pool $POOL has other providers ($(paste -sd, - <<<"$others")). Any can sign in as the accounts below,"
    note "  past this provider's condition: delete them (gcloud iam workload-identity-pools providers delete), or set"
    note "  POOL to a pool of its own. The check (step 9) fails until then."
  fi
fi

# ------------------------------------------------------------------------------------------------- 3. accounts
step "3. Service accounts, one per workflow role, each with only what it needs"
ensure_sa() {  # NAME EMAIL DESCRIPTION
  if [ -n "$(look gcloud iam service-accounts describe "$2" --project "$PROJECT" --format='value(email)')" ]; then
    note "service account $2 exists"
  else
    run gcloud iam service-accounts create "$1" --project "$PROJECT" --display-name="$1" --description="$3"
  fi
}
bind() {  # artifacts|sa|project RESOURCE ROLE MEMBER [flags]: add the binding unless it is there
  local res=$2 role=$3 member=$4 group=projects
  case $1 in
    artifacts) group="artifacts repositories" ;;
    sa) group="iam service-accounts" ;;
  esac
  shift 4
  # shellcheck disable=SC2086 # $group is one or two words on purpose
  if look gcloud $group get-iam-policy "$res" ${1+"$@"} --project "$PROJECT" --format=json | has_binding "$role" "$member"
  then
    note "$role on $res: $member already"
  else
    # shellcheck disable=SC2086
    run_retry gcloud $group add-iam-policy-binding "$res" ${1+"$@"} --project "$PROJECT" --role="$role" \
      --member="$member" --condition=None --format=none
  fi
}
if ! skipped accounts; then
  note "images ($CI_SA): pushes environment images, harness packs and the control-plane image to $AR_REPO only."
  note "images.yml builds with Docker on the runner (--builder docker), so no Cloud Build role."
  ensure_sa "$CI_SA_NAME" "$CI_SA" "GitHub Actions: images.yml and deploy.yml's image build push to $REGISTRY"
  bind artifacts "$AR_REPO" roles/artifactregistry.writer "serviceAccount:$CI_SA" --location="$REGION"
  bind sa "$CI_SA" roles/iam.workloadIdentityUser "$CI_MEMBER"

  note "deploy ($DEPLOY_SA): fetches the cluster's credentials (roles/container.clusterViewer: container.clusters.get,"
  note "which authorizes nothing inside the cluster); a Role in $NS lets it read and patch deploy/hangar, nothing else."
  ensure_sa "$DEPLOY_SA_NAME" "$DEPLOY_SA" "GitHub Actions: deploy.yml in the $ENVIRONMENT environment rolls out deploy/hangar"
  bind project "$PROJECT" roles/container.clusterViewer "serviceAccount:$DEPLOY_SA"
  bind sa "$DEPLOY_SA" roles/iam.workloadIdentityUser "$DEPLOY_MEMBER"
  bind sa "$DEPLOY_SA" roles/iam.workloadIdentityUser "$DEPLOY_MEMBER_IDS"

  # Narrowing an earlier setup: the first deploy.yml gave roles/container.developer (every Secret, exec into every
  # pod) to the deploy account, or to the images account when there was no deploy account (#95 review), and an
  # account bound by repository alone is usable from any run the provider admits, without your approval. Each account
  # keeps only what is granted above: no other project role, and no other federated identity on it.
  narrow() {  # projects|sa RESOURCE PATTERNS [ROLE=MEMBER...]
    local group=projects res=$2 extra role member
    [ "$1" = sa ] && group="iam service-accounts"
    shift 2
    # shellcheck disable=SC2086 # $group is one or two words on purpose
    extra=$(look gcloud $group get-iam-policy "$res" --project "$PROJECT" --format=json | unwanted "$@")
    while read -r role member; do
      [ -n "$role" ] || continue
      note "$member has $role on $res, which this setup doesn't grant: removing it"
      # shellcheck disable=SC2086
      run gcloud $group remove-iam-policy-binding "$res" --project "$PROJECT" --role="$role" --member="$member" \
        --all --format=none </dev/null
    done <<<"$extra"
  }
  # --- infra-state: a merge to main deploys only while the infra is up, which deploy.yml's `state` job reads from
  # the cluster's power labels before any approval (so not as the deploy account, which only the approved job can
  # use). Its account holds a custom role with container.clusters.get alone, on the project (GKE has no per-cluster
  # IAM): the metadata and labels of the project's clusters; not list or connect, and inside a cluster no binding, so
  # only the discovery Kubernetes grants every authenticated identity. It can change nothing.
  note "state ($STATE_SA): reads the cluster's metadata and labels only ($STATE_ROLE: $STATE_PERMISSIONS)."
  role_json=$(look gcloud iam roles describe "$STATE_ROLE_ID" --project "$PROJECT" --format=json)
  if [ -z "$role_json" ]; then
    run gcloud iam roles create "$STATE_ROLE_ID" --project "$PROJECT" --title="hangar cluster state (read)" \
      --description="deploy.yml reads whether the infra is up (deploy/gke/setup-cicd.sh)" \
      --permissions="$STATE_PERMISSIONS" --stage=GA
  else
    if [ "$(json 'bool(d.get("deleted"))' <<<"$role_json")" = true ]; then
      run gcloud iam roles undelete "$STATE_ROLE_ID" --project "$PROJECT"
    fi
    if [ "$(json '",".join(sorted(d.get("includedPermissions", [])))' <<<"$role_json")" = "$STATE_PERMISSIONS" ]; then
      note "role $STATE_ROLE_ID: $STATE_PERMISSIONS only"
    else
      run gcloud iam roles update "$STATE_ROLE_ID" --project "$PROJECT" --permissions="$STATE_PERMISSIONS" --quiet
    fi
  fi
  ensure_sa "$STATE_SA_NAME" "$STATE_SA" "GitHub Actions: deploy.yml reads whether hangar's infra is up (read-only)"
  bind project "$PROJECT" "$STATE_ROLE" "serviceAccount:$STATE_SA"
  bind sa "$STATE_SA" roles/iam.workloadIdentityUser "$CI_MEMBER"

  narrow projects "$PROJECT" "serviceAccount:$CI_SA serviceAccount:$DEPLOY_SA serviceAccount:$STATE_SA" \
    "roles/container.clusterViewer=serviceAccount:$DEPLOY_SA" "$STATE_ROLE=serviceAccount:$STATE_SA"
  narrow sa "$CI_SA" "$FEDERATED" "roles/iam.workloadIdentityUser=$CI_MEMBER"
  narrow sa "$STATE_SA" "$FEDERATED" "roles/iam.workloadIdentityUser=$CI_MEMBER"
  narrow sa "$DEPLOY_SA" "$FEDERATED" "roles/iam.workloadIdentityUser=$DEPLOY_MEMBER" \
    "roles/iam.workloadIdentityUser=$DEPLOY_MEMBER_IDS"
  # get: the running image and `set image`'s read; list and watch: `rollout status` lists and watches with a
  # metadata.name field selector, which resourceNames allows; patch: `set image`. No secrets, no exec, no other object.
  role=$(cat <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: hangar-deploy, namespace: $NS}
rules:
  - apiGroups: [apps]
    resources: [deployments]
    resourceNames: [hangar]
    verbs: [get, list, watch, patch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: hangar-deploy, namespace: $NS}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: hangar-deploy}
subjects:
  - {kind: User, name: $DEPLOY_SA}
EOF
)
  printf '   + kubectl apply -f - <<EOF\n     %s\n     EOF\n' "${role//$'\n'/$'\n'     }"
  [ "$DRY" = 1 ] || kc apply -f - <<<"$role"

  note "canary (harness-canary.yml, harness-canary-land.yml): no Google Cloud access; it never signs in to GCP."
fi

# ------------------------------------------------------------------------------------------------- 4. GitHub
step "4. GitHub: the $ENVIRONMENT environment (your approval; main and v* tags only), who makes v* tags, the secrets"
if ! skipped github; then
  env_json=$(look gh api "repos/$REPO/environments/$ENVIRONMENT")
  if [ -n "$env_json" ] && json "$REVIEWER_IDS" <<<"$env_json" | grep -qw "$REVIEWER_ID" &&
     [ "$(json "$CUSTOM_POLICIES" <<<"$env_json")" = true ]; then
    note "environment $ENVIRONMENT exists: $REVIEWER approves, custom branch and tag policies"
  else
    # prevent_self_review stays false: you start deploys and approve them, and are the only reviewer.
    body="{\"reviewers\": [{\"type\": \"User\", \"id\": $REVIEWER_ID}], \"prevent_self_review\": false, \"wait_timer\": 0, \"deployment_branch_policy\": {\"protected_branches\": false, \"custom_branch_policies\": true}}"
    printf '   + %s <<< %s\n' "$(show gh api -X PUT "repos/$REPO/environments/$ENVIRONMENT" --input -)" "'$body'"
    [ "$DRY" = 1 ] || gh api -X PUT "repos/$REPO/environments/$ENVIRONMENT" --input - <<<"$body" >/dev/null
  fi
  policies=$(look gh api "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
    --jq '.branch_policies[] | "\(.id) \(.type // "branch") \(.name)"')
  for want in "branch main" "tag v*"; do
    if grep -qxF -- "$want" <<<"$(cut -d' ' -f2- <<<"$policies")"; then
      note "deploys allowed from $want"
    else
      run gh api -X POST "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
        -f "name=${want#* }" -f "type=${want%% *}" --silent
    fi
  done
  while read -r id kind name; do
    [ -n "$id" ] || continue
    case "$kind $name" in "branch main"|"tag v*") continue ;; esac
    note "removing the policy that lets $kind '$name' deploy: only main and v* tags deploy"
    run gh api -X DELETE "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies/$id" --silent
  done <<<"$policies"

  # The environment lets any v* tag deploy, after your approval; the ruleset decides who can make one (RULESET above)
  ruleset_id=$(look gh api "repos/$REPO/rulesets?includes_parents=false" \
    --jq ".[] | select(.name == \"$RULESET_NAME\") | .id" | head -1)
  if [ -n "$ruleset_id" ] &&
     [ "$(look gh api "repos/$REPO/rulesets/$ruleset_id" | json "$RULESET_SAME" || true)" = true ]; then
    note "only repository admins create, move or delete v* tags (ruleset $ruleset_id)"
  else
    if [ -n "$ruleset_id" ]; then
      note "the ruleset '$RULESET_NAME' lets more than repository admins make v* tags, or is off: narrowing it"
      method=PUT path=repos/$REPO/rulesets/$ruleset_id
    else
      method=POST path=repos/$REPO/rulesets
    fi
    printf '   + %s <<< %s\n' "$(show gh api -X "$method" "$path" --input -)" "'$RULESET'"
    if [ "$DRY" = 0 ]; then
      gh api -X "$method" "$path" --input - <<<"$RULESET" >/dev/null ||
        die "couldn't $method the tag ruleset (rulesets need a public repository, or GitHub Pro or Team for a private one)"
    fi
  fi

  note "repository secrets (images.yml runs outside the environment, on every push to main; none of them deploys):"
  set_secret GCP_WORKLOAD_IDENTITY_PROVIDER "$WIF_PROVIDER" public
  set_secret GCP_SERVICE_ACCOUNT "$CI_SA" public
  set_secret HANGAR_REGISTRY "$REGISTRY" public
  set_secret GCP_STATE_SERVICE_ACCOUNT "$STATE_SA" public
  set_secret HANGAR_CLUSTER "$HANGAR_CLUSTER" public
  note "$ENVIRONMENT secrets (released to the deploy job only after your approval):"
  set_secret GCP_DEPLOY_SERVICE_ACCOUNT "$DEPLOY_SA" public --env "$ENVIRONMENT"
  set_secret GKE_CLUSTER "$CLUSTER" public --env "$ENVIRONMENT"
  set_secret GKE_LOCATION "$GKE_LOCATION" public --env "$ENVIRONMENT"
  set_secret HANGAR_URL "$HANGAR_URL" public --env "$ENVIRONMENT"

  # Repository copies of what can deploy would be readable by any branch's workflow (the #95 review)
  repo_secrets=$(look gh secret list --repo "$REPO" --json name --jq '.[].name')
  for s in HANGAR_DEPLOY_TOKEN GCP_DEPLOY_SERVICE_ACCOUNT GKE_CLUSTER GKE_LOCATION; do
    if grep -qx "$s" <<<"$repo_secrets"; then
      note "$s is also a repository secret, which any branch's workflow can read: deleting that copy"
      run gh secret delete "$s" --repo "$REPO"
    fi
  done

  # HANGAR_DEPLOY_TOKEN: hangar has no deploy-only scope. The deploy drains (/api/admin/drain), reads the power
  # state, runs the acceptance checks (sandboxes, sessions, the doctor) and records the deploy (/api/admin/deploys),
  # all admin; `jobs`, `mcp` and `metrics` tokens can't. So: an admin token of its own, revocable by name.
  env_secrets=$(look gh secret list --env "$ENVIRONMENT" --repo "$REPO" --json name --jq '.[].name')
  if grep -qx HANGAR_DEPLOY_TOKEN <<<"$env_secrets" && [ "$ROTATE" = 0 ]; then
    note "HANGAR_DEPLOY_TOKEN is set (--rotate-deploy-token replaces it and revokes the old token)"
  else
    # In a variable: bash 3.2 brace-expands a {"a": 1, "b": 2} written inside "$(…)"
    mint_body="{\"name\": \"$TOKEN_NAME\", \"scopes\": [\"admin\"]}"
    printf '   + %s | gh secret set HANGAR_DEPLOY_TOKEN --env %s --repo %s  # the new token on stdin, not shown\n' \
      "$(show curl -X POST "$HANGAR_URL/api/tokens" -d "$mint_body")" "$ENVIRONMENT" "$REPO"
    if [ "$DRY" = 0 ]; then
      ADMIN_TOKEN=${HANGAR_TOKEN:-}
      if [ -z "$ADMIN_TOKEN" ] && [ "$(saved url)" = "$HANGAR_URL" ]; then ADMIN_TOKEN=$(saved token); fi
      if [ -z "$ADMIN_TOKEN" ] && [ -f "$HOME/.hangar/gke-admin-token" ]; then
        ADMIN_TOKEN=$(tr -d '\n' < "$HOME/.hangar/gke-admin-token")
      fi
      [ -n "$ADMIN_TOKEN" ] || die "no admin token for $HANGAR_URL: hangar login $HANGAR_URL, or set HANGAR_TOKEN"
      listing=$(hangar_api GET /api/tokens) || listing=
      if ! printf '%s' "$listing" | json 'len(d)' >/dev/null; then
        die "couldn't list hangar's tokens at $HANGAR_URL (is the token an admin's?)"
      fi
      minted=$(hangar_api POST /api/tokens "$mint_body") ||
        die "couldn't mint a token at $HANGAR_URL"
      new_id=$(printf '%s' "$minted" | json 'd["id"]') || die "hangar answered the mint without a token id"
      if ! printf '%s' "$minted" | json 'd["token"]' | tr -d '\n' |
           gh secret set HANGAR_DEPLOY_TOKEN --env "$ENVIRONMENT" --repo "$REPO"; then
        hangar_api DELETE "/api/tokens/$new_id" >/dev/null || true
        die "gh secret set HANGAR_DEPLOY_TOKEN failed; the new token ($new_id) was revoked"
      fi
      person=$(printf '%s' "$minted" | json 'd.get("person") or ""')
      minted=
      note "minted $TOKEN_NAME ($new_id), an admin token, into HANGAR_DEPLOY_TOKEN"
      # The previous ones: this name, and the same person (an admin lists everyone's; a teammate's isn't ours)
      for id in $(printf '%s' "$listing" | json '" ".join(t["id"] for t in d if t.get("name") == a[0]
          and (t.get("person") or "") == a[1] and t["id"] != a[2])' "$TOKEN_NAME" "$person" "$new_id"); do
        hangar_api DELETE "/api/tokens/$id" >/dev/null && note "revoked the previous $TOKEN_NAME ($id)"
      done
    fi
  fi

  # The optional acceptance fixtures, only when you exported them for this run
  for v in HANGAR_ACCEPT_TEST_REPO HANGAR_ACCEPT_FIXTURE_REPO; do
    if [ -n "${!v:-}" ]; then
      run gh variable set "$v" --env "$ENVIRONMENT" --repo "$REPO" --body "${!v}"
    fi
  done
  for v in HANGAR_ACCEPT_GITHUB_TOKEN HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET; do
    if [ -n "${!v:-}" ]; then set_secret "$v" "${!v}" secret --env "$ENVIRONMENT"; fi
  done
fi

# ------------------------------------------------------------------------------------------------- 5. vault key
step "5. The vault key in Secret Manager ($VAULT_SECRET; docs/upgrading.md step 1)"
if ! skipped vault; then
  if [ -n "$(look gcloud secrets describe "$VAULT_SECRET" --project "$PROJECT" --format='value(name)')" ]; then
    note "$VAULT_SECRET exists: left as it is (a rotation puts new keys there first: docs/security.md)"
  else
    printf '   + %s | base64 --decode | %s\n' \
      "$(show kubectl -n "$NS" get secret hangar -o 'jsonpath={.data.HANGAR_SECRET_KEY}')" \
      "$(show gcloud secrets create "$VAULT_SECRET" --data-file=- --replication-policy=automatic --project "$PROJECT")"
    if [ "$DRY" = 0 ]; then
      key64=$(kc -n "$NS" get secret hangar -o 'jsonpath={.data.HANGAR_SECRET_KEY}') ||
        die "can't read the hangar Secret in $NS (is the cluster reachable?)"
      [ -n "$key64" ] || die "the hangar Secret has no HANGAR_SECRET_KEY"
      printf '%s' "$key64" |
        python3 -c 'import base64, sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' |
        gcloud secrets create "$VAULT_SECRET" --data-file=- --replication-policy=automatic --project "$PROJECT"
      key64=
    fi
  fi
  note "Give roles/secretmanager.secretAccessor on it only to the operators who rotate keys; neither CI account has it."
fi

# ------------------------------------------------------------------------------------------------- 6. mirrors
step "6. Package mirrors: Artifact Registry remote repositories for PyPI and npm (docs/upgrading.md step 10a)"
if ! skipped mirrors; then
  for spec in "pypi-remote python --remote-python-repo=pypi PyPI" "npm-remote npm --remote-npm-repo=npmjs npm"; do
    read -r name format upstream desc <<<"$spec"
    if [ -n "$(look gcloud artifacts repositories describe "$name" --location="$MIRROR_LOCATION" --project "$PROJECT" --format='value(name)')" ]; then
      note "$name exists"
    else
      run gcloud artifacts repositories create "$name" --location="$MIRROR_LOCATION" --project "$PROJECT" \
        --repository-format="$format" --mode=remote-repository "$upstream" --remote-repo-config-desc="$desc"
    fi
    bind artifacts "$name" roles/artifactregistry.reader "serviceAccount:$HANGAR_SA" \
      --location="$MIRROR_LOCATION"
  done
  note "Go stays on the public proxy (docs/security.md, package mirrors). To use these, when you're ready:"
  note "  hangar config set HANGAR_PACKAGE_MIRRORS '{\"pypi\": \"https://$MIRROR_LOCATION-python.pkg.dev/$PROJECT/pypi-remote\", \"npm\": \"https://$MIRROR_LOCATION-npm.pkg.dev/$PROJECT/npm-remote\"}'"
fi

# ------------------------------------------------------------------------------------------------- 7. by hand
step "7. Quotas, days ahead (docs/upgrading.md step 2): by hand"
note "Quota increases can't be automated reliably: Google reviews each request, and may take days or grant less."
note "The sandbox pool needs N2D_CPUS >= 160 in $REGION (10 x n2d-standard-16), CPUS_ALL_REGIONS to match, and"
note "SSD_TOTAL_GB >= 2,250 in $REGION. What the project has now:"
look gcloud compute regions describe "$REGION" --project "$PROJECT" --flatten=quotas \
  --filter='quotas.metric=(N2D_CPUS PREEMPTIBLE_N2D_CPUS SSD_TOTAL_GB)' \
  --format='table[no-heading](quotas.metric,quotas.usage,quotas.limit)' | sed 's/^/     /'
look gcloud compute project-info describe --project "$PROJECT" --flatten=quotas \
  --filter='quotas.metric=CPUS_ALL_REGIONS' --format='table[no-heading](quotas.metric,quotas.usage,quotas.limit)' |
  sed 's/^/     /'
note "Request them in the console (filter by metric and region, then Edit quotas):"
note "  https://console.cloud.google.com/iam-admin/quotas?project=$PROJECT"
note "or with the Cloud Quotas API (gcloud beta): find the quota ids, then ask for each:"
note "  gcloud beta quotas info list --service=compute.googleapis.com --project=$PROJECT --filter='quotaId~^(N2D-CPUS|SSD-TOTAL-GB|CPUS-ALL-REGIONS)' --format='value(quotaId)'"
note "  gcloud beta quotas preferences create --service=compute.googleapis.com --project=$PROJECT --quota-id=QUOTA_ID --preferred-value=160 --dimensions=region=$REGION --justification='hangar sandbox node pool'"

step "8. The node pool and the power schedule, in the power-down window (docs/upgrading.md step 4): by hand"
note "After power.sh down has finished (01:00 ET) and before the 09:50 power-up, so no sandbox is on the pool:"
note "  terraform -chdir=$TF plan    # expect ~ update in-place on google_container_node_pool.sandboxes; stop on -/+"
note "  terraform -chdir=$TF apply"
note "No CI plan job: $TF keeps its state on your machine (no backend), so CI would plan against nothing."

# ------------------------------------------------------------------------------------------------- 9. check
step "9. Check (read-only)"
FAILED=0
check() {  # WHAT OK-OR-NOT
  if [ "$2" = ok ]; then note "ok    $1"; else note "FAIL  $1${2:+: $2}"; FAILED=1; fi
}
checks=(
  "gcloud iam workload-identity-pools providers describe $PROVIDER --workload-identity-pool=$POOL --location=global --project=$PROJECT --format=value(state,attributeCondition)"
  "gcloud iam workload-identity-pools providers list --workload-identity-pool=$POOL --location=global --project=$PROJECT (no other)"
  "gcloud iam service-accounts get-iam-policy $DEPLOY_SA --project $PROJECT"
  "gcloud iam service-accounts get-iam-policy $CI_SA --project $PROJECT"
  "gcloud iam service-accounts get-iam-policy $STATE_SA --project $PROJECT"
  "gcloud iam roles describe $STATE_ROLE_ID --project $PROJECT ($STATE_PERMISSIONS only)"
  "gcloud projects get-iam-policy $PROJECT (no role for $CI_SA_NAME; clusterViewer for $DEPLOY_SA_NAME; $STATE_ROLE_ID for $STATE_SA_NAME)"
  "kubectl auth can-i patch deployments/hangar -n $NS --as=$DEPLOY_SA (and not secrets, exec or other deployments)"
  "gh api repos/$REPO/environments/$ENVIRONMENT"
  "gh api repos/$REPO/rulesets (only admins make v* tags)"
  "gh secret list --env $ENVIRONMENT --repo $REPO"
  "gh secret list --repo $REPO"
)
if [ "$DRY" = 1 ]; then
  note "dry run: after the changes, this checks:"
  for c in "${checks[@]}"; do note "  $c"; done
else
  p=$(look gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" \
    --location=global --project="$PROJECT" --format=json)
  check "provider $WIF_PROVIDER is active" "$([ "$(json 'd.get("state")' <<<"$p")" = ACTIVE ] && echo ok)"
  check "its condition admits only $REPO's pushes and manual runs, on main and v* tags" \
    "$([ "$(json 'd.get("attributeCondition")' <<<"$p")" = "$CONDITION" ] && echo ok)"
  others=$(look gcloud iam workload-identity-pools providers list --workload-identity-pool="$POOL" \
    --location=global --project="$PROJECT" --format='value(name.basename())' | grep -vxF -- "$PROVIDER" || true)
  check "pool $POOL has no other provider, which could sign in as these accounts" \
    "$(if [ -z "$others" ]; then echo ok; else echo "it has $(paste -sd, - <<<"$others")"; fi)"
  pol=$(look gcloud iam service-accounts get-iam-policy "$DEPLOY_SA" --project "$PROJECT" --format=json)
  check "$DEPLOY_SA: impersonable from the $ENVIRONMENT environment only" \
    "$(has_binding roles/iam.workloadIdentityUser "$DEPLOY_MEMBER" <<<"$pol" &&
       has_binding roles/iam.workloadIdentityUser "$DEPLOY_MEMBER_IDS" <<<"$pol" &&
       [ -z "$(unwanted "$FEDERATED" "roles/iam.workloadIdentityUser=$DEPLOY_MEMBER" \
                 "roles/iam.workloadIdentityUser=$DEPLOY_MEMBER_IDS" <<<"$pol")" ] && echo ok)"
  pol=$(look gcloud iam service-accounts get-iam-policy "$CI_SA" --project "$PROJECT" --format=json)
  check "$CI_SA: impersonable from $REPO's runs the provider admits, and nothing else federated" \
    "$(has_binding roles/iam.workloadIdentityUser "$CI_MEMBER" <<<"$pol" &&
       [ -z "$(unwanted "$FEDERATED" "roles/iam.workloadIdentityUser=$CI_MEMBER" <<<"$pol")" ] && echo ok)"
  pol=$(look gcloud iam service-accounts get-iam-policy "$STATE_SA" --project "$PROJECT" --format=json)
  check "$STATE_SA: impersonable from $REPO's runs the provider admits, and nothing else federated" \
    "$(has_binding roles/iam.workloadIdentityUser "$CI_MEMBER" <<<"$pol" &&
       [ -z "$(unwanted "$FEDERATED" "roles/iam.workloadIdentityUser=$CI_MEMBER" <<<"$pol")" ] && echo ok)"
  check "role $STATE_ROLE_ID grants $STATE_PERMISSIONS and nothing else" \
    "$([ "$(look gcloud iam roles describe "$STATE_ROLE_ID" --project "$PROJECT" --format=json |
          json '",".join(sorted(d.get("includedPermissions", []))) if not d.get("deleted") else ""')" = \
         "$STATE_PERMISSIONS" ] && echo ok)"
  pol=$(look gcloud projects get-iam-policy "$PROJECT" --format=json)
  check "no project role for $CI_SA_NAME, only clusterViewer for $DEPLOY_SA_NAME, only $STATE_ROLE_ID for $STATE_SA_NAME" \
    "$(has_binding roles/container.clusterViewer "serviceAccount:$DEPLOY_SA" <<<"$pol" &&
       has_binding "$STATE_ROLE" "serviceAccount:$STATE_SA" <<<"$pol" &&
       [ -z "$(unwanted "serviceAccount:$CI_SA serviceAccount:$DEPLOY_SA serviceAccount:$STATE_SA" \
                 "roles/container.clusterViewer=serviceAccount:$DEPLOY_SA" \
                 "$STATE_ROLE=serviceAccount:$STATE_SA" <<<"$pol")" ] && echo ok)"
  for verb in get list watch patch; do
    check "$DEPLOY_SA may $verb deployments/hangar in $NS" \
      "$([ "$(look kc auth can-i "$verb" deployments.apps/hangar -n "$NS" --as="$DEPLOY_SA")" = yes ] && echo ok)"
  done
  # An earlier binding (a ClusterRoleBinding, another Role) would show here: the Role above grants none of these
  for denied in "get secrets" "create pods/exec" "patch deployments.apps" "list replicasets.apps"; do
    # shellcheck disable=SC2086 # two words on purpose
    check "$DEPLOY_SA may not ${denied/%deployments.apps/other deployments} in $NS" \
      "$([ "$(look kc auth can-i $denied -n "$NS" --as="$DEPLOY_SA")" = no ] && echo ok)"
  done
  check "only repository admins create, move or delete v* tags" \
    "$(look gh api "repos/$REPO/rulesets?includes_parents=false" --jq ".[] | select(.name == \"$RULESET_NAME\") | .id" |
       head -1 | { read -r id && look gh api "repos/$REPO/rulesets/$id" | json "$RULESET_SAME"; } | grep -qx true &&
       echo ok)"
  e=$(look gh api "repos/$REPO/environments/$ENVIRONMENT")
  check "$ENVIRONMENT: $REVIEWER is a required reviewer" \
    "$(json "$REVIEWER_IDS" <<<"$e" | grep -qw "$REVIEWER_ID" && echo ok)"
  check "$ENVIRONMENT deploys only main and v* tags" \
    "$([ "$(look gh api "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" --jq '[.branch_policies[] | "\(.type // "branch") \(.name)"] | sort | join(",")')" = "branch main,tag v*" ] && echo ok)"
  have=$(look gh secret list --env "$ENVIRONMENT" --repo "$REPO" --json name --jq '.[].name')
  for s in GCP_DEPLOY_SERVICE_ACCOUNT GKE_CLUSTER GKE_LOCATION HANGAR_URL HANGAR_DEPLOY_TOKEN; do
    check "$ENVIRONMENT secret $s" "$(grep -qx "$s" <<<"$have" && echo ok)"
  done
  have=$(look gh secret list --repo "$REPO" --json name --jq '.[].name')
  for s in GCP_WORKLOAD_IDENTITY_PROVIDER GCP_SERVICE_ACCOUNT HANGAR_REGISTRY GCP_STATE_SERVICE_ACCOUNT HANGAR_CLUSTER; do
    check "repository secret $s" "$(grep -qx "$s" <<<"$have" && echo ok)"
  done
  check "no repository copy of HANGAR_DEPLOY_TOKEN" "$(grep -qx HANGAR_DEPLOY_TOKEN <<<"$have" || echo ok)"
fi

# ------------------------------------------------------------------------------------------------- 10. next
step "10. What only you can do (each is in docs/upgrading.md or docs/acceptance.md)"
cat <<EOF
   [ ] Branch protection on $REPO's main: a required approval, dismiss stale approvals, require approval of the
       most recent push, \`contract\` as a required check; and Allow auto-merge (step 11b).
       https://github.com/$REPO/settings/branches   https://github.com/$REPO/settings
   [ ] The GitHub App agents open PRs as (step 9b), installed on the repos agents work on:
       https://github.com/settings/apps/new   then: hangar cred add github-app --harness git --kind github_app
   [ ] The canary's own App (step 11b), as repository secrets:
       gh secret set CANARY_APP_CLIENT_ID --repo $REPO; gh secret set CANARY_APP_PRIVATE_KEY --repo $REPO < key.pem
   [ ] Jira (step 9c): the hangar account's API token, the webhook to $HANGAR_URL/api/hooks/jira and its secret.
       https://id.atlassian.com/manage-profile/security/api-tokens   docs/intake.md
   [ ] Acceptance fixtures (optional; each check skips without its own): a test repo, a fixture repo, a Jira project
       mapped to the test repo (docs/acceptance.md). Then re-run this with them exported, or:
       gh variable set HANGAR_ACCEPT_TEST_REPO --env $ENVIRONMENT --repo $REPO --body OWNER/REPO
       gh secret set HANGAR_ACCEPT_GITHUB_TOKEN --env $ENVIRONMENT --repo $REPO     # prompts; nothing echoed
   [ ] Quotas (7) and the terraform apply in the power-down window (8).

The first deploy (it waits for your approval: Actions -> deploy -> Review deployments):
   gh workflow run deploy.yml --repo $REPO --ref main
   gh run watch --repo $REPO "\$(gh run list --repo $REPO --workflow deploy.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
EOF
if [ "$DRY" = 1 ]; then
  printf '\nDry run: nothing was changed. Run it again without --dry-run to make the changes above.\n'
elif [ "$FAILED" = 1 ]; then
  printf '\nSome checks failed (above). Fix them, or re-run this script, before the first deploy.\n'
  exit 1
fi
