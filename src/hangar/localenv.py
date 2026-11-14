"""`hangar env local`: run an environment spec on this computer the way hangar runs it in the cloud (KAN-472).

A local run is evidence about a cloud one only if both start from the same definition with the same code. So this
uses hangar's own pieces, never copies of them:

- the image is `envspec.render`'s build, the Dockerfile and context a hangar build gets, with the harness baked in
  (there are no pack mounts here);
- the allowlist is `egress.resolve`'s, the policy a hangar sandbox of the spec gets (no mirror, nothing approved);
- egress_sidecar.py enforces it, with the rule match and address checks the control plane's proxy uses.

The agent container mounts nothing from the host. It gets only the variables the contract file names (and the spec's
secrets), from this process's environment, through an env file, never argv. It sits on an internal Docker network
whose only other member is the sidecar: an HTTP CONNECT proxy and a DNS server that refuses every query. So a tool
that skips the proxy fails, and no name resolves inside the container (docs/environments/local-run.md).

The baked harness is set up as a hangar claim sets it up (KAN-496): its pinned CLI first on PATH, its env, its config
and the spec's MCP servers, resolved from the passed variables and written into its config file inside the container.
`check` then asks the agent CLI for one answer (`claude -p`), so a missing or wrong model credential fails there.

    hangar env local up agent --env-dir environments --harnesses harnesses --clone code=CODE_REPO
    hangar env local check agent ...   # two fresh instances report the same; nothing mounted; egress and DNS refused
    hangar env local exec -- claude
    hangar env local down
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import typer

# The sidecar's image: the same python:3.12-slim digest as the control plane's Dockerfile (BASE_IMAGE);
# tests/test_localenv.py keeps the two equal, so a bump reads in one diff.
SIDECAR_IMAGE = "python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
PROXY_PORT = 3128
LABEL = "hangar.localenv"
SIDECAR = Path(__file__).with_name("egress_sidecar.py")
# A DNS query for example.com (A), sent straight to a public resolver by `check`: it must get no answer.
_DNS_PROBE = r"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"

local_app = typer.Typer(no_args_is_help=True, help="Run an environment on this computer, as hangar runs it in the "
                        "cloud: hangar's image build, egress policy and proxy (docs/environments/local-run.md).")


# ------------------------------------------------------------------------------------------------ the definition
def catalog(env_dirs: list[Path], harnesses: Path | None = None):
    """The specs in `env_dirs`, after every directory under `harnesses` with a harness lock (later dirs win)."""
    from hangar import harness_lock as hl
    from hangar.envspec import EnvCatalog

    locks = sorted(d for d in harnesses.iterdir() if (d / hl.LOCK_FILE).is_file()) if harnesses else []
    return EnvCatalog([*locks, *env_dirs])


def contract_names(path: Path | None) -> list[str]:
    """The variable names an env contract (`NAME=` lines, like an .env.example) declares, in order."""
    if path is None:
        return []
    return [m.group(1) for line in path.read_text().splitlines() if (m := re.match(r"^([A-Z][A-Z0-9_]*)=", line))]


def egress_rules(spec: Any) -> list[list]:
    """The allowlist a hangar sandbox of `spec` gets (no package mirror, nothing approved): [host, wildcard, port]."""
    from hangar import egress

    settings = SimpleNamespace(egress_default="allowlist", package_mirror=False, gateway_url=None)
    policy = egress.resolve(settings, spec, harness=spec.harnesses[0] if spec.harnesses else None)  # type: ignore[arg-type]
    if policy.mode == "open":
        raise SystemExit(f"environment {spec.name}: egress is open; a local run needs mode allowlist or none")
    return [[r.host, r.wildcard, r.port] for r in policy.rules]


# ------------------------------------------------------------------------------------------------ docker
def docker(*args: str, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["docker", *args], input=input, capture_output=True)
    if check and r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace").strip()
        raise SystemExit(f"docker {' '.join(args[:2])} failed: {err[-2000:]}")
    return r


def build(spec: Any, harness: str, extra_ca: Path | None) -> str:
    from hangar.envspec import render

    bake = spec.harnesses[:1] if harness == "bake" and spec.harnesses else None
    ctx = render(spec, bake=bake, extra_ca=extra_ca.read_bytes() if extra_ca else None)
    tag = f"hangar-local-{spec.name}:{ctx.digest}{'' if bake else '-nopack'}"
    if docker("image", "inspect", tag, check=False).returncode != 0:
        print(f"building {tag} (hangar's render of environment {spec.name}) ...", file=sys.stderr)
        r = subprocess.run(["docker", "build", "-t", tag, "-"], input=ctx.tar(), capture_output=True)
        if r.returncode != 0:
            raise SystemExit(f"docker build failed:\n{r.stdout.decode(errors='replace')[-3000:]}"
                             f"{r.stderr.decode(errors='replace')[-3000:]}")
    return tag


def names(instance: str) -> SimpleNamespace:
    return SimpleNamespace(agent=f"hangar-local-{instance}", proxy=f"hangar-local-{instance}-egress",
                           net=f"hangar-local-{instance}-net")


def sidecar_args(rules: list[list]) -> list[str]:
    """`docker run` arguments for the sidecar, after the name and labels: its pinned image runs egress_sidecar.py."""
    return ["-e", f"EGRESS_RULES={json.dumps(rules)}", "-e", f"EGRESS_PORT={PROXY_PORT}", "-e", "DNS_PORT=53",
            SIDECAR_IMAGE, "python", "-c", SIDECAR.read_text()]


def agent_args(dns: str, env_file: str, env: dict[str, str] | None = None) -> list[str]:
    """`docker run` arguments for the agent, after the name, labels and network. Docker's resolver forwards every
    name it doesn't own to `dns`, the sidecar, which refuses it: no lookup leaves, whatever the host's resolver is.
    `env` is non-secret (the harness's PATH and env); secrets go through `env_file` only."""
    proxy = f"http://egress:{PROXY_PORT}"
    return ["--dns", dns, "--env-file", env_file,
            *(x for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy") for x in ("-e", f"{k}={proxy}")),
            "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
            *(x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}"))]


# ------------------------------------------------------------------------------------------------ the harness
HOME = "/home/agent"  # envspec.render's default sandbox user
CREDENTIAL_HINT = {"claude-code": "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`, a subscription login) or "
                                  "ANTHROPIC_API_KEY (billed to the API) in your shell, and name it in the contract"}


def baked_harness(spec: Any, harness: str) -> Any:
    """The harness `build` bakes into the image (the spec's first, with an install), or None."""
    from hangar.harnesses import get_harness

    if harness != "bake" or not spec.harnesses:
        return None
    h = get_harness(spec.harnesses[0])
    return h if h.install else None


def image_path(image: str) -> str:
    """The image's own PATH (its ENV), which the harness's bin goes in front of."""
    env = json.loads(docker("image", "inspect", "-f", "{{json .Config.Env}}", image).stdout or b"[]") or []
    return next((e.split("=", 1)[1] for e in env if e.startswith("PATH=")),
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


def harness_env(h: Any, base_path: str) -> dict[str, str]:
    """What a hangar claim exports for the harness (fleet._apply_session): its bin first on PATH, `path_extra` last,
    its env and HANGAR_HARNESS. None of it is secret."""
    if h is None:
        return {}
    extra = "".join(f":{h.prefix}/{d}" for d in h.path_extra)
    return {"PATH": f"{h.prefix}/bin:{base_path}{extra}", **h.env, "HANGAR_HARNESS": h.name}


def harness_config(spec: Any, h: Any, env: dict[str, str],
                   clones: list[tuple[str, str]] | None = None) -> tuple[dict[str, Any], list[str], list[str]]:
    """(files, servers, skipped): the config a hangar claim writes for the harness, as {path under $HOME: content},
    with the spec's MCP servers resolved against `env`, the variables passed into the container (and the spec's own
    env). A cloned input (`--clone DIR=VAR`) resolves to its clone, /workspace/DIR, not its URL: an MCP server such as
    ai-dlc's reads the checkout. A server whose `${VAR}` isn't set is skipped, as at a claim. JSON content is merged
    into the file there; a string (Codex's TOML) replaces hangar's managed block."""
    from hangar.harnesses import deep_merge, mcp_config, resolve_refs

    if h is None:
        return {}, [], []
    files: dict[str, Any] = {rel: dict(patch) for rel, patch in h.config.items()}
    servers = {n: s for n, s in spec.mcp.items() if s.harnesses is None or h.name in s.harnesses}
    resolved, skipped = {}, []
    for n, srv in sorted(servers.items()):
        missing: set[str] = set()
        fields = resolve_refs(srv.model_dump(include={"url", "headers", "command", "args", "env"}),
                              {**spec.env, **env, **{var: f"/workspace/{d}" for d, var in clones or []}}, missing)
        if missing:
            skipped.append(f"MCP server '{n}' skipped: needs {', '.join(sorted(missing))}")
            continue
        resolved[n] = srv.model_copy(update=fields)
    if resolved and h.mcp_format:
        rel, content = mcp_config(h.mcp_format, resolved)
        files[rel] = deep_merge(files[rel], content) if isinstance(content, dict) and isinstance(
            files.get(rel), dict) else content
    return files, sorted(resolved), skipped


def write_config(container: str, files: dict[str, Any]) -> None:
    """Merge each file into the container's (mode 600). Contents go through stdin, so no value reaches argv."""
    from hangar.harnesses import deep_merge, merge_codex_toml

    for rel, content in files.items():
        path = f"{HOME}/{rel}"
        cur = docker("exec", container, "cat", path, check=False)
        old = cur.stdout.decode(errors="replace") if cur.returncode == 0 else ""
        if isinstance(content, str):
            new = merge_codex_toml(old, content)
        else:
            try:
                base = json.loads(old) if old.strip() else {}
            except ValueError:
                base = {}
            new = json.dumps(deep_merge(base if isinstance(base, dict) else {}, content), indent=2)
        docker("exec", "-i", container, "sh", "-c", 'umask 077; mkdir -p "$(dirname "$1")"; cat > "$1"; chmod 600 "$1"',
               "sh", path, input=new.encode())


def setup_harness(container: str, spec: Any, h: Any, env: dict[str, str],
                  clones: list[tuple[str, str]] | None = None) -> list[str]:
    """Write the harness's config and MCP servers into the container; lines for the run's log (names only)."""
    files, servers, skipped = harness_config(spec, h, env, clones)
    if files:
        write_config(container, files)
    return ([f"{h.name}: MCP servers configured: {', '.join(servers) or 'none'}"] if h else []) + skipped


def claude_probe(container: str, spec: Any, h: Any, passed: list[str]) -> list[str]:
    """Failures of the agent probe: the pinned CLI is on PATH and answers one prompt with the passed credential."""
    if h is None or h.name != "claude-code":
        return []
    creds = [c.env_var for c in h.credentials]
    if not set(creds) & set(passed):
        return [f"no Claude credential was passed: {CREDENTIAL_HINT[h.name]}"]
    v = sh(container, "claude --version")
    got = v.stdout.decode(errors="replace").strip()
    if v.returncode != 0:
        return [f"claude is not on PATH in the container: {(got or v.stderr.decode(errors='replace'))[-300:]}"]
    if h.version and not got.startswith(h.version):
        return [f"claude --version is {got!r}, but hangar pins {h.version}"]
    model = f" --model {spec.model}" if spec.model and re.fullmatch(r"[A-Za-z0-9._-]+", spec.model) else ""
    r = sh(container, f"cd /tmp && timeout 300 claude -p 'say ok' --output-format json{model} < /dev/null")
    return probe_failures(r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace"))


def probe_failures(code: int, out: str, err: str) -> list[str]:
    """`claude -p --output-format json` succeeded: exit 0 and a result event that is no error."""
    result = None
    for line in reversed(out.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj
            break
    if code == 0 and result is not None and not result.get("is_error"):
        return []
    why = (str(result.get("result") or result.get("subtype")) if result else (err or out).strip()[-500:]) or "no output"
    hint = " (is the Claude credential valid? `claude setup-token` makes a new one)" if re.search(
        r"auth|login|401|403|token|api key", why, re.I) else ""
    return [f"claude -p failed (exit {code}): {why}{hint}"]


def start(spec: Any, image: str, instance: str, passed_names: list[str], environ: dict[str, str],
          env: dict[str, str] | None = None) -> SimpleNamespace:
    """The sidecar on the default bridge and an internal network, then the agent on the internal network only.
    `env` is the non-secret environment (harness_env); the passed names' values go through an env file."""
    n = names(instance)
    labels = ["--label", f"{LABEL}={instance}"]
    docker("network", "create", "--internal", *labels, n.net)
    docker("run", "-d", "--name", n.proxy, *labels, *sidecar_args(egress_rules(spec)))
    docker("network", "connect", "--alias", "egress", n.net, n.proxy)
    dns = docker("inspect", "-f", "{{(index .NetworkSettings.Networks \"" + n.net + "\").IPAddress}}",
                 n.proxy).stdout.decode().strip()
    if not dns:
        raise SystemExit(f"the sidecar has no address on {n.net}")
    passed = [k for k in passed_names if environ.get(k)]
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:  # values never reach argv
        for k in passed:
            fh.write(f"{k}={environ[k]}\n")
        env_file = fh.name
    try:
        docker("run", "-d", "--name", n.agent, *labels, "--network", n.net, *agent_args(dns, env_file, env), image)
    finally:
        os.unlink(env_file)
    print(f"started {n.agent}: {image}; variables passed by name: {', '.join(passed) or 'none'}", file=sys.stderr)
    n.passed = passed
    return n


def sh(container: str, script: str) -> subprocess.CompletedProcess:
    return docker("exec", container, "bash", "-lc", script, check=False)


def parse_clones(clones: list[str]) -> list[tuple[str, str]]:
    out = []
    for c in clones:
        d, _, var = c.partition("=")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", d) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", var):
            raise typer.BadParameter(f"--clone {c!r}: DIR=VAR, a directory under /workspace and a variable name")
        out.append((d, var))
    return out


def clone_inputs(container: str, clones: list[tuple[str, str]]) -> list[str]:
    """Clone each VAR's repository to /workspace/DIR inside the container, through the proxy."""
    if not clones:
        return []
    pairs = " ".join(f"{d}:{v}" for d, v in clones)
    script = f"""set -e
[ -n "$GH_TOKEN" ] && command -v gh >/dev/null && gh auth setup-git >/dev/null 2>&1 || true
for pair in {pairs}; do
  dir=${{pair%%:*}}; var=${{pair#*:}}; url=${{!var}}
  if [ -z "$url" ]; then echo "input $var is not set"; continue; fi
  [ -d "/workspace/$dir/.git" ] || git clone --quiet --depth 1 "$url" "/workspace/$dir"
done
"""
    r = sh(container, script)
    out = (r.stdout + r.stderr).decode(errors="replace").strip()
    if r.returncode != 0:
        raise SystemExit(f"cloning the inputs failed:\n{out}")
    return [line for line in out.splitlines() if line]


def report(container: str, spec: Any, clones: list[tuple[str, str]], command: str | None) -> tuple[int, str]:
    """The spec's model and env, each clone's commit, and `command`'s output: what two instances must agree on."""
    head = [f"model: {spec.model}", *(f"env {k}: {v}" for k, v in sorted(spec.env.items()))]
    script = "".join(f'echo "{d}: $(git -C /workspace/{d} rev-parse HEAD 2>/dev/null || echo none)"\n'
                     for d, _ in clones)
    if command:
        script += f"echo '--- {command.replace(chr(39), '')}'\n{command}\n"
    r = sh(container, script or "true")
    return r.returncode, "\n".join(head) + "\n" + (r.stdout + r.stderr).decode(errors="replace")


def down(instance: str | None = None) -> None:
    flt = f"label={LABEL}" + (f"={instance}" if instance else "")
    if ids := docker("ps", "-aq", "--filter", flt).stdout.decode().split():
        docker("rm", "-f", *ids)
    if nets := docker("network", "ls", "-q", "--filter", flt).stdout.decode().split():
        docker("network", "rm", *nets)


# ------------------------------------------------------------------------------------------------ the checks
def egress_failures(container: str, allowed_url: str) -> list[str]:
    """An allowed host is reached; an off-list host, plain HTTP, a request that skips the proxy and every DNS lookup
    (through Docker's resolver, or straight to a public resolver on port 53) are refused."""
    out = []
    if sh(container, f"curl -fsS -o /dev/null -I --max-time 20 {allowed_url}").returncode != 0:
        out.append(f"the allowed {allowed_url} was not reached")
    for url in ("https://example.com/", "http://example.com/"):
        if sh(container, f"curl -fsS -o /dev/null --max-time 20 {url}").returncode == 0:
            out.append(f"{url} is off the allowlist and was reached")
    if sh(container, "env -u HTTPS_PROXY -u https_proxy curl -fsS -o /dev/null --max-time 10 "
                     f"{allowed_url}").returncode == 0:
        out.append("a request that skipped the proxy got out")
    for host in ("example.com", "github.com"):  # off-list and on-list: names resolve only in the sidecar
        if sh(container, f"getent hosts {host}").returncode == 0:
            out.append(f"a DNS lookup of {host} got an answer inside the container")
    udp = sh(container, f"exec 3<>/dev/udp/1.1.1.1/53 && printf '{_DNS_PROBE}' >&3 && "
                        "timeout 5 head -c 1 <&3 | wc -c | grep -qv '^0$'")
    if udp.returncode == 0:
        out.append("a DNS query to 1.1.1.1:53 (UDP) got an answer")
    if sh(container, "timeout 5 bash -c '</dev/tcp/1.1.1.1/53'").returncode == 0:
        out.append("a connection to 1.1.1.1:53 (TCP) got out")
    return out


# ------------------------------------------------------------------------------------------------ the CLI
EnvName = Annotated[str, typer.Argument(help="the environment to run")]
EnvDirs = Annotated[list[Path], typer.Option("--env-dir", help="a directory of environment specs (repeatable)")]
Harnesses = Annotated[Path | None, typer.Option(help="a directory of harness directories (each with a harness lock)")]
Contract = Annotated[Path | None, typer.Option(help="an env contract (NAME= lines): the variables passed in by name")]
Clones = Annotated[list[str], typer.Option("--clone", help="DIR=VAR: clone $VAR to /workspace/DIR (repeatable)")]
ReportCmd = Annotated[str | None, typer.Option(help="a command whose output is part of the report")]
Harness = Annotated[str, typer.Option(help="bake the agent CLI into the image (bake), or leave it out (none)")]
ExtraCa = Annotated[Path | None, typer.Option(help="a CA certificate to trust in the image (TLS-intercepting "
                                                    "networks)")]
Instance = Annotated[str, typer.Option(help="name of the local instance")]


def _up(env: str, env_dir: list[Path], harnesses: Path | None, contract: Path | None, clone: list[str],
        harness: str, extra_ca: Path | None, instance: str) -> tuple[SimpleNamespace, Any, list[tuple[str, str]]]:
    if harness not in ("bake", "none"):
        raise typer.BadParameter("--harness is bake or none")
    clones = parse_clones(clone)
    spec = catalog(env_dir, harnesses).get(env)
    image = build(spec, harness, extra_ca)
    passed = list(dict.fromkeys([*contract_names(contract), *spec.secrets]))
    h = baked_harness(spec, harness)
    environ = dict(os.environ)
    n = start(spec, image, instance, passed, environ, harness_env(h, image_path(image)) if h else None)
    n.harness = h
    for line in setup_harness(n.agent, spec, h, {k: environ[k] for k in n.passed}, clones):
        print(line, file=sys.stderr)
    for line in clone_inputs(n.agent, clones):
        print(line, file=sys.stderr)
    return n, spec, clones


@local_app.command("up")
def up_cmd(env: EnvName, env_dir: EnvDirs, harnesses: Harnesses = None, contract: Contract = None,
           clone: Clones = [], report_cmd: ReportCmd = None, harness: Harness = "bake",  # noqa: B006
           extra_ca: ExtraCa = None, instance: Instance = "local") -> None:
    """Build and start it, clone the inputs, and print the report."""
    down(instance)
    n, spec, clones = _up(env, env_dir, harnesses, contract, clone, harness, extra_ca, instance)
    code, text = report(n.agent, spec, clones, report_cmd)
    typer.echo(text, nl=False)
    typer.echo(f"\n{n.agent} is running: `hangar env local exec -- <command>`; stop it with `hangar env local down`",
               err=True)
    raise typer.Exit(code)


@local_app.command("check")
def check_cmd(env: EnvName, env_dir: EnvDirs, harnesses: Harnesses = None, contract: Contract = None,
              clone: Clones = [], report_cmd: ReportCmd = None, harness: Harness = "bake",  # noqa: B006
              extra_ca: ExtraCa = None,
              allowed_url: Annotated[str, typer.Option(help="a URL on the allowlist")] = "https://github.com/",
              keep: Annotated[bool, typer.Option(help="leave the instances running")] = False,
              agent_probe: Annotated[bool, typer.Option(help="ask the baked agent CLI for one answer (`claude -p`) "
                                                             "in the first instance")] = True) -> None:
    """Two fresh instances report the same; nothing is mounted; off-list hosts and every DNS lookup are refused;
    the agent CLI answers with the passed credential."""
    failures: list[str] = []
    texts: list[str] = []
    try:
        for i in (1, 2):
            n, spec, clones = _up(env, env_dir, harnesses, contract, clone, harness, extra_ca,
                                  f"check{i}-{uuid.uuid4().hex[:6]}")
            code, text = report(n.agent, spec, clones, report_cmd)
            texts.append(text)
            typer.echo(f"instance {i}: report exit {code}", err=True)
            if code != 0:
                failures.append(f"instance {i}: the report failed (exit {code}); it is printed below")
            if mounts := json.loads(docker("inspect", "-f", "{{json .Mounts}}", n.agent).stdout or b"[]"):
                failures.append(f"instance {i} has mounts: {mounts}")
            if i == 1:
                bad = egress_failures(n.agent, allowed_url)
                failures += bad
                typer.echo("egress: FAILED" if bad else "egress: allowed host reached; off-list hosts, proxy-skipping "
                           "requests and DNS lookups refused", err=True)
                if agent_probe and n.harness is not None:
                    bad = claude_probe(n.agent, spec, n.harness, n.passed)
                    failures += bad
                    typer.echo("agent probe: FAILED" if bad else f"agent probe: {n.harness.name} answered", err=True)
        if texts[0] != texts[1]:
            import difflib

            diff = "".join(difflib.unified_diff(texts[0].splitlines(True), texts[1].splitlines(True), "1", "2"))
            failures.append(f"the two instances differ:\n{diff}")
        else:
            typer.echo("two fresh instances: identical reports", err=True)
        typer.echo(texts[0], nl=False)
    finally:
        if not keep:
            down()
    for f in failures:
        typer.echo(f"FAIL: {f}", err=True)
    raise typer.Exit(1 if failures else 0)


@local_app.command("report")
def report_cmd_(env: EnvName, env_dir: EnvDirs, harnesses: Harnesses = None, clone: Clones = [],  # noqa: B006
                report_cmd: ReportCmd = None, instance: Instance = "local") -> None:
    """The report again, from the running instance."""
    code, text = report(names(instance).agent, catalog(env_dir, harnesses).get(env), parse_clones(clone), report_cmd)
    typer.echo(text, nl=False)
    raise typer.Exit(code)


@local_app.command("policy")
def policy_cmd(env: EnvName, env_dir: EnvDirs, harnesses: Harnesses = None) -> None:
    """The egress allowlist hangar resolves for it."""
    for host, wildcard, port in egress_rules(catalog(env_dir, harnesses).get(env)):
        typer.echo(("*." if wildcard else "") + host + (f":{port}" if port else ""))


@local_app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_cmd(ctx: typer.Context, instance: Instance = "local") -> None:
    """Run a command in the running instance (default: a login shell): `hangar env local exec -- claude`."""
    tty = "-it" if sys.stdin.isatty() else "-i"
    raise typer.Exit(subprocess.call(["docker", "exec", tty, names(instance).agent, *(ctx.args or ["bash", "-l"])]))


@local_app.command("down")
def down_cmd(instance: Annotated[str | None, typer.Option(help="one instance (default: every one)")] = None) -> None:
    """Stop and remove the instances and their networks."""
    down(instance)
