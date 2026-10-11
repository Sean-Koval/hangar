"""Agent harness profiles: how each coding-agent CLI is installed, authenticated and run headless.

Each profile is data, so adding a new harness means adding an entry here (or in a YAML
file passed via `harness_overrides` in an environment spec) - no provider changes needed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from hangar.models import CredentialKind


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


class Harness(BaseModel):
    name: str
    title: str
    install: str  # shell, run as the sandbox user during image build
    needs: list[str] = Field(default_factory=list)  # runtime prerequisites, e.g. ["node"]
    env: dict[str, str] = Field(default_factory=dict)  # non-secret defaults baked into the image
    credentials: list[CredentialSlot] = Field(default_factory=list)
    logins: list[LoginFlow] = Field(default_factory=list)
    run: str  # headless invocation; $HANGAR_PROMPT_FILE holds the prompt
    skills_dir: str | None = None  # relative to $HOME
    instructions_file: str | None = None  # global CLAUDE.md / AGENTS.md path relative to $HOME
    first_run: str | None = None  # shell executed at image build to pre-seed config (onboarding etc.)
    verify: str = "true"


HARNESSES: dict[str, Harness] = {
    "claude-code": Harness(
        name="claude-code",
        title="Claude Code",
        install="curl -fsSL https://claude.ai/install.sh | bash",
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
            "--output-format stream-json --verbose"
        ),
        skills_dir=".claude/skills",
        instructions_file=".claude/CLAUDE.md",
        # Pre-seed global state so interactive `claude` in a fresh sandbox skips the first-run wizard.
        first_run=(
            "mkdir -p ~/.claude && "
            "python3 - <<'PY'\n"
            "import json, os, pathlib\n"
            "p = pathlib.Path.home() / '.claude.json'\n"
            "d = json.loads(p.read_text()) if p.exists() else {}\n"
            "d.update({'hasCompletedOnboarding': True, 'bypassPermissionsModeAccepted': True})\n"
            "p.write_text(json.dumps(d))\n"
            "PY"
        ),
        verify="claude --version",
    ),
    "codex": Harness(
        name="codex",
        title="OpenAI Codex CLI",
        needs=["node"],
        install="npm install -g @openai/codex",
        credentials=[
            CredentialSlot(
                kind=CredentialKind.API_KEY,
                env_var="CODEX_API_KEY",
                post_inject='printenv CODEX_API_KEY | codex login --with-api-key >/dev/null 2>&1 || true',
            ),
            # ChatGPT login. Refresh tokens rotate, so one auth.json may only be live in one place.
            CredentialSlot(
                kind=CredentialKind.AUTH_FILE, file_path=".codex/auth.json", exclusive=True
            ),
        ],
        logins=[
            LoginFlow(
                command="codex login --device-auth",
                captures=CredentialKind.AUTH_FILE,
                capture_file=".codex/auth.json",
                notes="Device-code flow: open the URL on any device and enter the code.",
            )
        ],
        run='codex exec --full-auto "$(cat "$HANGAR_PROMPT_FILE")"',
        skills_dir=".codex/skills",
        instructions_file=".codex/AGENTS.md",
        verify="codex --version",
    ),
    "antigravity": Harness(
        name="antigravity",
        title="Google Antigravity CLI (agy)",
        install="curl -fsSL https://antigravity.google/cli/install.sh | bash",
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
        ],
        logins=[
            LoginFlow(
                command="agy",
                captures=CredentialKind.GENERIC,
                notes=(
                    "Experimental: agy stores OAuth in the OS keyring, which headless containers may lack. "
                    "Prefer a GEMINI_API_KEY credential for fleets."
                ),
            )
        ],
        run=(
            'agy -p "$(cat "$HANGAR_PROMPT_FILE")" --output-format stream-json '
            "--dangerously-skip-permissions"
        ),
        instructions_file=".gemini/GEMINI.md",
        verify="agy --version",
    ),
    "gemini": Harness(
        name="gemini",
        title="Gemini CLI",
        needs=["node"],
        install="npm install -g @google/gemini-cli",
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
        run='gemini -p "$(cat "$HANGAR_PROMPT_FILE")" --yolo',
        instructions_file=".gemini/GEMINI.md",
        verify="gemini --version",
    ),
    "git": Harness(
        name="git",
        title="Git / GitHub",
        install="true",
        credentials=[
            CredentialSlot(
                kind=CredentialKind.GIT_TOKEN,
                env_var="GH_TOKEN",
                post_inject=(
                    'git config --global credential.helper store && '
                    'printf "https://x-access-token:%s@github.com\\n" "$GH_TOKEN" > ~/.git-credentials && '
                    "chmod 600 ~/.git-credentials"
                ),
            ),
            CredentialSlot(
                kind=CredentialKind.SSH_KEY,
                file_path=".ssh/id_ed25519",
                post_inject="ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null || true",
            ),
        ],
        run="true",
    ),
}


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
