# Configuration and development

How hangar's settings are set and where each value comes from, then how to run hangar's tests and docs while you
work on it.

## Configuration

Everything is set through `HANGAR_*` settings (see `src/hangar/config.py`), which come from two places:

- **The environment.** This includes `.env` and, on GKE, the `hangar` and `hangar-site` config maps. It is read at
  startup, and it wins.
- **hangar's database.** Edit it with `hangar config set KEY VALUE`, `get`, `unset` and `list` (admin tokens only).
  Values are checked against each setting's type and survive redeploys. Most apply at once; `hangar config set` tells
  you when one needs a restart. Secrets are refused: they go in the vault (`hangar cred add`), and settings name the
  credential.

`hangar config list` shows each value and where it came from. The common settings:

| Variable | Purpose |
| --- | --- |
| `HANGAR_ADMIN_TOKEN` | Admin bearer token. If unset, a bootstrap token is generated once. |
| `HANGAR_SECRET_KEY` | Fernet key for the credential vault. If unset, a key file is created in the data dir. |
| `HANGAR_DATA_DIR` | State: SQLite DB, key, workspace snapshots. Default `~/.hangar`. |
| `HANGAR_ENV_DIRS` | JSON list of spec directories. Default `["envs"]`. |
| `HANGAR_DEFAULT_PROVIDER` / `HANGAR_ENABLED_PROVIDERS` | `docker`, `kubernetes`, `e2b` |
| `HANGAR_DOCKER_RUNTIME` | `runsc` (gVisor) or `kata` on Linux hosts |
| `HANGAR_K8S_RUNTIME_CLASS` | `gvisor` (default), `kata-qemu-runtime-rs` (Kata microVMs), or empty for runc |
| `HANGAR_REGISTRY` | Where cluster/E2B images are pushed, e.g. Artifact Registry |
| `HANGAR_EXTRA_CA_BUNDLE` | PEM file trusted inside every image, for corporate TLS-inspecting proxies |
| `HANGAR_DEFAULT_TTL_MINUTES` / `HANGAR_MAX_SANDBOXES` | Fleet guardrails |
| `HANGAR_E2B_GCP_SERVICE_ACCOUNT_JSON` / `HANGAR_E2B_AWS_*` / `HANGAR_E2B_REGISTRY_USERNAME` | How E2B pulls images from a private `HANGAR_REGISTRY` |

`hangar gc` lists containers left behind by crashed creates or other state directories, and `hangar gc --yes` removes
them.

## Development

```bash
uv sync --all-extras
uv run pytest -n 8                             # unit + API tests (fake provider), in parallel
scripts/ci/select_tests.py --base origin/main  # just the tests your branch can break (docs/ci.md)
HANGAR_E2E=1 uv run pytest tests/test_e2e_docker.py   # real Docker, driven over MCP
uv run ruff check src tests
uv run hangar serve --reload
```

What CI runs on a pull request and on `main`, and how a red main hands its bisect to a hangar job, is on
[CI](../ci.md).

The docs site builds the way `.github/workflows/docs.yml` builds it (Zensical, from `mkdocs.yml`):

```bash
pip install zensical==0.0.71
zensical build --strict    # into site/; a broken link or anchor fails it, as in CI
zensical serve             # http://localhost:8000, rebuilt on save
python tests/test_editorial.py   # each page's words of prose, and its long paragraphs and sentences
```

`tests/test_editorial.py` holds the docs to the suite's editorial rule. A paragraph is at most 60 words, a sentence
at most 25, and a page at most 700 words of prose. Pages written before the rule are listed there with a reason and
ceilings that only go down.

Code map:

- `fleet.py` (lifecycle), `envspec.py` (specs to Dockerfiles);
- `broker.py` + `vault.py` (credentials), `harnesses.py` (per-agent install, auth, run);
- `mcp_server.py`, `app.py` (REST, websockets, dashboard);
- `providers/` (docker, kubernetes, e2b), `ui/` (no-build dashboard).
