"""Kubernetes provider backed by kubernetes-sigs/agent-sandbox (GKE Agent Sandbox add-on or OSS install).

Mapping:
    environment  -> SandboxTemplate  hangar-<env>   (pod spec: image, runtimeClass, resources)
                 -> SandboxWarmPool  hangar-<env>   (replicas = spec.warm_pool)
    sandbox      -> SandboxClaim     hangar-<sb-id> (warmPoolRef, lifecycle.shutdownTime)

Isolation comes from the runtime class: `gvisor` (GKE Sandbox, default), `kata-qemu-runtime-rs` (real
microVMs; needs nested-virt Intel nodes), or none (runc, local dev clusters).

I/O uses the pods/exec API, so the control plane needs RBAC for pods/exec in the sandbox
namespace (see deploy/k8s). Images must live in a registry the cluster can pull from
(HANGAR_REGISTRY, e.g. Artifact Registry); builds run with local docker or Cloud Build.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import shlex
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from hangar.config import Settings
from hangar.envd import OFF, SKIP, KubernetesEnvd, Off  # --- envd
from hangar.envspec import BuildContext, EnvSpec
from hangar.harnesses import PACK_ROOT
from hangar.models import ExecResult, Sandbox, SandboxState
from hangar.providers._build import docker_available, docker_build, run_cli
from hangar.providers.base import Capabilities, LogFn, OutputTail, Provider, ProviderError, TerminalStream

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    from kubernetes.stream import stream as k8s_stream
    from kubernetes.stream.ws_client import RESIZE_CHANNEL
except ImportError:  # pragma: no cover
    client = None  # type: ignore[assignment]

CORE_GROUP, CORE_V = "agents.x-k8s.io", "v1beta1"
PS_GROUP, PS_V = "podsnapshot.gke.io", "v1"
SNAPSHOT_LABEL = {"hangar.dev/snapshottable": "true"}  # matched by the PodSnapshotPolicy selector
EXT_GROUP, EXT_V = "extensions.agents.x-k8s.io", "v1beta1"
log = logging.getLogger("hangar.kubernetes")
CONTAINER = "sandbox"
MANAGED = {"app.kubernetes.io/managed-by": "hangar"}
MAX_OUTPUT = 256 * 1024


def _rfc3339(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64_len(n: int) -> int:
    """Length of the (padded) base64 of n bytes. --- scale"""
    return 4 * ((n + 2) // 3)


def _cond(obj: dict, kind: str) -> dict | None:
    return next((c for c in (obj.get("status") or {}).get("conditions") or [] if c.get("type") == kind), None)


def _snapshot_result(trigger: dict) -> str | None:
    """PodSnapshot name once a manual trigger completes; raises if GKE reports failure."""
    status = trigger.get("status") or {}
    for c in status.get("conditions") or []:
        if c.get("type") != "Triggered":
            continue
        if c.get("status") == "True" and c.get("reason") == "Complete":
            name = (status.get("snapshotCreated") or {}).get("name")
            if name:
                return name
        if c.get("status") == "False" and c.get("reason") in ("Failed", "Error"):
            raise ProviderError(f"pod snapshot failed: {c.get('message', 'unknown error')}")
    return None


class K8sTerminal(TerminalStream):
    def __init__(self, ws):
        self.ws = ws
        self._closed = False

    async def read(self) -> bytes:
        def _read() -> bytes:
            while self.ws.is_open():
                self.ws.update(timeout=1)
                out = b""
                if self.ws.peek_stdout():
                    out += self.ws.read_stdout().encode(errors="replace")
                if self.ws.peek_stderr():
                    out += self.ws.read_stderr().encode(errors="replace")
                if out:
                    return out
            return b""

        if self._closed:
            return b""
        return await asyncio.to_thread(_read)

    async def write(self, data: bytes) -> None:
        if not self._closed:
            await asyncio.to_thread(self.ws.write_stdin, data.decode(errors="replace"))

    async def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            await asyncio.to_thread(self.ws.write_channel, RESIZE_CHANNEL, json.dumps({"Width": cols, "Height": rows}))

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await asyncio.to_thread(self.ws.close)


class KubernetesProvider(Provider):
    name = "kubernetes"
    envd: KubernetesEnvd | Off = OFF  # --- envd: set in __init__

    def __init__(self, settings: Settings):
        if client is None:
            raise ProviderError("kubernetes extra not installed: pip install 'hangar[kubernetes]'")
        self.settings = settings
        self.ns = settings.k8s_namespace
        if settings.k8s_pod_snapshots and (settings.k8s_runtime_class or "") != "gvisor":
            raise ProviderError("GKE Pod snapshots require HANGAR_K8S_RUNTIME_CLASS=gvisor")
        try:
            if settings.k8s_in_cluster:
                config.load_incluster_config()
            else:
                config.load_kube_config()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"cannot load kubernetes config: {e}") from e
        self.core = client.CoreV1Api()
        self.crd = client.CustomObjectsApi()
        self.envd = KubernetesEnvd(self, settings)  # --- envd: exec, files and terminals through the in-sandbox daemon
        self._known_images: set[str] = set()
        self._synced: dict[str, tuple] = {}
        try:
            self.crd.list_namespaced_custom_object(EXT_GROUP, EXT_V, self.ns, "sandboxwarmpools", limit=1)
        except ApiException as e:
            if e.status == 404:
                raise ProviderError(
                    "Agent Sandbox CRDs (extensions.agents.x-k8s.io/v1beta1) not found or namespace "
                    f"'{self.ns}' missing. Enable the GKE add-on (--enable-agent-sandbox) or install the OSS "
                    "controller with extensions, then apply deploy/k8s."
                ) from e
            raise ProviderError(f"kubernetes API error: {e.reason}") from e

    @property
    def capabilities(self) -> Capabilities:
        rc = (self.settings.k8s_runtime_class or "").lower()
        # GKE Pod snapshots restore a sandbox only from its *own* snapshots, so they power
        # pause/resume (memory + processes kept, node capacity freed), not cross-sandbox forks.
        return Capabilities(full_snapshots=False, warm_pools=True, pause=self.settings.k8s_pod_snapshots,
                            microvm="kata" in rc, image_volumes=self.settings.k8s_image_volumes,
                            local_build=bool(shutil.which("docker") or shutil.which("gcloud")))

    # ------------------------------------------------------------------ templates
    async def image_exists(self, tag: str) -> bool:
        if tag in self._known_images:
            return True
        probes = []
        if await docker_available():
            probes.append(["docker", "manifest", "inspect", tag])
        if shutil.which("gcloud") and ".pkg.dev/" in tag:
            probes.append(["gcloud", "artifacts", "docker", "images", "describe", tag, "--quiet"])
        for args in probes:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL)
            if await proc.wait() == 0:
                self._known_images.add(tag)
                return True
        return False

    async def build_template(self, spec: EnvSpec, ctx: BuildContext, tag: str, log: LogFn | None = None) -> str:
        if not self.settings.registry:
            raise ProviderError("set HANGAR_REGISTRY (e.g. us-central1-docker.pkg.dev/PROJECT/hangar) so the "
                                "cluster can pull environment images")
        if await docker_available():
            await docker_build(ctx, tag, log=log, platform="linux/amd64", push=True)
        elif shutil.which("gcloud"):
            await self._cloud_build(ctx, tag, log)
        else:
            raise ProviderError("no image builder available: install docker, or gcloud for Cloud Build")
        self._known_images.add(tag)
        return tag

    async def _cloud_build(self, ctx: BuildContext, tag: str, log: LogFn | None) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Dockerfile").write_text(ctx.dockerfile)
            for rel, data in ctx.files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            sa = self.settings.k8s_build_service_account
            (root / "cloudbuild.yaml").write_text(
                "steps:\n- name: gcr.io/cloud-builders/docker\n  env: ['DOCKER_BUILDKIT=1']\n"
                f"  args: ['build', '-t', '{tag}', '.']\nimages: ['{tag}']\ntimeout: 3600s\n"
                # A user-specified build SA can't write to the default logs bucket.
                + ("options:\n  logging: CLOUD_LOGGING_ONLY\n" if sa else "")
            )
            args = ["gcloud", "builds", "submit", "--config", str(root / "cloudbuild.yaml")]
            if sa:
                args.append(f"--service-account=projects/-/serviceAccounts/{sa}")
            await run_cli([*args, str(root)], log=log)

    def _pod_template(self, spec: EnvSpec, image: str) -> dict:
        s = self.settings
        res = spec.resources
        pod: dict = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "runAsNonRoot": True},
            "containers": [{
                "name": CONTAINER,
                "image": image,
                # Required by GKE Agent Sandbox's sandbox-hardening admission policy, and sound anywhere:
                # no capabilities, no privilege escalation (so sudo inside the sandbox is unavailable).
                "securityContext": {"capabilities": {"drop": ["ALL"]}, "allowPrivilegeEscalation": False,
                                    "privileged": False},
                "command": ["sleep", "infinity"],
                "workingDir": s.workspace_dir,
                "env": [{"name": "TERM", "value": "xterm-256color"}],
                "resources": {
                    # --- scale: agents idle on the model most of a turn, so a sandbox reserves part of its CPU
                    # limit (HANGAR_K8S_CPU_REQUEST_FRACTION) and bursts to all of it; memory stays reserved in full.
                    "requests": {"cpu": f"{max(100, round(res.cpus * s.k8s_cpu_request_fraction * 1000))}m",
                                 "memory": f"{int(res.memory_gb * 1024)}Mi"},
                    "limits": {"cpu": str(res.cpus), "memory": f"{int(res.memory_gb * 1024)}Mi"},
                },
            }],
        }
        if res.disk_gb:
            pod["containers"][0]["resources"]["requests"]["ephemeral-storage"] = f"{int(res.disk_gb)}Gi"
        if spec.packs:
            # Harness packs: OCI images mounted read-only (image volumes, stable in Kubernetes 1.36). Pulled once
            # per node and shared by every sandbox, so environment images carry no harness binaries.
            pod["volumes"] = [{"name": f"pack-{n}", "image": {"reference": ref, "pullPolicy": "IfNotPresent"}}
                              for n, ref in sorted(spec.packs.items())]
            pod["containers"][0]["volumeMounts"] = [
                {"name": f"pack-{n}", "mountPath": f"{PACK_ROOT}/{n}", "readOnly": True} for n in sorted(spec.packs)]
        rc = spec.runtime_class if spec.runtime_class is not None else s.k8s_runtime_class
        if rc:
            pod["runtimeClassName"] = rc
            if rc == "gvisor" and s.k8s_gke_node_selector:
                pod["nodeSelector"] = {"sandbox.gke.io/runtime": "gvisor"}
                pod["tolerations"] = [{"key": "sandbox.gke.io/runtime", "operator": "Equal", "value": "gvisor",
                                       "effect": "NoSchedule"}]
            elif rc.startswith("kata") and s.k8s_gke_node_selector:
                # Matches the nested-virt pool created by deploy/terraform/gke (enable_kata_pool).
                pod["nodeSelector"] = {"hangar.dev/runtime": "kata"}
                pod["tolerations"] = [{"key": "hangar.dev/runtime", "operator": "Equal", "value": "kata",
                                       "effect": "NoSchedule"}]
        labels = {**MANAGED, "dev.hangar/env": spec.name}
        if s.k8s_pod_snapshots and rc == "gvisor":
            # Pod snapshots need gVisor, a KSA with access to the snapshot bucket, and the policy's label.
            labels.update(SNAPSHOT_LABEL)
            pod["serviceAccountName"] = s.k8s_snapshot_service_account
        return {"metadata": {"labels": labels}, "spec": pod}

    async def _apply(self, plural: str, body: dict) -> None:
        """Create-or-replace with retries on 409 (concurrent creates race on resourceVersion)."""
        name = body["metadata"]["name"]

        def _do():
            for attempt in range(5):
                try:
                    try:
                        cur = self.crd.get_namespaced_custom_object(EXT_GROUP, EXT_V, self.ns, plural, name)
                    except ApiException as e:
                        if e.status != 404:
                            raise
                        self.crd.create_namespaced_custom_object(EXT_GROUP, EXT_V, self.ns, plural, body)
                        return
                    if cur.get("spec") == body["spec"] and \
                            (cur["metadata"].get("annotations") or {}) == body["metadata"].get("annotations", {}):
                        return  # already up to date
                    body["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
                    self.crd.replace_namespaced_custom_object(EXT_GROUP, EXT_V, self.ns, plural, name, body)
                    return
                except ApiException as e:
                    if e.status != 409 or attempt == 4:
                        raise
                    time.sleep(0.2 * (attempt + 1))

        await asyncio.to_thread(_do)

    async def sync_warm_pool(self, spec: EnvSpec, image: str) -> None:
        name = f"hangar-{spec.name}"
        meta = {"name": name, "labels": {**MANAGED, "dev.hangar/env": spec.name},
                "annotations": {"dev.hangar/image": image}}
        await self._apply("sandboxtemplates", {
            "apiVersion": f"{EXT_GROUP}/{EXT_V}", "kind": "SandboxTemplate", "metadata": dict(meta),
            "spec": {"podTemplate": self._pod_template(spec, image)},
        })
        await self._apply("sandboxwarmpools", {
            "apiVersion": f"{EXT_GROUP}/{EXT_V}", "kind": "SandboxWarmPool", "metadata": dict(meta),
            "spec": {"replicas": max(0, spec.warm_pool), "sandboxTemplateRef": {"name": name},
                     "updateStrategy": {"type": "Recreate"}},
        })

    # ------------------------------------------------------------------ lifecycle
    async def create(self, sandbox: Sandbox, image: str, spec: EnvSpec | None = None) -> None:
        if spec is None:
            raise ProviderError("kubernetes provider needs the environment spec to create a sandbox")
        key = (image, spec.warm_pool, tuple(sorted(spec.packs.items())))
        if self._synced.get(spec.name) != key:
            await self.sync_warm_pool(spec, image)  # idempotent; keeps template image and packs current
            self._synced[spec.name] = key
        claim = f"hangar-{sandbox.id}"
        body = {
            "apiVersion": f"{EXT_GROUP}/{EXT_V}", "kind": "SandboxClaim",
            "metadata": {"name": claim, "labels": {**MANAGED, "dev.hangar/sandbox": sandbox.id,
                                                   "dev.hangar/env": spec.name}},
            "spec": {"warmPoolRef": {"name": f"hangar-{spec.name}"},
                     "lifecycle": {"shutdownPolicy": "Delete"}},
        }
        if sandbox.expires_at:
            body["spec"]["lifecycle"]["shutdownTime"] = _rfc3339(sandbox.expires_at + 300)  # reaper first, k8s backstop
        await asyncio.to_thread(self.crd.create_namespaced_custom_object, EXT_GROUP, EXT_V, self.ns,
                                "sandboxclaims", body)
        sandbox.provider_ref = claim

    async def _claim(self, sandbox: Sandbox) -> dict | None:
        try:
            return await asyncio.to_thread(self.crd.get_namespaced_custom_object, EXT_GROUP, EXT_V, self.ns,
                                           "sandboxclaims", sandbox.provider_ref)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def _pod(self, sandbox: Sandbox) -> str:
        if pod := sandbox.info.get("pod"):
            return pod
        claim = await self._claim(sandbox)
        if not claim:
            raise ProviderError(f"sandbox {sandbox.id} no longer exists in the cluster")
        sb_name = ((claim.get("status") or {}).get("sandbox") or {}).get("name") or \
            claim["metadata"].get("annotations", {}).get("agents.x-k8s.io/sandbox-name")
        if not sb_name:
            raise ProviderError("sandbox not bound yet")
        obj = await asyncio.to_thread(self.crd.get_namespaced_custom_object, CORE_GROUP, CORE_V, self.ns,
                                      "sandboxes", sb_name)
        pod = (obj["metadata"].get("annotations") or {}).get("agents.x-k8s.io/pod-name") or sb_name
        sandbox.info["pod"] = pod
        sandbox.info["k8s_sandbox"] = sb_name
        sandbox.info["warm"] = (obj["metadata"].get("labels") or {}).get("agents.x-k8s.io/launch-type") == "warm"
        return pod

    async def status(self, sandbox: Sandbox) -> SandboxState:
        claim = await self._claim(sandbox)
        if claim is None:
            return SandboxState.STOPPED
        ready = _cond(claim, "Ready")
        if ready and ready.get("status") == "True":
            try:
                pod = await self._pod(sandbox)
                p = await asyncio.to_thread(self.core.read_namespaced_pod, pod, self.ns)
                return SandboxState.RUNNING if p.status.phase == "Running" else SandboxState.STARTING
            except (ProviderError, ApiException):
                return SandboxState.STARTING
        if ready and ready.get("reason") in ("Failed", "Error", "TemplateNotFound", "WarmPoolNotFound"):
            return SandboxState.ERROR
        return SandboxState.STARTING

    async def destroy(self, sandbox: Sandbox) -> None:
        self.envd.forget(sandbox)  # --- envd
        if not sandbox.provider_ref:
            return
        await self._cleanup_triggers(sandbox)
        try:
            await asyncio.to_thread(self.crd.delete_namespaced_custom_object, EXT_GROUP, EXT_V, self.ns,
                                    "sandboxclaims", sandbox.provider_ref,
                                    body=client.V1DeleteOptions(propagation_policy="Foreground"))
        except ApiException as e:
            if e.status != 404:
                raise

    async def explain_pending(self, sandbox: Sandbox) -> str | None:
        """Plain-language reason a claim isn't running: scheduling (quota, no room, scale-up), image pull."""
        try:
            pod = await self._pod(sandbox)
        except ProviderError:
            claim = await self._claim(sandbox)
            ready = _cond(claim or {}, "Ready") or {}
            return ready.get("message") or "waiting for a sandbox from the pool"
        evs = await asyncio.to_thread(self.core.list_namespaced_event, self.ns,
                                      field_selector=f"involvedObject.name={pod}")
        def _ts(e) -> float:
            t = e.last_timestamp or e.event_time or (e.metadata.creation_timestamp if e.metadata else None)
            return t.timestamp() if t else 0.0

        items = sorted(evs.items or [], key=_ts)
        if not items:
            return "waiting to be scheduled"
        e = items[-1]
        return _explain_event(e.reason or "", e.message or "")

    async def set_expiry(self, sandbox: Sandbox, expires_at: float) -> None:
        patch = {"spec": {"lifecycle": {"shutdownTime": _rfc3339(expires_at + 300)}}}
        await asyncio.to_thread(self.crd.patch_namespaced_custom_object, EXT_GROUP, EXT_V, self.ns,
                                "sandboxclaims", sandbox.provider_ref, patch)

    async def list_refs(self) -> dict[str, SandboxState]:
        items = await asyncio.to_thread(self.crd.list_namespaced_custom_object, EXT_GROUP, EXT_V, self.ns,
                                        "sandboxclaims", label_selector="app.kubernetes.io/managed-by=hangar")
        out = {}
        for it in items.get("items", []):
            ready = _cond(it, "Ready")
            out[it["metadata"]["name"]] = (SandboxState.RUNNING if ready and ready.get("status") == "True"
                                           else SandboxState.STARTING)
        return out

    # ------------------------------------------------------------------ I/O
    def _exec_blocking(self, pod: str, argv: list[str], timeout: int,
                       keep: int | None = 2 * MAX_OUTPUT) -> tuple[int, str, str]:
        """Only the last `keep` characters of each stream are kept (None: all of them, for hangar's own uploads
        and reads): the sandbox decides how much its commands print."""
        ws = k8s_stream(self.core.connect_get_namespaced_pod_exec, pod, self.ns, container=CONTAINER,
                        command=argv, stderr=True, stdin=False, stdout=True, tty=False,
                        _preload_content=False)
        big = 1 << 62
        out, err = OutputTail(keep or big, ""), OutputTail(keep or big, "")
        try:
            deadline = time.monotonic() + timeout + 10
            while ws.is_open() and time.monotonic() < deadline:
                ws.update(timeout=1)
                if ws.peek_stdout():
                    out.add(ws.read_stdout())
                if ws.peek_stderr():
                    err.add(ws.read_stderr())
            code = ws.returncode
        finally:
            ws.close()
        return (code if code is not None else -1), out.value(), err.value()

    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None) -> ExecResult:
        if (res := await self.envd.exec(sandbox, command, timeout, cwd, user)) is not None:  # --- envd
            return res
        pod = await self._pod(sandbox)
        wd = cwd or self.settings.workspace_dir
        script = f"cd {shlex.quote(wd)} 2>/dev/null; {command}"
        argv = ["timeout", "--kill-after=5", str(int(timeout)), "bash", "-lc", script]
        t0 = time.monotonic()
        try:
            code, out, err = await asyncio.to_thread(self._exec_blocking, pod, argv, timeout)
        except ApiException as e:
            raise ProviderError(f"exec failed: {e.reason}") from e
        trunc = len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT
        if code == 124:
            err += f"\n[hangar] command timed out after {timeout}s"
        return ExecResult(exit_code=code, stdout=out[-MAX_OUTPUT:], stderr=err[-MAX_OUTPUT:],
                          duration_ms=int((time.monotonic() - t0) * 1000), truncated=trunc)

    async def read_file(self, sandbox, path, limit=None) -> bytes | None:
        if (got := await self.envd.read_file(sandbox, path, limit=limit)) is not SKIP:  # --- envd
            return got
        pod = await self._pod(sandbox)
        q = shlex.quote(path)
        if limit is None:
            code, out, _ = await asyncio.to_thread(
                self._exec_blocking, pod, ["sh", "-c", f"base64 -w0 {q}"], 300, None)
            return base64.b64decode(out) if code == 0 else None
        want = 4 * ((limit + 3) // 3)  # base64 of limit + 1 bytes
        code, out, _ = await asyncio.to_thread(
            self._exec_blocking, pod, ["sh", "-c", f"[ -f {q} ] && head -c {limit + 1} {q} | base64 -w0"], 300,
            want + 16)
        if code != 0 or len(out) > want:  # more than head prints: something in the sandbox isn't head
            return None
        return base64.b64decode(out)

    def _upload_stream_blocking(self, pod: str, path: str, data: bytes, mode: int, timeout: int,
                                size: int | None = None) -> tuple[int, str]:
        """One exec, data on stdin. The exec websocket can't signal EOF to the process, so the receiver reads an
        exact byte count (`head -c N`) instead: no EOF needed, and ~1 round trip instead of one per 96 KB.
        --- scale: base64 is made a frame at a time, so a large upload never holds a second, 4/3-size copy."""
        q, d = shlex.quote(path), shlex.quote(str(Path(path).parent))
        tmp = f"/tmp/.hangar-upload-{uuid.uuid4().hex[:10]}"
        # The size check guards against a short read (a dropped stream) ever replacing the file with a truncation.
        check = f' && [ "$(wc -c < {tmp})" -eq {int(size)} ]' if size is not None else ""
        script = (f"mkdir -p {d} && head -c {_b64_len(len(data))} | base64 -d > {tmp}{check} && chmod {mode:o} {tmp} "
                  f"&& mv {tmp} {q}; rc=$?; rm -f {tmp}; exit $rc")
        ws = k8s_stream(self.core.connect_get_namespaced_pod_exec, pod, self.ns, container=CONTAINER,
                        command=["sh", "-c", script], stderr=True, stdin=True, stdout=True, tty=False,
                        _preload_content=False)
        err: list[str] = []
        deadline = time.monotonic() + timeout
        view = memoryview(data)
        try:
            step = 192 * 1024  # a multiple of 3: each frame's base64 continues the one before it (256 KB a frame)
            for i in range(0, len(data), step):
                if time.monotonic() > deadline or not ws.is_open():
                    raise ProviderError(f"upload of {path} stalled after {i} of {len(data)} bytes")
                ws.write_stdin(base64.b64encode(view[i:i + step]).decode())
                ws.update(timeout=0)
                if ws.peek_stderr():
                    err.append(ws.read_stderr())
            while ws.is_open() and time.monotonic() < deadline:
                ws.update(timeout=1)
                if ws.peek_stderr():
                    err.append(ws.read_stderr())
            code = ws.returncode
        finally:
            ws.close()
        return (code if code is not None else -1), "".join(err)

    async def write_file(self, sandbox, path, data, mode=0o644) -> None:
        if await self.envd.write_file(sandbox, path, data, mode):  # --- envd
            return
        pod = await self._pod(sandbox)
        q = shlex.quote(path)
        d = shlex.quote(str(Path(path).parent))
        b64_len = _b64_len(len(data))  # --- scale: encoded a piece at a time below, never the whole file at once
        if self.settings.k8s_stream_uploads and b64_len > 64 * 1024:
            try:
                code, err = await asyncio.to_thread(self._upload_stream_blocking, pod, path, data, mode,
                                                    max(60, b64_len // (512 * 1024)), len(data))
                if code == 0:
                    return
                log.warning("streamed upload of %s failed (exit %s: %s); falling back to chunks", path, code,
                            err[-200:])
            except Exception as e:  # noqa: BLE001 - fall back to the slow, known-good path
                log.warning("streamed upload of %s failed (%s); falling back to chunks", path, e)
        # Fallback: ship base64 inline in chunks via a temp file (one exec per 96 KB).
        tmp = f"/tmp/.hangar-upload-{uuid.uuid4().hex[:10]}"
        chunk = 72 * 1024  # raw bytes, a multiple of 3: 96 KB of base64 per exec, continuing the chunk before
        await asyncio.to_thread(self._exec_blocking, pod, ["sh", "-c", f": > {tmp}"], 30)
        view = memoryview(data)
        for i in range(0, len(data), chunk):
            part = base64.b64encode(view[i:i + chunk]).decode()
            code, _, err = await asyncio.to_thread(
                self._exec_blocking, pod, ["sh", "-c", f"printf %s {shlex.quote(part)} >> {tmp}"], 60)
            if code != 0:
                raise ProviderError(f"write {path} failed: {err}")
        code, _, err = await asyncio.to_thread(
            self._exec_blocking, pod,
            ["sh", "-c", f"mkdir -p {d} && base64 -d {tmp} > {q} && chmod {mode:o} {q}; rc=$?; rm -f {tmp}; exit $rc"],
            60)
        if code != 0:
            raise ProviderError(f"write {path} failed: {err}")

    # ------------------------------------------------------------------ GKE Pod snapshots: pause/resume
    async def _take_snapshot(self, sandbox: Sandbox, pod: str) -> str:
        trigger = f"hangar-{sandbox.id}-{int(time.time())}"[:63]
        body = {"apiVersion": f"{PS_GROUP}/{PS_V}", "kind": "PodSnapshotManualTrigger",
                "metadata": {"name": trigger, "labels": {**MANAGED, "dev.hangar/sandbox": sandbox.id}},
                "spec": {"targetPod": pod}}
        await asyncio.to_thread(self.crd.create_namespaced_custom_object, PS_GROUP, PS_V, self.ns,
                                "podsnapshotmanualtriggers", body)
        # Triggers are kept until the sandbox is destroyed (the reference client does the same;
        # deleting early may garbage-collect the snapshot it produced).
        sandbox.info.setdefault("snapshot_triggers", []).append(trigger)
        deadline = time.monotonic() + self.settings.k8s_snapshot_timeout_seconds
        while time.monotonic() < deadline:
            obj = await asyncio.to_thread(self.crd.get_namespaced_custom_object, PS_GROUP, PS_V, self.ns,
                                          "podsnapshotmanualtriggers", trigger)
            done = _snapshot_result(obj)
            if done:
                return done
            await asyncio.sleep(2)
        raise ProviderError(f"pod snapshot {trigger} did not complete in time")

    async def _set_mode(self, sandbox: Sandbox, mode: str) -> None:
        name = sandbox.info.get("k8s_sandbox")
        if not name:
            await self._pod(sandbox)
            name = sandbox.info["k8s_sandbox"]
        await asyncio.to_thread(self.crd.patch_namespaced_custom_object, CORE_GROUP, CORE_V, self.ns, "sandboxes",
                                name, {"spec": {"operatingMode": mode}})

    async def pause(self, sandbox: Sandbox) -> None:
        if not self.settings.k8s_pod_snapshots:
            raise ProviderError("pause on Kubernetes needs GKE Pod snapshots (HANGAR_K8S_POD_SNAPSHOTS=true)")
        pod = await self._pod(sandbox)
        uid = await self._take_snapshot(sandbox, pod)
        old = await asyncio.to_thread(self.core.read_namespaced_pod, pod, self.ns)
        await self._set_mode(sandbox, "Suspended")
        deadline = time.monotonic() + self.settings.k8s_snapshot_timeout_seconds
        while time.monotonic() < deadline:  # wait for the pod to go away so its node capacity is freed
            try:
                cur = await asyncio.to_thread(self.core.read_namespaced_pod, pod, self.ns)
                if cur.metadata.uid != old.metadata.uid:
                    break
            except ApiException as e:
                if e.status == 404:
                    break
                raise
            await asyncio.sleep(2)
        sandbox.info.pop("pod", None)
        sandbox.info["paused_snapshot"] = uid

    async def resume(self, sandbox: Sandbox) -> None:
        uid = sandbox.info.get("paused_snapshot")
        await self._set_mode(sandbox, "Running")
        sandbox.info.pop("pod", None)
        deadline = time.monotonic() + self.settings.k8s_snapshot_timeout_seconds
        while time.monotonic() < deadline:
            # The controller recreates the pod under the Sandbox's own name; fall back to the claim lookup.
            pod = sandbox.info.get("k8s_sandbox")
            try:
                if not pod:
                    pod = await self._pod(sandbox)
                p = await asyncio.to_thread(self.core.read_namespaced_pod, pod, self.ns)
            except (ProviderError, ApiException):
                sandbox.info.pop("pod", None)
                await asyncio.sleep(2)
                continue
            sandbox.info["pod"] = pod
            conds = {c.type: c for c in (p.status.conditions or [])} if p.status else {}
            if p.status and p.status.phase == "Running" and getattr(conds.get("Ready"), "status", "") == "True":
                restored = conds.get("PodRestored")
                if uid and not (restored and restored.status == "True" and uid in (restored.message or "")):
                    raise ProviderError(
                        f"sandbox resumed as a fresh pod instead of from snapshot {uid}: "
                        f"{getattr(restored, 'message', 'no PodRestored condition')}. Files outside the image "
                        "were lost; destroy and recreate it.")
                sandbox.info.pop("paused_snapshot", None)
                return
            await asyncio.sleep(2)
        raise ProviderError("timed out waiting for the sandbox to resume")

    async def _cleanup_triggers(self, sandbox: Sandbox) -> None:
        for trigger in sandbox.info.get("snapshot_triggers", []):
            try:
                await asyncio.to_thread(self.crd.delete_namespaced_custom_object, PS_GROUP, PS_V, self.ns,
                                        "podsnapshotmanualtriggers", trigger)
            except ApiException:
                pass

    async def open_terminal(self, sandbox, command, cols=120, rows=32) -> TerminalStream:
        if (term := await self.envd.open_terminal(sandbox, command, cols, rows)) is not None:  # --- envd
            return term
        pod = await self._pod(sandbox)

        def _open():
            return k8s_stream(self.core.connect_get_namespaced_pod_exec, pod, self.ns, container=CONTAINER,
                              command=command, stderr=True, stdin=True, stdout=True, tty=True,
                              _preload_content=False)

        ws = await asyncio.to_thread(_open)
        term = K8sTerminal(ws)
        await term.resize(cols, rows)
        return term

    async def close(self) -> None:  # --- envd
        await self.envd.close()


def _explain_event(reason: str, message: str) -> str:
    """Turn a Kubernetes pod event into something a person waiting on a sandbox can act on."""
    m = message.strip()
    low = m.lower()
    if reason == "TriggeredScaleUp":
        return "adding a node to the cluster (usually 1-3 minutes)"
    if reason == "NotTriggerScaleUp":
        if "quota" in low:
            return f"the cluster can't add a node: a Google Cloud quota is exhausted ({m[:200]})"
        if "max node group size" in low:
            return "the sandbox node pool is at its maximum size; raise sandbox_max_nodes or free sandboxes"
        return f"the cluster can't add a node: {m[:200]}"
    if reason == "FailedScheduling":
        if "insufficient" in low:
            return f"no node has room right now ({m[:160]}); waiting for one to be added"
        return f"can't be scheduled yet: {m[:200]}"
    if reason == "Pulling":
        return "pulling the environment image onto the node (first time on this node)"
    if reason in ("Pulled", "Created", "Started", "Scheduled"):
        return {"Pulled": "image pulled; starting", "Created": "starting", "Started": "starting",
                "Scheduled": "placed on a node; starting"}[reason]
    if reason in ("Failed", "ErrImagePull", "ImagePullBackOff", "BackOff"):
        return f"problem starting: {m[:200]}"
    return f"{reason}: {m[:200]}" if reason else m[:200]
