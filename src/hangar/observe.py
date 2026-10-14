"""OpenTelemetry traces from the control plane, and the hooks that also feed /metrics (docs/observability.md).

Tracing is set up from the standard OTEL_* variables and stays off unless they name an exporter
(OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, or OTEL_TRACES_EXPORTER=otlp|console). Off,
nothing from opentelemetry is imported and every hook costs one global check. Hangar keeps its own
TracerProvider (the process-wide one is left alone) and exports OTLP over HTTP/protobuf, or gRPC when that
exporter is installed. The SDK applies OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES, OTEL_TRACES_SAMPLER,
OTEL_BSP_* and the exporter's own endpoint, header and timeout variables.

Spans cover the sandbox lifecycle, exec and file I/O (only inside another span, so housekeeping loops don't make
a trace per call), checkpoints, session boots, agent turns (GenAI `invoke_agent` spans with model and token
usage), job phases, MCP tool calls, REST requests and the git gateway. Agent runs get TRACEPARENT, so a harness
that exports its own telemetry (HANGAR_OTEL_SANDBOX_ENDPOINT) puts its spans under hangar's turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NamedTuple
from urllib.parse import quote, urlparse

from hangar import metrics

log = logging.getLogger("hangar.observe")

_tracer: Any = None  # an opentelemetry Tracer while tracing is on
_provider: Any = None
_redact: Callable[[str], str] = lambda s: s  # noqa: E731 - the broker's, once the app starts
_open_turns: dict[str, Any] = {}  # run id -> its invoke_agent span, from begin_turn until end_turn
_MAX_OPEN_TURNS = 2000
_QUIET_PATHS = {"/metrics", "/healthz"}  # scraped and probed every few seconds: no span each time
# review(observe): the client picks the method, so only HTTP semconv's known ones are labels; the rest are "_OTHER"
_METHODS = {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
# GenAI semconv `gen_ai.provider.name` for harnesses tied to one model provider
_PROVIDERS = {"claude-code": "anthropic", "codex": "openai", "antigravity": "gcp.gemini"}
CODEX_BLOCK = ("# >>> hangar otel (managed; edits are overwritten) >>>", "# <<< hangar otel <<<")


# ============================================================================ setup
def tracing_exporter(env: Mapping[str, str] = os.environ) -> str | None:
    """"otlp", "console", or None (tracing off). Unlike the SDK's default, nothing set means off."""
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    names = [x.strip().lower() for x in env.get("OTEL_TRACES_EXPORTER", "").split(",") if x.strip()]
    if "none" in names:
        return None
    for n in names:
        if n in ("otlp", "console"):
            return n
    if names:
        log.warning("OTEL_TRACES_EXPORTER=%s is not supported by hangar (use otlp or console); tracing is off",
                    ",".join(names))
        return None
    if env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return "otlp"
    return None


def _span_exporter(kind: str) -> Any:
    if kind == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    proto = (os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
             or "http/protobuf").strip().lower()
    try:
        if proto == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        elif proto == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        else:
            log.warning("OTLP protocol %s is not supported (use http/protobuf or grpc); tracing is off", proto)
            return None
    except ImportError:
        pkg = "opentelemetry-exporter-otlp-proto-grpc" if proto == "grpc" else "hangar[otel]"
        log.warning("OTEL_* asks for %s traces, but the exporter is not installed (pip install '%s'); tracing is "
                    "off", proto, pkg)
        return None
    return OTLPSpanExporter()


async def start(settings: Any, redact: Callable[[str], str] | None = None, exporter: Any = None) -> None:
    """At app start: metrics (unless HANGAR_METRICS_ENABLED=false) and, when OTEL_* asks for it, tracing.
    `exporter` (tests) is a SpanExporter used as is, synchronously."""
    global _tracer, _provider, _redact
    await stop()
    _redact = redact or (lambda s: s)
    m = metrics.setup(settings.metrics_enabled)
    if m is not None:
        m.start_lag_monitor()
    kind = "given" if exporter is not None else tracing_exporter()
    if kind is None:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        log.warning("OTEL_* asks for traces, but the OpenTelemetry SDK is not installed (pip install "
                    "'hangar[otel]'); tracing is off")
        return
    exporter = exporter if exporter is not None else _span_exporter(kind)
    if exporter is None:
        return
    attrs: dict[str, Any] = {}
    if not os.environ.get("OTEL_SERVICE_NAME") and "service.name=" not in os.environ.get("OTEL_RESOURCE_ATTRIBUTES",
                                                                                         ""):
        attrs["service.name"] = "hangar"
    provider = TracerProvider(resource=Resource.create(attrs))
    provider.add_span_processor(SimpleSpanProcessor(exporter) if kind == "given" else BatchSpanProcessor(exporter))
    _provider, _tracer = provider, provider.get_tracer("hangar")
    log.info("tracing on: %s spans", "otlp" if kind == "given" else kind)


async def stop() -> None:
    """Flush and drop the tracer (export may block on the network, so off the loop) and stop the lag monitor."""
    global _tracer, _provider
    if metrics.M is not None:
        await metrics.M.stop_lag_monitor()
        metrics.M = None
    provider, _provider, _tracer = _provider, None, None
    for sp in list(_open_turns.values()):
        with contextlib.suppress(Exception):
            sp.end()
    _open_turns.clear()
    if provider is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(provider.shutdown)


def enabled() -> bool:
    return _tracer is not None


# ============================================================================ spans
def _clean(attrs: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (attrs or {}).items():
        if v is None:
            continue
        out[k] = v if isinstance(v, str | bool | int | float) else str(v)
    return out


def _fail(sp: Any, e: BaseException) -> None:
    from opentelemetry.trace import Status, StatusCode

    cancelled = isinstance(e, asyncio.CancelledError)
    # MCP semconv: a tool's own failure (an error result to the client) is "tool_error"
    etype = "cancelled" if cancelled else "tool_error" if type(e).__name__ == "ToolError" else type(e).__name__
    sp.set_attribute("error.type", etype)
    msg = "" if cancelled else _redact(str(e))[:300]
    sp.set_status(Status(StatusCode.ERROR, msg or None))


def _kind(kind: str) -> Any:
    from opentelemetry.trace import SpanKind

    return {"server": SpanKind.SERVER, "client": SpanKind.CLIENT}.get(kind, SpanKind.INTERNAL)


def _in_span() -> bool:
    from opentelemetry import trace

    return trace.get_current_span().is_recording()


@contextlib.contextmanager
def span(name: str, attrs: dict[str, Any] | None = None, *, kind: str = "internal",
         child_only: bool = False, context: Any = None) -> Iterator[Any]:
    """A span around a block, or None when tracing is off (or `child_only` and nothing encloses it)."""
    if _tracer is None or (child_only and not _in_span()):
        yield None
        return
    with _tracer.start_as_current_span(name, context=context, kind=_kind(kind), attributes=_clean(attrs),
                                       record_exception=False, set_status_on_exception=False) as sp:
        try:
            yield sp
        except BaseException as e:
            _fail(sp, e)
            raise


def set_attrs(sp: Any, attrs: dict[str, Any] | None) -> None:
    if sp is not None and attrs:
        sp.set_attributes(_clean(attrs))


def _safe(fn: Callable[..., Any] | None, *a: Any) -> Any:
    """Telemetry must never break the work it watches."""
    if fn is None:
        return None
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001
        log.debug("telemetry hook failed", exc_info=True)
        return None


class _Spec(NamedTuple):
    attrs: Callable[[dict[str, Any]], dict[str, Any]] | None = None  # from the call's arguments
    result: Callable[[Any], dict[str, Any]] | None = None  # from what it returned
    record: Callable[[dict[str, Any], Any, BaseException | None, float], None] | None = None  # metrics
    child_only: bool = False


def traced(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate an async function or method with the span (and metrics) registered for `name` in _SPECS."""
    spec = _SPECS.get(name, _Spec())

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*a: Any, **kw: Any) -> Any:
            if _tracer is None and (spec.record is None or metrics.M is None):
                return await fn(*a, **kw)
            try:
                bound = sig.bind(*a, **kw)
                bound.apply_defaults()
                args = dict(bound.arguments)
            except TypeError:
                args = {}
            t0 = time.monotonic()
            result: Any = None
            err: BaseException | None = None
            attrs = _safe(spec.attrs, args) if _tracer is not None else None
            with span(name, attrs, child_only=spec.child_only) as sp:
                try:
                    result = await fn(*a, **kw)
                    if sp is not None:
                        set_attrs(sp, _safe(spec.result, result))
                    return result
                except BaseException as e:
                    err = e
                    raise
                finally:
                    _safe(spec.record, args, result, err, time.monotonic() - t0)

        return wrapper

    return deco


def current_traceparent() -> str | None:
    """The W3C traceparent of the current span, for a process hangar starts. A span the sampler dropped still
    passes its context on (flags 00), so the agent doesn't start a trace of its own."""
    if _tracer is None:
        return None
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    if not trace.get_current_span().get_span_context().is_valid:
        return None

    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    tp = carrier.get("traceparent")
    if not tp:
        return None
    # Only the sampled bit: the SDK also sets W3C level 2's "random" bit (03), which older parsers reject.
    head, _, flags = tp.rpartition("-")
    return f"{head}-{int(flags, 16) & 1:02x}"


# ============================================================================ sandbox lifecycle
def _sb_attrs(sb: Any) -> dict[str, Any]:
    return {"hangar.sandbox.id": getattr(sb, "id", None), "hangar.provider": getattr(sb, "provider", None),
            "hangar.environment": getattr(sb, "environment", None)}


def _record_create(a: dict[str, Any], sb: Any, err: BaseException | None, secs: float) -> None:
    fleet = a.get("self")
    env = a.get("environment")
    provider = getattr(sb, "provider", None) or a.get("provider") or fleet.settings.default_provider
    warm = None
    if sb is not None and a.get("_image") is None and sb.info.get("warm") is not None:
        if fleet._warm(fleet.catalog.get(env)).warm_pool > 0:  # a claim was possible: a hit or a miss
            warm = bool(sb.info["warm"])
    metrics.sandbox_created(provider, env, secs, err is None, warm)


def _record_capture(a: dict[str, Any], ck: Any, err: BaseException | None, secs: float) -> None:
    result = "error" if err is not None else ("stored" if ck is not None else "unchanged")
    metrics.checkpoint("capture", result, secs, a.get("reason"), sum(ck.parts.values()) if ck is not None else None)


def _record_restore(a: dict[str, Any], _: Any, err: BaseException | None, secs: float) -> None:
    metrics.checkpoint("restore", "error" if err is not None else "ok", secs)


def _job_attrs(a: dict[str, Any]) -> dict[str, Any]:
    job = a.get("job")
    if job is None and a.get("job_id") and a.get("self") is not None:
        job = a["self"]._jobs.get(a["job_id"])
    if job is None:
        return {"hangar.job.id": a.get("job_id")}
    return {"hangar.job.id": job.id, "hangar.job.source": job.source, "hangar.environment": job.environment,
            "hangar.job.strategy": job.strategy, "hangar.job.turn": len(job.turns)}


_SPECS: dict[str, _Spec] = {
    "sandbox.create": _Spec(
        attrs=lambda a: {"hangar.environment": a.get("environment"), "hangar.provider": a.get("provider"),
                         "hangar.harness": a.get("harness"), "hangar.sandbox.fork": a.get("_parent") is not None},
        result=lambda sb: {**_sb_attrs(sb), "hangar.sandbox.warm": sb.info.get("warm")},
        record=_record_create),
    "sandbox.image": _Spec(
        attrs=lambda a: {"hangar.environment": a["spec"].name, "hangar.provider": a.get("pname"),
                         "hangar.harness": a.get("harness")},
        result=lambda ref: {"hangar.image": ref}),
    "sandbox.start_wait": _Spec(attrs=lambda a: _sb_attrs(a.get("sb"))),
    "sandbox.setup": _Spec(attrs=lambda a: {**_sb_attrs(a.get("sb")), "hangar.harness": a.get("hname")}),
    "sandbox.repo": _Spec(attrs=lambda a: _sb_attrs(a.get("sb"))),
    "sandbox.destroy": _Spec(attrs=lambda a: {"hangar.sandbox.ref": a.get("ref")}, result=_sb_attrs),
    "sandbox.snapshot": _Spec(attrs=lambda a: {"hangar.sandbox.ref": a.get("ref"),
                                               "hangar.snapshot.kind": a.get("kind")},
                              result=lambda s: {"hangar.snapshot.id": s.id, "hangar.snapshot.bytes": s.size_bytes}),
    "sandbox.exec": _Spec(attrs=lambda a: {"hangar.sandbox.ref": a.get("ref"), "hangar.exec.timeout": a.get("timeout")},
                          result=lambda r: {"hangar.exec.exit_code": r.exit_code}, child_only=True),
    "sandbox.read_file": _Spec(attrs=lambda a: {"hangar.sandbox.ref": a.get("ref")},
                               result=lambda d: {"hangar.file.bytes": len(d) if d is not None else None},
                               child_only=True),
    "sandbox.write_file": _Spec(attrs=lambda a: {"hangar.sandbox.ref": a.get("ref"),
                                                 "hangar.file.bytes": len(a.get("data") or b"")}, child_only=True),
    "sandbox.list_files": _Spec(attrs=lambda a: {"hangar.sandbox.ref": a.get("ref")}, child_only=True),
    "checkpoint.capture": _Spec(
        attrs=lambda a: {"hangar.session.id": a["s"].id, "hangar.checkpoint.reason": a.get("reason")},
        result=lambda ck: {"hangar.checkpoint.stored": ck is not None,
                           "hangar.checkpoint.bytes": sum(ck.parts.values()) if ck is not None else None},
        record=_record_capture),
    "checkpoint.restore": _Spec(
        attrs=lambda a: {"hangar.session.id": a["s"].id, "hangar.checkpoint.id": a["ck"].id,
                         "hangar.checkpoint.bytes": sum(a["ck"].parts.values())},
        record=_record_restore),
    "session.boot": _Spec(attrs=lambda a: {"hangar.session.id": a["s"].id, "hangar.environment": a["s"].environment},
                          result=_sb_attrs),
    # integrate (#57): database backups (scheduled, or `hangar admin backup`) and staged restores
    "db.backup": _Spec(result=lambda r: {"hangar.backup.name": r["name"], "hangar.backup.db_bytes": r["db_bytes"],
                                         "hangar.backup.stored_bytes": r["stored_bytes"]},
                       record=lambda a, r, err, secs: metrics.db_backup(err is None, secs)),
    "db.restore_stage": _Spec(attrs=lambda a: {"hangar.backup.name": a.get("name")},
                              result=lambda r: {"hangar.backup.db_bytes": r["db_bytes"]}),
    "job.process": _Spec(attrs=_job_attrs),
    "job.provision": _Spec(attrs=_job_attrs),
    "job.deliver": _Spec(attrs=lambda a: {**_job_attrs(a), "hangar.job.draft": a.get("draft"),
                                          "hangar.job.deliver": a["job"].deliver}),
}


# ============================================================================ agent turns
def _turn_attrs(s: Any, turn: Any) -> dict[str, Any]:
    harness = turn.harness or s.harness
    return {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": harness,
            "gen_ai.provider.name": _PROVIDERS.get(harness or ""), "gen_ai.request.model": s.model,
            "gen_ai.conversation.id": turn.conversation, "hangar.session.id": s.id, "hangar.job.id": s.job_id,
            "hangar.environment": s.environment, "hangar.run.id": turn.run_id, "hangar.turn.mode": turn.mode,
            "hangar.harness": harness}


def turn_begin(fn: Callable[..., Any]) -> Callable[..., Any]:
    """SessionService.begin_turn: open the turn's `invoke_agent` span. It is current while the agent starts (so
    the run gets its TRACEPARENT) and stays open until end_turn."""

    @functools.wraps(fn)
    async def wrapper(self: Any, s: Any, turn: Any, *a: Any, **kw: Any) -> Any:
        if _tracer is None:
            return await fn(self, s, turn, *a, **kw)
        from opentelemetry import trace

        sp = _tracer.start_span(f"invoke_agent {s.harness or ''}".rstrip(), kind=_kind("internal"),
                                attributes=_clean(_turn_attrs(s, turn)))
        try:
            with trace.use_span(sp, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                result = await fn(self, s, turn, *a, **kw)
        except BaseException as e:
            _fail(sp, e)
            sp.end()
            raise
        set_attrs(sp, _turn_attrs(s, turn))  # harness, model, run id and conversation are known now
        if turn.run_id:
            while len(_open_turns) >= _MAX_OPEN_TURNS:  # end_turn never came (a crash in between): don't leak
                _open_turns.pop(next(iter(_open_turns))).end()
            _open_turns[turn.run_id] = sp
        else:
            sp.end()
        return result

    return wrapper


def turn_end(fn: Callable[..., Any]) -> Callable[..., Any]:
    """SessionService.end_turn: the turn's span is current while hangar waits for the agent and checkpoints, then
    ends with the outcome and usage; the turn is counted in /metrics. After a control-plane restart the span is
    made again from the turn's start time."""

    @functools.wraps(fn)
    async def wrapper(self: Any, s: Any, turn: Any, *a: Any, **kw: Any) -> Any:
        if _tracer is None and metrics.M is None:
            return await fn(self, s, turn, *a, **kw)
        sp = None
        if _tracer is not None:
            sp = _open_turns.pop(turn.run_id or "", None) or _tracer.start_span(
                f"invoke_agent {turn.harness or s.harness or ''}".rstrip(), kind=_kind("internal"),
                start_time=int(turn.started_at * 1e9) if turn.started_at else None,
                attributes=_clean(_turn_attrs(s, turn)))
        try:
            if sp is None:
                result = await fn(self, s, turn, *a, **kw)
            else:
                from opentelemetry import trace

                with trace.use_span(sp, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = await fn(self, s, turn, *a, **kw)
        except BaseException as e:
            _safe(_end_turn, sp, s, turn, "error", e)
            raise
        _safe(_end_turn, sp, s, turn, result[0], None)
        return result

    return wrapper


def _end_turn(sp: Any, s: Any, turn: Any, outcome: str, err: BaseException | None) -> None:
    from hangar.events import total_usage

    usage = total_usage([turn])
    secs = (turn.finished_at or time.time()) - turn.started_at if turn.started_at else None
    metrics.turn_finished(harness=turn.harness or s.harness, environment=s.environment, model=s.model,
                          requester=turn.requester, outcome=outcome, seconds=secs, usage=usage)
    if sp is None:
        return
    try:
        _turn_result(sp, s, turn, outcome, err, usage)
    finally:
        sp.end()


def _turn_result(sp: Any, s: Any, turn: Any, outcome: str, err: BaseException | None, usage: Any) -> None:
    attrs = {**_turn_attrs(s, turn), "hangar.turn.outcome": outcome, "hangar.turn.exit_code": turn.exit_code}
    if usage is not None:
        attrs |= {
            # semconv: input_tokens includes cached tokens; hangar's excludes them
            "gen_ai.usage.input_tokens": usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": usage.cache_read_tokens,
            "gen_ai.usage.cache_write.input_tokens": usage.cache_write_tokens,
            "gen_ai.response.model": usage.model, "hangar.cost_usd": usage.cost_usd,
            "hangar.cost_source": usage.cost_source}
    set_attrs(sp, attrs)
    if err is not None:
        _fail(sp, err)
    elif outcome != "interrupted" and (outcome not in ("exited", "needs_input") or (turn.exit_code or 0) != 0):
        # integrate (#59): an interrupt is a person stopping the turn (exit 130), not a failure
        from opentelemetry.trace import Status, StatusCode

        sp.set_attribute("error.type", outcome if outcome != "exited" else "agent_exit_code")
        sp.set_status(Status(StatusCode.ERROR, _redact(turn.outcome_reason or outcome)[:300]))


# ============================================================================ agent runs: TRACEPARENT, harness OTel
def sandbox_endpoint(settings: Any) -> str | None:
    """HANGAR_OTEL_SANDBOX_ENDPOINT, if it is a plain http(s) URL (it goes into env files and TOML)."""
    ep = (settings.otel_sandbox_endpoint or "").strip().rstrip("/")
    if not ep:
        return None
    u = urlparse(ep)
    if u.scheme not in ("http", "https") or not u.netloc or re.search(r"[\s\"'\\`$]", ep) or u.username:
        log.warning("HANGAR_OTEL_SANDBOX_ENDPOINT must be a plain http(s) URL without credentials; ignored")
        return None
    return ep


def run_env(settings: Any, sb: Any, run_id: str, harness: str | None) -> dict[str, str]:
    """Environment for one agent run: TRACEPARENT (the current turn span) and, with a sandbox endpoint, the
    variables that make Claude Code export its own telemetry. Codex reads its exporters from config.toml
    (configure_harness); both read TRACEPARENT (Claude Code in -p mode; codex 0.162.1 logs "TRACEPARENT detected")."""
    env: dict[str, str] = {}
    if tp := current_traceparent():
        env["TRACEPARENT"] = tp
    ep = sandbox_endpoint(settings)
    if ep is None:
        return env
    res = {"hangar.sandbox.id": sb.id, "hangar.run.id": run_id, "hangar.environment": sb.environment,
           "hangar.harness": harness}
    env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(f"{k}={quote(str(v), safe='')}" for k, v in res.items() if v)
    if harness == "claude-code":
        env |= {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                "OTEL_TRACES_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", "OTEL_EXPORTER_OTLP_ENDPOINT": ep}
    return env


def codex_otel_toml(ep: str) -> str:
    def exporter(path: str) -> str:
        return f'{{ otlp-http = {{ endpoint = {json.dumps(ep + path)}, protocol = "binary" }} }}'

    return "\n".join(["[otel]", 'environment = "hangar"', f"exporter = {exporter('/v1/logs')}",
                      f"trace_exporter = {exporter('/v1/traces')}", f"metrics_exporter = {exporter('/v1/metrics')}"])


async def configure_harness(prov: Any, sb: Any, harness: str, home: str, settings: Any) -> None:
    """Codex takes its exporters only from ~/.codex/config.toml (`[otel]`), so with a sandbox endpoint hangar
    writes a managed block there. A config that already has an [otel] table is left alone, with a warning."""
    ep = sandbox_endpoint(settings)
    if ep is None or harness != "codex":
        return
    from hangar.fleet import CONFIG_READ_LIMIT  # integrate (#57): the sandbox decides the file's size

    path = f"{home}/.codex/config.toml"
    raw = await prov.read_file(sb, path, limit=CONFIG_READ_LIMIT) or b""
    if len(raw) > CONFIG_READ_LIMIT:
        sb.info.setdefault("warnings", []).append(
            f"Codex telemetry not configured: {path} is over {CONFIG_READ_LIMIT // 1024 // 1024} MB")
        return
    cur = raw.decode(errors="replace")
    start, end = CODEX_BLOCK
    body = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", cur, flags=re.S).rstrip()
    if re.search(r"(?m)^\s*(\[\s*otel\s*[\].]|otel\s*[.=])", body):
        sb.info.setdefault("warnings", []).append(
            "Codex telemetry not configured: ~/.codex/config.toml already has an [otel] section")
        return
    block = f"{start}\n{codex_otel_toml(ep)}\n{end}\n"
    await prov.write_file(sb, path, ((body + "\n\n" + block) if body else block).encode(), mode=0o600)


# ============================================================================ MCP tool calls
def mcp_tool(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """A `tools/call <name>` server span (MCP semconv) and the call counted by tool and status."""

    def finish(t0: float, err: BaseException | None) -> None:
        status = "ok" if err is None else ("tool_error" if type(err).__name__ == "ToolError" else "error")
        metrics.mcp_call(name, status, time.monotonic() - t0)

    def attrs() -> dict[str, Any]:
        return {"mcp.method.name": "tools/call", "gen_ai.tool.name": name, "gen_ai.operation.name": "execute_tool"}

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapped(*a: Any, **kw: Any) -> Any:
            if _tracer is None and metrics.M is None:
                return await fn(*a, **kw)
            t0, err = time.monotonic(), None
            with span(f"tools/call {name}", attrs(), kind="server"):
                try:
                    return await fn(*a, **kw)
                except BaseException as e:
                    err = e
                    raise
                finally:
                    finish(t0, err)
    else:
        @functools.wraps(fn)
        def wrapped(*a: Any, **kw: Any) -> Any:
            if _tracer is None and metrics.M is None:
                return fn(*a, **kw)
            t0, err = time.monotonic(), None
            with span(f"tools/call {name}", attrs(), kind="server"):
                try:
                    return fn(*a, **kw)
                except BaseException as e:
                    err = e
                    raise
                finally:
                    finish(t0, err)
    return wrapped


# ============================================================================ HTTP: REST, dashboard, MCP, gateway
def _route(scope: dict[str, Any]) -> str:
    """The matched route's template, never the raw path (ids would blow up span names and metric labels). Git
    gateway requests are named by git service."""
    path = getattr(scope.get("route"), "path", None)
    if not isinstance(path, str) or not path:
        raw = scope.get("path", "")  # /mcp is routed on a copy of the scope (MCPPathMiddleware)
        return "/mcp" if raw == "/mcp" or raw.startswith("/mcp/") else "unmatched"
    if path.startswith("/git/"):
        raw = scope.get("path", "")
        return next((f"/git/{svc}" for svc in ("info/refs", "git-upload-pack", "git-receive-pack")
                     if raw.endswith("/" + svc)), "/git")
    return path


def _extract(scope: dict[str, Any]) -> Any:
    """The caller's trace context (an MCP client or service that traces), so hangar's spans join its trace."""
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    headers = {}
    for k, v in scope.get("headers") or []:
        if k in (b"traceparent", b"tracestate"):
            headers[k.decode()] = v.decode("latin-1")
    return TraceContextTextMapPropagator().extract(headers) if headers else None


class HTTPMiddleware:
    """A server span and a latency observation per HTTP request, named by route template."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or (_tracer is None and metrics.M is None):
            return await self.app(scope, receive, send)
        raw_method = scope.get("method", "GET")
        method = raw_method if raw_method in _METHODS else "_OTHER"  # review(observe): a bounded label
        status = 500

        async def send_(msg: dict[str, Any]) -> None:
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
            await send(msg)

        t0 = time.monotonic()
        quiet = _tracer is None or scope.get("path") in _QUIET_PATHS
        attrs = {"http.request.method": method} | ({} if method == raw_method else
                                                   {"http.request.method_original": raw_method[:40]})
        name = method if method == raw_method else "HTTP"  # HTTP semconv: "HTTP {route}" for an unknown method
        cm = contextlib.nullcontext() if quiet else span(name, attrs, kind="server", context=_extract(scope))
        with cm as sp:
            try:
                await self.app(scope, receive, send_)
            finally:
                route = _route(scope)
                if sp is not None:
                    sp.update_name(f"{name} {route}")
                    set_attrs(sp, {"http.route": route, "http.response.status_code": status})
                    if status >= 500:
                        from opentelemetry.trace import Status, StatusCode

                        sp.set_attribute("error.type", str(status))
                        sp.set_status(Status(StatusCode.ERROR))
                metrics.http_request(method, route, status, time.monotonic() - t0)


def gateway_push(result: str, **attrs: Any) -> None:
    """A push the gateway forwarded (ok or failed upstream), on the request's span and in /metrics."""
    metrics.gateway_push(result)
    if _tracer is not None:
        from opentelemetry import trace

        set_attrs(trace.get_current_span(), {"hangar.gateway.push": result, **attrs})


def gateway_denied(reason: str) -> None:
    """A request the gateway refused. The reason is reduced to a few kinds for the metric label."""
    r = reason.lower()
    kind = ("repo_not_granted" if "repo not granted" in r else "too_large" if "too large" in r
            else "protocol" if any(w in r for w in ("request", "pkt", "command", "protocol")) else "ref_policy")
    metrics.gateway_denied(kind)
    if _tracer is not None:
        from opentelemetry import trace

        set_attrs(trace.get_current_span(), {"hangar.gateway.denied": kind})
