# Plugins and events out

hangar's doors, trackers, notifiers and deliverers are plugins. Its own (GitHub, Slack, Jira, GitHub Projects, the events webhook) are built in. A package of your own adds more without changing hangar: a chat bot's door, a company tracker, a deliverer that bumps your deploy repo. The surface is `hangar.plugin/v1`, in `hangar.ext`.

Events out are the other half. Every change to a job becomes a [`hangar.event/v1`](#events) that each notifier hears once: queued, needs input, a tool approval, PR opened, approved, landed. The built-in [webhook notifier](#the-webhook) signs each event and POSTs it. So a bot in a room (the Symphony bot, for example) needs no code inside hangar.

!!! note "Not Claude Code plugins"
    The plugins on this page extend the control plane. The Claude Code plugins seeded into sandbox images are a different thing ([environments.md](environments.md#claude-code-plugins)).

## Write a plugin

A plugin is a Python package with one entry point in the `hangar.plugins` group:

```toml
# pyproject.toml of your package
[project.entry-points."hangar.plugins"]
acme-bump = "hangar_acme.bump:PLUGIN"
```

The entry point names an object (or a class, which hangar instantiates) with a `name`, an `api`, a pydantic `Settings` model and `register`:

```python
from pydantic import BaseModel
from hangar.ext import PluginHost

class Settings(BaseModel):
    model_config = {"extra": "forbid"}
    argocd_app_prefix: str

class AcmeBump:
    name = "acme-bump"
    api = "hangar.plugin/v1"      # another major version refuses to start
    Settings = Settings           # validated at startup

    def register(self, host: PluginHost, settings: Settings) -> None:
        host.add_deliverer(Bumper(settings.argocd_app_prefix))

PLUGIN = AcmeBump()
```

`register` runs once, when the app is built. It adds through the host:

| Call | Adds | Interface |
| --- | --- | --- |
| `host.add_door(d)` | Routes, from `d.mount(app)`; checks for `hangar doctor`, from `d.doctor()`; and, with `d.reply`, replies in threads whose ref starts with `d.name:` | `hangar.ext.doors.Door` |
| `host.add_tracker(scheme, fn)` | Ticket write-back for jobs whose `intake.tracker` label starts with `scheme:` ([intake.md](intake.md)) | `hangar.ext.trackers.Tracker` |
| `host.add_notifier(n)` | Events out: `n.wants(person, event)` and `n.send(event, person)` | `hangar.ext.notify.Notifier` |
| `host.add_deliverer(d)` | A post-merge action, keyed by `d.kind` (change sets) | `hangar.ext.deliverers.Deliverer` |

The host also gives read access to `host.jobs`, `host.store`, `host.broker` and `host.settings`, as the built-ins have. Two plugins can't claim the same door name, tracker scheme, notifier name or deliverer kind. The `github` reply scheme is hangar's own.

## Enable and configure

Installed is not enabled. An outside plugin loads only when `HANGAR_PLUGINS` names it in `enable`. Its settings go under its name, in the section for what it adds (`doors`, `notifiers`, `trackers`, `deliverers`) or in `settings`. Secrets are vault credential names, never values.

```yaml
# HANGAR_PLUGINS, as JSON in the environment or the hangar-site ConfigMap
enable: [acme-bump]
notifiers:
  webhook: {url: https://symphony-bot.internal/hangar/events, secret_credential: bot-hook-key,
            events: [pr.opened, job.needs_input, turn.tool_approval, pr.landed, "changeset.*"]}
deliverers:
  acme-bump: {argocd_app_prefix: svc-}
```

`HANGAR_PLUGINS` is read once at startup, so `hangar config set plugins` applies after a restart. The server refuses to start, with the reason, when:

- `HANGAR_PLUGINS` names a plugin that isn't installed;
- it configures one that isn't enabled;
- settings don't validate, or carry a key the plugin doesn't know;
- it has an unknown top-level key, or configures one plugin in two sections;
- a plugin targets another major version of the API, can't be imported, or fails to register;
- two installed packages use one plugin name.

The built-ins always load, and each waits for its own settings as before. The GitHub door answers 404 until `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` is set, Slack until its signing secret is set, Jira until `HANGAR_JIRA` is. So `hangar setup jira` still works without a restart. The `webhook` notifier loads when `notifiers.webhook` is configured.

See what loaded with `hangar doctor` (one `plugin <name>` check each, with its version and what it added) or `GET /api/admin/plugins` (admin):

```json
{"api": "hangar.plugin/v1", "plugins": [
  {"name": "jira", "version": "0.1.0", "builtin": true, "api": "hangar.plugin/v1", "adds": ["door:jira", "tracker:jira"]},
  {"name": "acme-bump", "version": "1.4.0", "builtin": false, "api": "hangar.plugin/v1", "adds": ["deliverer:acme-bump-argocd"]}]}
```

A company image adds plugins on top of a released hangar image: `FROM` the image by digest, then `pip install` the pinned plugin wheels.

## Events

hangar derives events from its own records, so jobs.py doesn't call notifiers. Each write of a job or a session is read as the facts true of it now. Each fact has a stable `id`, and each notifier gets it once. What was sent is kept a week in kv (`events:sent:<notifier>:<id>`), so a restart doesn't repeat it and a burst of writes is one event. A notifier hears facts from when it was first enabled, not the store's history. A notifier that answers `False` gets the event again on a later pass, up to ten times. After that, the job's timeline says it was dropped.

| `type` | When | `id` |
| --- | --- | --- |
| `job.awaiting_approval` | An untrusted sender's job waits for a person; `actions`: `approve_job`, `reject_job` | `<job>:job.awaiting_approval` |
| `job.queued` | Admitted | `<job>:job.queued` |
| `job.started` | A turn started | `<job>:job.started:<turn>` |
| `job.needs_input` | The agent asked a question; `data.question`; `actions`: `follow_up_job` | `<job>:job.needs_input:<turn>` |
| `turn.tool_approval` | A live turn waits on a tool approval; `actions`: `answer_approval` allow / deny | `<job or session>:turn.tool_approval:<approval>` |
| `job.delivered` | A branch was pushed; `data.head_sha` | `<job>:job.delivered:<sha12>` |
| `pr.opened` | `data.pr_number`, `data.draft` | `<job>:pr.opened:<n>` |
| `pr.approved` | An approval that counts; `data.approved_by` | `<job>:pr.approved:<sha12>` |
| `pr.checks` | Every check concluded on a head; `data.checks`: `passed` or `failed` | `<job>:pr.checks:<sha12>:<verdict>` |
| `pr.landed` | Merged; `data.merge_sha` | `<job>:pr.landed:<n>` |
| `pr.closed` | Closed without merging | `<job>:pr.closed:<n>` |
| `job.failed`, `job.finished` | The turn ended (`finished` for every outcome, in `data.outcome`) | `<job>:job.finished:<turn>` |

Other producers add types under their own prefix. Change sets, for example, emit `changeset.*` events through `events_out.of(jobs).emit(event)`. Each event is a JSON object:

```json
{"schema": "hangar.event/v1", "id": "job-3fa2c1d0:pr.opened:12", "type": "pr.opened", "at": 1792427003.7,
 "job": "job-3fa2c1d0", "session": "ses-…", "person": "dana", "repo": "https://github.com/acme/api.git",
 "reply_to": "symphony:<streamId>:<messageId>",
 "links": {"job": "https://hangar.example.com/#jobs?job=job-3fa2c1d0", "pr": "https://github.com/acme/api/pull/12"},
 "summary": "fix the login bug: PR #12 opened", "actions": [], "data": {"pr_number": 12, "draft": true, "state": "succeeded"}}
```

`reply_to` says where the conversation is. It comes from the job's `relay.reply_to` label (a relay acting `on_behalf_of` someone sets it), else the latest turn's `reply_to`, else the job's `source_ref`. Summaries are redacted, as thread posts are. `actions` are MCP tool calls a renderer can offer as buttons. The person pressing one still needs the right to make the call.

### The webhook

`notifiers.webhook` POSTs each wanted event with these headers:

| Header | |
| --- | --- |
| `X-Hangar-Event` | The event's `type` |
| `X-Hangar-Delivery` | The event's `id`; drop one you've already seen |
| `X-Hangar-Timestamp` | Unix seconds when it was sent |
| `X-Hangar-Signature` | `sha256=` and the hex HMAC-SHA256 of `<timestamp>.<body>`, keyed with the secret in `secret_credential` |

Settings: `url`, `secret_credential`, `events` (glob patterns, default every event) and `timeout_seconds` (default 10). The URL follows the [job callback](jobs/README.md) rules: https and a public address, unless the host is in `HANGAR_JOB_CALLBACK_HOSTS` (for an in-cluster bot, `symphony-bot.internal`). The rules are checked at startup and again on every connection. Without a usable secret nothing is sent unsigned, and `hangar doctor` says why.

To verify, recompute the HMAC over the timestamp, a dot and the raw body. Compare in constant time. Refuse a timestamp more than five minutes from your clock, and an event id you've already seen within that window. `hangar.events_out.verify(key, body, headers)` does the first two:

```python
import hashlib, hmac, time

def verify(key: bytes, body: bytes, headers) -> bool:
    ts, sig = headers["X-Hangar-Timestamp"], headers["X-Hangar-Signature"]
    good = "sha256=" + hmac.new(key, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return abs(time.time() - int(ts)) <= 300 and hmac.compare_digest(good, sig)
```

## The built-ins

| Plugin | Adds | Configured by |
| --- | --- | --- |
| `github` | Door `/api/hooks/github` (issues, comments, landing, checks, Projects items) | `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` ([jobs/README.md](jobs/README.md)) |
| `slack` | Door `/api/hooks/slack/*`, replies in `slack:` threads | `HANGAR_SLACK_*` ([slack.md](slack.md)) |
| `jira` | Door `/api/hooks/jira`, comments on `jira:` issues, tracker `jira` | `HANGAR_JIRA` ([intake.md](intake.md)) |
| `ghprojects` | Tracker `ghproject` | `HANGAR_GITHUB_PROJECTS` ([intake.md](intake.md)) |
| `webhook` | Notifier `webhook` | `HANGAR_PLUGINS.notifiers.webhook` ([above](#the-webhook)) |
| `command`, `image-ready` | Deliverers for change sets | Listed now; they show as "not available in this build" until change sets ship |

The Slack inbox DMs and digests ([team.md](team.md#inbox-delivery-dms-and-the-digest)) stay as they were.
