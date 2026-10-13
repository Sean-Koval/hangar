"""hangar-envd end to end on the real Docker provider: started in a sandbox on first use, I/O through it, the
provider path when it goes away, nothing left when the sandbox is destroyed.

Opt-in (needs Docker and the binary from envd/build.sh):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_envd_docker.py -v
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from hangar.envd import EnvdTerminal, find_binaries

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]


async def via_envd(fleet, ref: str) -> bool:
    """Whether a command's parent's parent (bash <- timeout <- ?) is the daemon."""
    res = await fleet.exec(ref, "p=$(awk '/^PPid/{print $2}' /proc/$PPID/status); readlink /proc/$p/exe")
    return "hangar-envd" in res.stdout


async def test_envd_in_a_docker_sandbox(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if not find_binaries(Settings(envd_dir=envd_dir)):
        pytest.skip("no hangar-envd binary (run envd/build.sh)")
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    prov = fleet.provider("docker")
    try:
        sb = await fleet.create("minimal", name="e2e-envd", labels={"e2e-envd": "1"}, actor="test")
        ep = await prov.envd.ready(fleet.get(sb.id))
        assert ep is not None, "the daemon did not come up"
        assert await via_envd(fleet, sb.id)
        r = await fleet.exec(sb.id, "whoami; echo $HOME; test -z \"$HANGAR_ENVD_TOKEN\" && echo no-token-in-env")
        assert r.exit_code == 0 and r.stdout.split() == ["agent", "/home/agent", "no-token-in-env"], r

        # Files, with the provider's semantics (relative paths, limits, ownership).
        await fleet.write_file(sb.id, "envd/f.txt", b"hello")
        assert await fleet.read_file(sb.id, "envd/f.txt") == b"hello"
        assert await fleet.read_file(sb.id, "envd/f.txt", limit=2) == b"hel"
        r = await fleet.exec(sb.id, "stat -c '%U %a' /workspace/envd/f.txt")
        assert r.stdout.strip() == "agent 644"
        assert await fleet.read_file(sb.id, "envd/missing") is None

        # Output is capped like the provider's.
        r = await fleet.exec(sb.id, "head -c 3000000 /dev/zero | tr '\\0' x; echo; echo end")
        assert r.truncated and r.stdout.endswith("end\n") and len(r.stdout) < 300_000

        # Deny by default: from inside the sandbox, without the token, the daemon answers 401.
        r = await fleet.exec(sb.id, "exec 3<>/dev/tcp/127.0.0.1/49983; printf 'GET /info HTTP/1.1\\r\\nHost: x\\r\\n"
                                    "Connection: close\\r\\n\\r\\n' >&3; head -1 <&3")
        assert " 401 " in r.stdout, r

        # The dashboard's live terminal.
        term = await fleet.attach(sb.id, None, 100, 30)
        try:
            assert isinstance(term, EnvdTerminal)
            await term.write(b"stty size; echo $((6*7))\r")
            seen = b""
            while b"42" not in seen or b"30 100" not in seen:
                chunk = await asyncio.wait_for(term.read(), 10)
                assert chunk, seen
                seen += chunk
        finally:
            await term.close()
            fleet.detach(sb.id)

        # The daemon dies: the next command still runs (the provider's path) and the daemon comes back.
        c = prov.client.containers.get(sb.provider_ref)
        assert c.exec_run(["pkill", "-f", "hangar-envd"], user="root").exit_code == 0
        await asyncio.sleep(0.2)
        r = await fleet.exec(sb.id, "echo still-works")
        assert r.stdout == "still-works\n"
        assert await prov.envd.ready(fleet.get(sb.id)) is not None
        assert await via_envd(fleet, sb.id)

        # Destroying the sandbox ends what the daemon started, and hangar forgets the daemon.
        await fleet.exec(sb.id, "nohup sleep 4242 >/dev/null 2>&1 &")
        assert subprocess.run(["pgrep", "-f", "sleep 4242"], capture_output=True).returncode == 0
        await fleet.destroy(sb.id)
        assert sb.id not in prov.envd._eps
        await asyncio.sleep(0.5)
        assert subprocess.run(["pgrep", "-f", "sleep 4242"], capture_output=True).returncode == 1
    finally:
        await fleet.destroy_many(label="e2e-envd")
        await fleet.stop()
