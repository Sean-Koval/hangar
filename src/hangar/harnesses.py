"""Agent harness profiles: how each coding-agent CLI is packaged, authenticated, configured and run headless.

A harness is installed into its own prefix, `/opt/hangar/harness/<name>`, by a self-contained script. That
prefix is shipped two ways from the same build stage:

* as a **harness pack**: a small OCI image (`FROM scratch`) mounted read-only into the sandbox at start
  (Kubernetes image volumes, Docker `--mount type=image`). Environment images stay harness-free, packs are
  pulled once per node and shared by every environment.
* **baked**: copied into the environment image, for providers that cannot mount images (E2B, Docker without
  the containerd image store). Only the harness the sandbox was created for is baked.

Everything per-sandbox (credentials, config, MCP servers, PATH) is applied when a sandbox is claimed, for the
one harness selected, so an environment can allow several harnesses without carrying their secrets around.

Each profile is data, so adding a harness means adding an entry here; no provider changes are needed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from hangar.models import CredentialKind

PACK_ROOT = "/opt/hangar/harness"
# Build stage for every pack: Debian 12 + node 22 + npm + curl. Binaries built here run on any glibc >= 2.36
# distro (Ubuntu 24.04, Debian 12+); codex is a static musl binary and runs anywhere.
PACK_BUILDER = "mirror.gcr.io/library/node:22-bookworm"


class CredentialSlot(BaseModel):
    """How one kind of credential materializes inside a sandbox."""

    kind: CredentialKind
    env_var: str | None = None
    file_path: str | None = None  # relative to the sandbox user's $HOME when not absolute
    file_mode: str = "600"
    exclusive: bool = False  # rotating refresh tokens: lease to a single sandbox, write back on release
    post_inject: str | None = None  # shell run (as the sandbox user) after the secret is in place


class LoginFlow(BaseModel):
    """An interactive login run inside a short-lived auth sandbox, driven from the UI terminal."""

    command: str
    captures: CredentialKind
    # For token flows: regex that pulls the token out of terminal output.
    token_regex: str | None = None
    # For file flows: file to read back after the login completes.
    capture_file: str | None = None
    notes: str = ""


McpFormat = Literal["claude", "codex", "pi", "antigravity"]


class Harness(BaseModel):
    name: str
    title: str
    agent: bool = True  # False for helpers such as git that are not a coding agent
    version: str = "latest"  # pinned into the pack tag; set from harnesses.lock.yaml for every harness with `install`
    # --- images: the lock's other fields for this harness (checksums, build ids), exported to `install` as
    # upper-case variables: `sha256: {x86_64: ...}` becomes $SHA256_X86_64.
    pin: dict[str, str] = Field(default_factory=dict)
    # Shell run as root in the pack builder with $PREFIX (= PACK_ROOT/<name>), $VERSION and a throwaway $HOME.
    # Must leave runnable entrypoints in $PREFIX/bin and nothing outside $PREFIX that they depend on.
    install: str = ""
    env: dict[str, str] = Field(default_factory=dict)  # non-secret env set for the selected harness
    credentials: list[CredentialSlot] = Field(default_factory=list)
    logins: list[LoginFlow] = Field(default_factory=list)
    # Headless invocation. $HANGAR_PROMPT_FILE holds the prompt; $HANGAR_MODEL is set when a model was chosen.
    run: str = "true"
    skills_dir: str | None = None  # relative to $HOME
    instructions_file: str | None = None  # global CLAUDE.md / AGENTS.md path relative to $HOME
    # JSON files (relative to $HOME) deep-merged at claim time, e.g. to skip first-run wizards.
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mcp_format: McpFormat | None = None
    verify: str = "true"
    deprecated: str | None = None  # reason, shown in listings; still usable
    # Sessions (spec 0010). Directories (relative to $HOME) where the harness keeps resumable conversations;
    # `resume` says how a conversation id is obtained: "preset" (hangar picks it and passes
    # $HANGAR_SESSION_NEW), "capture" (the harness assigns it; `session_id_regex` finds it in the run log),
    # or None (no native resume: sessions fall back to a summary handoff). The run command reads
    # $HANGAR_SESSION_NEW / $HANGAR_SESSION_RESUME. `resume_hint` is the interactive command for engineers.
    session_dirs: list[str] = Field(default_factory=list)
    resume: Literal["preset", "capture"] | None = None
    session_id_regex: str | None = None
    resume_hint: str | None = None
    # --- workstream D: forks/best-of-N. The CLI can branch a conversation into a new one: the run command then
    # reads $HANGAR_SESSION_FORK (the conversation to fork), plus $HANGAR_SESSION_NEW for "preset" harnesses (the
    # new id). Without it, a forked session starts fresh with a summary handoff.
    fork: bool = False
    # --- workstream steer: live turns. `live` names the structured protocol hangar's driver speaks with this CLI
    # (driver.py); `live_run` is the command it starts (shell words; the driver adds the permission flags and, for
    # codex, opens or resumes the conversation itself). Without them, turns run `run` one-shot.
    live: Literal["claude", "codex"] | None = None
    live_run: str = ""
    # --- overnight/cache: env that asks the CLI for each prompt-cache TTL it supports ("5m", "1h", "off"; cache.py).
    # A TTL missing here is one the CLI can't be asked for; the provider's own caching applies.
    cache_env: dict[str, dict[str, str]] = Field(default_factory=dict)
    # --- images (harness P2): directories in the prefix appended to PATH for the selected harness (lowest priority),
    # set at claim time with the rest of its environment rather than by a wrapper in the pack.
    path_extra: list[str] = Field(default_factory=list)

    @property
    def prefix(self) -> str:
        return f"{PACK_ROOT}/{self.name}"


# --- images: the locked release's binary straight from the vendor's release bucket (what install.sh would fetch for
# that version), checked against the lock's sha256, rather than install.sh, which starts from whatever is latest.
_CLAUDE_INSTALL = r"""
case "$(uname -m)" in
  x86_64) P=linux-x64 SUM=$SHA256_X86_64 ;;
  aarch64) P=linux-arm64 SUM=$SHA256_AARCH64 ;;
  *) echo "no Claude Code build for $(uname -m)" >&2; exit 1 ;;
esac
curl -fsSL "https://downloads.claude.ai/claude-code-releases/$VERSION/$P/claude" -o /tmp/claude
echo "$SUM  /tmp/claude" | sha256sum -c -
install -D -m 755 /tmp/claude "$PREFIX/bin/claude"
rm -f /tmp/claude
"""

# The npm package is a node launcher around a static musl binary; ship the binary, its helper binaries and
# resources. --- images (harness P2): the pack holds the vendor's files only. bin/codex links to the binary, and the
# bundled ripgrep the launcher would have put on PATH comes from `path_extra`, which hangar applies at claim time: a
# change to how hangar starts codex ships with hangar, not as a new pack.
_CODEX_INSTALL = r"""
npm install -g --prefix /tmp/codex-npm "@openai/codex@$VERSION"
V=$(find /tmp/codex-npm -type d -path "*/vendor/$(uname -m)-unknown-linux-musl" | head -1)
test -x "$V/bin/codex"
mkdir -p "$PREFIX/bin" "$PREFIX/libexec"
cp -a "$V/." "$PREFIX/libexec/"
ln -s ../libexec/bin/codex "$PREFIX/bin/codex"
"""

# pi needs node >= 22.19. Bundle the builder's node so pi never depends on (or changes) the project's node.
# --- images (harness P2): the launcher names its prefix rather than finding it with dirname and readlink, which a
# hardened turn would look up on the agent's PATH (harness B5): one fixed line, nothing to change in it later.
_PI_INSTALL = r"""
mkdir -p "$PREFIX/bin" "$PREFIX/node/bin"
install -m 755 "$(command -v node)" "$PREFIX/node/bin/node"
npm install -g --prefix "$PREFIX/lib" "@earendil-works/pi-coding-agent@$VERSION"
PKG="$PREFIX/lib/lib/node_modules/@earendil-works/pi-coding-agent"
ENTRY=$(node -p "require('$PKG/package.json').bin.pi")
cat > "$PREFIX/bin/pi" <<SH
#!/bin/sh
exec "$PREFIX/node/bin/node" "$PKG/$ENTRY" "\$@"
SH
chmod 755 "$PREFIX/bin/pi"
"""

# --- images: agy's install.sh takes no version (it installs the updater's current one), so fetch the locked build's
# archive, the one install.sh downloads for that version, and check it against the lock's sha512.
_AGY_INSTALL = r"""
case "$(uname -m)" in
  x86_64) P=linux-x64/cli_linux_x64 SUM=$SHA512_X86_64 ;;
  aarch64) P=linux-arm/cli_linux_arm64 SUM=$SHA512_AARCH64 ;;
  *) echo "no Antigravity CLI build for $(uname -m)" >&2; exit 1 ;;
esac
curl -fsSL "https://storage.googleapis.com/antigravity-public/antigravity-cli/$VERSION-$BUILD/$P.tar.gz" -o /tmp/agy.tgz
echo "$SUM  /tmp/agy.tgz" | sha512sum -c -
tar -xzf /tmp/agy.tgz -C /tmp antigravity
install -D -m 755 /tmp/antigravity "$PREFIX/bin/agy"
rm -f /tmp/agy.tgz /tmp/antigravity
"""

# A GCP service-account key, billed to the key's own project. The project comes from the key file;
# GOOGLE_CLOUD_LOCATION expands when ~/.hangar/env is sourced. `extra` adds exports (with %s = project id).
def _gcp_sa_slot(extra: str = "") -> CredentialSlot:
    script = (
        "P=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser(\"~/.config/hangar/gcp-sa.json\")))"
        "[\"project_id\"])') && "
        "printf 'export GOOGLE_APPLICATION_CREDENTIALS=%s GOOGLE_CLOUD_PROJECT=%s "
        "GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION:-global}\\n' "
        "\"$HOME/.config/hangar/gcp-sa.json\" \"$P\" >> ~/.hangar/env"
    )
    if extra:
        script += f" && printf 'export {extra}\\n' \"$P\" >> ~/.hangar/env"
    return CredentialSlot(kind=CredentialKind.GENERIC, file_path=".config/hangar/gcp-sa.json", post_inject=script)


HARNESSES: dict[str, Harness] = {
    "claude-code": Harness(
        name="claude-code",
        title="Claude Code",
        install=_CLAUDE_INSTALL,
        env={"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        credentials=[
            CredentialSlot(kind=CredentialKind.API_KEY, env_var="ANTHROPIC_API_KEY"),
            # Long-lived (1y) subscription token from `claude setup-token`. Model requests only.
            CredentialSlot(kind=CredentialKind.OAUTH_TOKEN, env_var="CLAUDE_CODE_OAUTH_TOKEN"),
        ],
        logins=[
            LoginFlow(
                command="claude setup-token",
                captures=CredentialKind.OAUTH_TOKEN,
                token_regex=r"(sk-ant-oat[0-9A-Za-z_\-]{20,})",
                notes="Opens a browser URL; paste the code back into the terminal. Requires Pro/Max/Team/Enterprise.",
            )
        ],
        run=(
            'claude -p "$(cat "$HANGAR_PROMPT_FILE")" --dangerously-skip-permissions '
            '--output-format stream-json --verbose ${HANGAR_MODEL:+--model "$HANGAR_MODEL"} '
            '${HANGAR_SESSION_NEW:+--session-id "$HANGAR_SESSION_NEW"} '
            '${HANGAR_SESSION_RESUME:+--resume "$HANGAR_SESSION_RESUME"} '
            '${HANGAR_SESSION_FORK:+--resume "$HANGAR_SESSION_FORK" --fork-session} '
            # --- overnight/autofix: the job's verify as a Stop hook, inline (verifyloop.py)
            '${HANGAR_CLAUDE_SETTINGS:+--settings "$HANGAR_CLAUDE_SETTINGS"}'
        ),
        # Verified with 2.1.296: the prompt and later messages go on stdin (stream-json), see driver.Claude.
        live="claude",
        live_run=(
            'claude -p --input-format stream-json --output-format stream-json --verbose '
            '${HANGAR_MODEL:+--model "$HANGAR_MODEL"} '
            '${HANGAR_SESSION_NEW:+--session-id "$HANGAR_SESSION_NEW"} '
            '${HANGAR_SESSION_RESUME:+--resume "$HANGAR_SESSION_RESUME"} '
            '${HANGAR_SESSION_FORK:+--resume "$HANGAR_SESSION_FORK" --fork-session} '
            '${HANGAR_CLAUDE_SETTINGS:+--settings "$HANGAR_CLAUDE_SETTINGS"}'  # --- overnight/autofix
        ),
        # --- overnight/cache: verified with 2.1.296 against a scripted Messages API: by default an API key gets
        # cache_control without a ttl (5 min); CLAUDE_CODE_PROMPT_CACHE_TTL=1h sends ttl "1h" on the main
        # conversation (subagents keep 5 min) and DISABLE_PROMPT_CACHING=1 sends no cache_control at all.
        cache_env={"5m": {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}, "1h": {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"},
                   "off": {"DISABLE_PROMPT_CACHING": "1"}},
        session_dirs=[".claude/projects"],
        resume="preset",
        # Verified with 2.1.296: --session-id may be combined with --resume only together with --fork-session.
        fork=True,
        resume_hint="claude --resume {id}",
        skills_dir=".claude/skills",
        instructions_file=".claude/CLAUDE.md",
        # Skip the first-run wizard and the bypass-permissions warning in interactive sessions.
        config={".claude.json": {"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True}},
        mcp_format="claude",
        verify="claude --version",
    ),
    "codex": Harness(
        name="codex",
        title="OpenAI Codex CLI",
        install=_CODEX_INSTALL,
        credentials=[
            CredentialSlot(
                kind=CredentialKind.API_KEY,
                env_var="CODEX_API_KEY",
                post_inject='printenv CODEX_API_KEY | codex login --with-api-key >/dev/null 2>&1 || true',
            ),
            # ChatGPT login. Refresh tokens rotate, so one auth.json may only be live in one place.
            CredentialSlot(kind=CredentialKind.AUTH_FILE, file_path=".codex/auth.json", exclusive=True),
        ],
        logins=[
            LoginFlow(
                command="codex login --device-auth",
                captures=CredentialKind.AUTH_FILE,
                capture_file=".codex/auth.json",
                notes="Device-code flow: open the URL on any device and enter the code.",
            )
        ],
        # The sandbox is the isolation boundary, so codex's own (Landlock/seccomp) sandbox is bypassed;
        # it cannot work under gVisor anyway.
        run=(
            'codex exec ${HANGAR_SESSION_RESUME:+resume "$HANGAR_SESSION_RESUME"} '
            '${HANGAR_SESSION_FORK:+fork "$HANGAR_SESSION_FORK"} '
            "--json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "
            '${HANGAR_MODEL:+-m "$HANGAR_MODEL"} "$(cat "$HANGAR_PROMPT_FILE")"'
        ),
        # Verified with 0.162.1: thread/start, thread/resume and thread/fork from $HANGAR_SESSION_*, see driver.Codex.
        live="codex",
        live_run="codex app-server",
        # --- overnight/cache: no cache_env. Verified with 0.162.1: requests carry prompt_cache_key = the thread id
        # and no prompt_cache_retention / prompt_cache_options, and there is no flag or config key for them. OpenAI
        # caches automatically (GPT-5.6+: at least 30 min).
        path_extra=["libexec/codex-path"],  # the bundled rg (--- images)
        session_dirs=[".codex/sessions"],
        resume="capture",
        # Verified with 0.162.1: `codex exec fork <id>` starts a new thread (its thread_id is in thread.started)
        # whose rollout records forked_from_id; the parent's rollout must stay on disk (it is in the checkpoint).
        fork=True,
        session_id_regex=r'"thread_id"\s*:\s*"([0-9A-Za-z-]{8,})"',
        resume_hint="codex resume {id}",
        skills_dir=".codex/skills",
        instructions_file=".codex/AGENTS.md",
        mcp_format="codex",
        verify="codex --version",
    ),
    "pi": Harness(
        name="pi",
        title="pi coding agent",
        install=_PI_INSTALL,
        credentials=[
            # Any provider key works: pass env_var (OPENAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY...)
            # when storing the credential. OpenRouter is the default because one key reaches every model.
            CredentialSlot(kind=CredentialKind.API_KEY, env_var="OPENROUTER_API_KEY"),
            # `pi /login` subscriptions (ChatGPT, Claude...). Tokens refresh in place, so lease exclusively.
            CredentialSlot(kind=CredentialKind.AUTH_FILE, file_path=".pi/agent/auth.json", exclusive=True),
            # Vertex AI on your own GCP project (`--model google-vertex/<id>`).
            # Vertex AI (`--model google-vertex/<id>`), verified on GKE.
            _gcp_sa_slot(),
        ],
        run=(
            'pi -p --mode json --approve ${HANGAR_MODEL:+--model "$HANGAR_MODEL"} '
            '${HANGAR_SESSION_NEW:+--session-id "$HANGAR_SESSION_NEW"} '
            '${HANGAR_SESSION_RESUME:+--session-id "$HANGAR_SESSION_RESUME"} '
            '${HANGAR_SESSION_FORK:+--fork "$HANGAR_SESSION_FORK"} '
            '"$(cat "$HANGAR_PROMPT_FILE")"'
        ),
        # --- overnight/cache: verified with 1.2.0 (1.1.0 documents the same variable): PI_CACHE_RETENTION=long sends
        # ttl "1h" to Anthropic models (24h retention or a 30m TTL on OpenAI models that have one). pi has no
        # environment switch to turn caching off, so `off` leaves it at its default.
        cache_env={"5m": {"PI_CACHE_RETENTION": "short"}, "1h": {"PI_CACHE_RETENTION": "long"}},
        session_dirs=[".pi/agent/sessions"],
        resume="preset",
        fork=True,  # verified with 1.1.0 and 1.2.0: --fork <id> --session-id <new id>
        resume_hint="pi --session-id {id}",
        skills_dir=".pi/agent/skills",
        instructions_file=".pi/agent/AGENTS.md",
        mcp_format="pi",
        verify="pi --version",
    ),
    "antigravity": Harness(
        name="antigravity",
        title="Google Antigravity CLI (agy)",
        install=_AGY_INSTALL,
        credentials=[
            CredentialSlot(
                kind=CredentialKind.API_KEY,
                env_var="GEMINI_API_KEY",
                # agy only honours GEMINI_API_KEY when modelProvider=gemini is set.
                post_inject=(
                    "mkdir -p ~/.gemini/antigravity-cli && python3 - <<'PY'\n"
                    "import json, pathlib\n"
                    "p = pathlib.Path.home() / '.gemini/antigravity-cli/settings.json'\n"
                    "d = json.loads(p.read_text()) if p.exists() else {}\n"
                    "d['modelProvider'] = 'gemini'\n"
                    "p.write_text(json.dumps(d, indent=2))\n"
                    "PY"
                ),
            ),
            # Google Cloud project billing (Antigravity enterprise "ADC" mode), verified on GKE with agy 1.3.3:
            # AGY_ADC_AUTH=true + a service-account key; the project comes from GOOGLE_CLOUD_QUOTA_PROJECT.
            # Needs aiplatform.googleapis.com enabled and Vertex access (roles/aiplatform.user) for the account.
            _gcp_sa_slot("AGY_ADC_AUTH=true GOOGLE_CLOUD_QUOTA_PROJECT=%s"),
        ],
        # No login flow: agy's interactive Google sign-in lives in the OS keyring (Secret Service over D-Bus),
        # which sandboxes lack. Use the service-account key above, or a GEMINI_API_KEY (Gemini API billing).
        run=(
            'agy -p "$(cat "$HANGAR_PROMPT_FILE")" --output-format stream-json '
            '--dangerously-skip-permissions ${HANGAR_MODEL:+--model "$HANGAR_MODEL"} '
            '${HANGAR_SESSION_RESUME:+--conversation "$HANGAR_SESSION_RESUME"}'
        ),
        # Verified with agy 1.3.3: stream-json's first event is {"event":"init","conversation_id":...}; each
        # conversation is a SQLite file under conversations/; an unknown --conversation id silently starts a new
        # one, which the session's post-turn check (is <id> on disk?) catches.
        session_dirs=[".gemini/antigravity-cli/conversations", ".gemini/antigravity-cli/conversation_summaries.db"],
        resume="capture",
        session_id_regex=r'"conversation_id"\s*:\s*"([0-9a-f-]{36})"',
        resume_hint="agy --conversation {id}",
        # No fork flag in agy 1.3.3 (only --conversation/--continue), so forked sessions get a summary handoff.
        fork=False,
        # --- overnight/cache: no cache_env: agy 1.3.3 has no cache flag, setting or variable (Gemini caches
        # implicitly).
        skills_dir=".gemini/skills",
        instructions_file=".gemini/GEMINI.md",
        mcp_format="antigravity",
        verify="agy --version",
    ),
    "gemini": Harness(
        name="gemini",
        title="Gemini CLI",
        deprecated="Google is retiring Gemini CLI for AI Pro/Ultra accounts in favour of Antigravity (agy)",
        install=(
            'mkdir -p "$PREFIX/bin" "$PREFIX/node/bin" && install -m 755 "$(command -v node)" "$PREFIX/node/bin/node"\n'
            'npm install -g --prefix "$PREFIX/lib" "@google/gemini-cli@$VERSION"\n'
            "printf '#!/bin/sh\\nexec \"%s/node/bin/node\" \"%s/lib/bin/gemini\" \"$@\"\\n' \"$PREFIX\" \"$PREFIX\" "
            "> \"$PREFIX/bin/gemini\"\n"
            'chmod 755 "$PREFIX/bin/gemini"'
        ),
        env={"NO_BROWSER": "true"},
        credentials=[
            CredentialSlot(kind=CredentialKind.API_KEY, env_var="GEMINI_API_KEY"),
            CredentialSlot(kind=CredentialKind.AUTH_FILE, file_path=".gemini/oauth_creds.json", exclusive=True),
        ],
        logins=[
            LoginFlow(
                command="gemini",
                captures=CredentialKind.AUTH_FILE,
                capture_file=".gemini/oauth_creds.json",
                notes="Choose 'Login with Google'; NO_BROWSER prints a URL and asks for the code.",
            )
        ],
        run='gemini -p "$(cat "$HANGAR_PROMPT_FILE")" --yolo ${HANGAR_MODEL:+-m "$HANGAR_MODEL"}',
        instructions_file=".gemini/GEMINI.md",
        verify="gemini --version",
    ),
    "git": Harness(
        name="git",
        title="Git / GitHub",
        agent=False,
        credentials=[
            CredentialSlot(
                kind=CredentialKind.GIT_TOKEN,
                env_var="GH_TOKEN",
                post_inject=(
                    'git config --global credential.helper store && '
                    'printf "https://x-access-token:%s@github.com\\n" "$GH_TOKEN" > ~/.git-credentials && '
                    "chmod 600 ~/.git-credentials && "
                    # Commit as the token's GitHub user (noreply address) unless an identity is already set.
                    '{ git config --global user.email >/dev/null || { '
                    'L=$(gh api user --jq .login 2>/dev/null) && I=$(gh api user --jq .id 2>/dev/null) && '
                    'git config --global user.name "$L" && '
                    'git config --global user.email "$I+$L@users.noreply.github.com"; }; } || true'
                ),
            ),
            CredentialSlot(
                kind=CredentialKind.SSH_KEY,
                file_path=".ssh/id_ed25519",
                post_inject="ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null || true",
            ),
        ],
    ),
}


# --- images: the harness lock (harnesses.lock.yaml): every harness hangar installs is pinned there, nowhere else.
LOCK_FILE = Path(__file__).with_name("harnesses.lock.yaml")
_PIN_VALUE = re.compile(r"[A-Za-z0-9._-]+")  # exported unquoted-safe into the install script


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}_{k}" if prefix else str(k), v, out)
        return
    if not _PIN_VALUE.fullmatch(str(value)):
        raise ValueError(f"{LOCK_FILE.name}: {prefix} = {value!r} is not a plain version, checksum or id")
    key = re.sub(r"[^A-Z0-9_]", "_", prefix.upper())
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in ("PREFIX", "VERSION", "HOME", "PATH"):
        raise ValueError(f"{LOCK_FILE.name}: {prefix!r} can't be an install variable")
    out[key] = str(value)


def apply_lock(harnesses: dict[str, Harness], lock: dict[str, Any]) -> None:
    """Pin each harness to its lock entry. A harness that installs something must have one, so nothing floats."""
    if unknown := sorted(set(lock) - set(harnesses)):
        raise ValueError(f"{LOCK_FILE.name} names unknown harnesses: {', '.join(unknown)}")
    for name, h in harnesses.items():
        entry = dict(lock.get(name) or {})
        if not h.install:
            continue
        version = str(entry.pop("version", "") or "")
        if not _PIN_VALUE.fullmatch(version) or version == "latest":
            raise ValueError(f"{LOCK_FILE.name}: {name} needs an exact `version`")
        pin: dict[str, str] = {}
        _flatten("", entry, pin)
        h.version, h.pin = version, pin


apply_lock(HARNESSES, yaml.safe_load(LOCK_FILE.read_text()) or {})


def get_harness(name: str) -> Harness:
    try:
        return HARNESSES[name]
    except KeyError as e:
        raise ValueError(f"unknown harness '{name}'. Known: {', '.join(HARNESSES)}") from e


def slot_for(harness: str, kind: CredentialKind) -> CredentialSlot | None:
    h = HARNESSES.get(harness)
    if not h:
        return None
    return next((s for s in h.credentials if s.kind == kind), None)


# --------------------------------------------------------------------------- packs
# --- images: what the installed CLI says it is (`verify`, its --version). The pack build prints it on a marker line
# (VERSION_MARKER), fails when it doesn't name the locked version, and keeps it in VERSION_FILE: its own last layer,
# so hangar can read it from the registry without pulling the pack, and mounted at <prefix>/.hangar-version.
VERSION_MARKER = "hangar-harness-version: "
VERSION_FILE = ".hangar-version"


def pack_script(h: Harness) -> str:
    pins = "".join(f" {k}={v}" for k, v in sorted(h.pin.items()))
    return (
        "#!/bin/bash\nset -euxo pipefail\n"
        f"export PREFIX={h.prefix} VERSION={json.dumps(h.version)} HOME=/tmp/pack-home{pins}\n"
        'mkdir -p "$HOME" "$PREFIX"\n'
        f"{h.install.strip()}\n"
        f'OUT=$(PATH="$PREFIX/bin:$PATH" {h.verify} 2>&1)\n'
        "{ set +x; } 2>/dev/null\n"
        # the line naming the version (a runtime may warn first), else the first line, for the error
        'INSTALLED=$(printf \'%s\\n\' "$OUT" | grep -F -m1 -- "$VERSION" || true)\n'
        "if [ -z \"$INSTALLED\" ]; then echo \"installed '${OUT%%$'\\n'*}', but the lock pins $VERSION\" >&2; "
        "exit 1; fi\n"
        f"printf '%s\\n' \"$INSTALLED\" > /{VERSION_FILE}\n"
        f"echo \"{VERSION_MARKER}$INSTALLED\"\n"
        "set -x\n"
        'rm -rf "$HOME" /tmp/*-npm\n'
    )


def pack_stage(h: Harness) -> str:
    """Dockerfile stage that installs the harness into its prefix. Shared by packs and baked images."""
    return (f"FROM {PACK_BUILDER} AS pack-{h.name}\n"
            f"COPY packs/{h.name}.sh /tmp/pack.sh\n"
            "RUN bash /tmp/pack.sh\n")


def pack_dockerfile(h: Harness) -> str:
    # The image's root *is* the prefix: it is mounted at PACK_ROOT/<name>. The version file is a layer of its own,
    # the last one (--- images).
    return (pack_stage(h) + f"FROM scratch\nCOPY --from=pack-{h.name} {h.prefix}/ /\n"
            f"COPY --from=pack-{h.name} /{VERSION_FILE} /{VERSION_FILE}\n")


def version_from_log(lines: list[str] | str) -> str | None:
    """The installed version a pack build printed (VERSION_MARKER), from its build log."""
    for line in (lines.splitlines() if isinstance(lines, str) else lines):
        if (i := line.find(VERSION_MARKER)) >= 0 and "echo" not in line[:i]:
            return line[i + len(VERSION_MARKER):].strip()[:200] or None
    return None


def pack_digest(h: Harness) -> str:
    import hashlib

    return hashlib.sha256((pack_dockerfile(h) + pack_script(h)).encode()).hexdigest()[:12]


def pack_tag(h: Harness, prefix: str, registry: str | None = None) -> str:
    repo = f"{registry.rstrip('/')}/harness-{h.name}" if registry else f"{prefix}/harness-{h.name}"
    ver = re.sub(r"[^A-Za-z0-9_.-]", "", h.version) or "latest"
    return f"{repo}:{ver}-{pack_digest(h)}"


# --------------------------------------------------------------------------- MCP
class McpServer(BaseModel):
    """One MCP server, written in each harness's own config format at claim time.

    String values may reference `${VAR}`: an env var injected into the sandbox (a credential's env var or an
    entry of the environment's `secrets:` map). References are resolved when the config is written.
    """

    url: str | None = None  # streamable HTTP
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None  # stdio
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    harnesses: list[str] | None = None  # default: every harness

    @model_validator(mode="after")
    def _one_transport(self) -> McpServer:
        if bool(self.url) == bool(self.command):
            raise ValueError("an MCP server needs exactly one of `url` (HTTP) or `command` (stdio)")
        return self


_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_refs(value: Any, env: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            if m.group(1) in env:
                return env[m.group(1)]
            missing.add(m.group(1))
            return m.group(0)
        return _REF.sub(sub, value)
    if isinstance(value, list):
        return [resolve_refs(v, env, missing) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, env, missing) for k, v in value.items()}
    return value


def _toml_str(s: str) -> str:
    return json.dumps(s)  # JSON string escapes are valid TOML basic strings


def mcp_config(fmt: McpFormat, servers: dict[str, McpServer]) -> tuple[str, Any]:
    """(path relative to $HOME, content) for one harness. Content is a dict to deep-merge into a JSON file,
    or for codex a TOML fragment that replaces hangar's managed block in config.toml."""
    if fmt == "codex":
        out: list[str] = []
        for name, s in servers.items():
            out.append(f"[mcp_servers.{_toml_str(name)}]")
            if s.url:
                out.append(f"url = {_toml_str(s.url)}")
                if s.headers:
                    out.append("http_headers = { " + ", ".join(
                        f"{_toml_str(k)} = {_toml_str(v)}" for k, v in s.headers.items()) + " }")
            else:
                out.append(f"command = {_toml_str(s.command or '')}")
                out.append("args = [" + ", ".join(map(_toml_str, s.args)) + "]")
                if s.env:
                    out.append("env = { " + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}"
                                                      for k, v in s.env.items()) + " }")
            out.append("")
        return ".codex/config.toml", "\n".join(out)
    entries: dict[str, Any] = {}
    for name, s in servers.items():
        if s.url:
            key = "serverUrl" if fmt == "antigravity" else "url"
            e: dict[str, Any] = {key: s.url}
            if fmt == "claude":
                e = {"type": "http", **e}
            if s.headers:
                e["headers"] = dict(s.headers)
        else:
            e = {"command": s.command, "args": list(s.args)}
            if fmt == "claude":
                e = {"type": "stdio", **e}
            if s.env:
                e["env"] = dict(s.env)
        entries[name] = e
    path = {"claude": ".claude.json", "pi": ".pi/agent/mcp.json",
            "antigravity": ".gemini/config/mcp_config.json"}[fmt]
    return path, {"mcpServers": entries}


CODEX_BLOCK = ("# >>> hangar mcp (managed; edits are replaced)", "# <<< hangar mcp")


def merge_codex_toml(existing: str, fragment: str) -> str:
    start, end = CODEX_BLOCK
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    body = pattern.sub("", existing).rstrip()
    block = f"{start}\n{fragment.rstrip()}\n{end}\n"
    return (body + "\n\n" + block) if body else block


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out
