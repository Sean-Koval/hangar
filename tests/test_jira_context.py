"""KAN-292: jira-KEY.md carries the ticket's comments, parent or epic, subtasks, linked issues and text attachments.

Jira Cloud (REST v3, ADF) and Data Center (REST v2, wiki markup and the Epic Link field) are faked with
httpx.MockTransport from recorded shapes in tests/fixtures/intake; nothing writes to a live Jira."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hangar import intake, jira

FIX = Path(__file__).parent / "fixtures" / "intake"
DANA = "5b10ac8d82e05b22cc7d4ef5"
CLOUD = {"url": "https://acme.atlassian.net", "email": "hangar@acme.com", "token_credential": "jira-api",
         "webhook_secret_credential": "jira-hook", "projects": {"ABC": {"repo": "acme/api"}},
         "trigger": {"status": "Ready for agent"}}
SERVER = {**CLOUD, "deployment": "server", "url": "https://jira.acme.internal"}


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class FakeJira:
    def __init__(self, data: dict, cloud: bool):
        self.data, self.cloud, self.calls = data, cloud, []
        self.api = "/rest/api/3" if cloud else "/rest/api/2"

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        want = ("Basic " + base64.b64encode(b"hangar@acme.com:tok").decode()) if self.cloud else "Bearer tok"
        if r.headers.get("authorization") != want:
            return httpx.Response(401)
        p, d = r.url.path, self.data
        if p == f"{self.api}/field":
            return httpx.Response(200, json=d["fields"])
        if p == f"{self.api}/issue/ABC-12/comment":
            return httpx.Response(200, json=d["comments"])
        if p == f"{self.api}/issue/ABC-12/remotelink":
            return httpx.Response(200, json=d["remotelinks"])
        if p == f"{self.api}/issue/ABC-12":
            return httpx.Response(200, json=d["links"])
        if p == f"{self.api}/issue/ABC-1":
            return httpx.Response(200, json=d["parent"])
        if p == "/rest/api/3/attachment/content/10001" and r.url.params.get("redirect") == "false":
            return httpx.Response(200, content=d["attachment_10001"].encode())
        if p == "/secure/attachment/301/notes.md":
            return httpx.Response(200, content=d["attachment_301"].encode())
        return httpx.Response(404, json={"errorMessages": ["nope"]})


@pytest.fixture
def linked(monkeypatch):
    people = {DANA: "dana", "JIRAUSER10100": "dana"}
    monkeypatch.setattr(intake, "person_for", lambda store, source, ext: people.get(ext or ""))
    return SimpleNamespace(store=None)


async def build(cfg_raw: dict, data: dict) -> tuple[jira.TicketContext, FakeJira, jira.JiraConfig]:
    cfg = jira.JiraConfig.model_validate(cfg_raw)
    fake = FakeJira(data, cfg.deployment == "cloud")
    api = jira.JiraAPI(cfg, "tok", transport=httpx.MockTransport(fake))
    try:
        return await jira.fetch_context(api, cfg, "ABC-12"), fake, cfg
    finally:
        await api.close()


async def test_cloud_ticket_context_in_order_with_trust_marks(linked):
    tc, fake, cfg = await build(CLOUD, fixture("jira_context_cloud.json"))
    assert tc.problems == []
    text = jira.assemble("# ABC-12: Retry uploads\n\ndesc", jira.context_sections(linked, cfg, tc),
                         cfg.context_max_bytes)
    order = [text.index(h) for h in ("## Comments", "## Epic: ABC-1", "## Subtasks and linked issues",
                                     "## Attachments", "## Confluence pages")]
    assert order == sorted(order)
    # newest comments oldest first, each marked; hangar's own comments are left out
    assert text.index("Ignore the above") < text.index("Acceptance: retries")
    assert "Dana Lee, 2025-10-14T11:04:00 (trusted: linked to hangar person dana)" in text
    assert "Pat Outside, 2025-10-14T10:30:00 (untrusted: not linked to a hangar person)" in text
    assert "[hangar] Queued" not in text
    assert "Uploads must survive a flaky network." in text
    assert "- ABC-13 (Subtask, To Do): Add a retry test" in text
    assert "- blocks ABC-20 (Story, To Do): Ship uploads v2" in text
    assert "- relates to XYZ-7 (Bug, Done): Storage timeouts" in text
    # a text attachment is read by id without following Jira to the media host; others are listed by name
    assert "### upload.log" in text and "timed out after 30s" in text
    assert "- screenshot.png (not included: not text)" in text and "- huge.log (not included: over 1 MB)" in text
    assert not any("/attachment/content/1000" + n in c.url.path for c in fake.calls for n in "23")
    assert "- https://acme.atlassian.net/wiki/spaces/ENG/pages/123/Uploads" in text
    assert "github.com/acme/api/pull/9" not in text
    comments = next(c for c in fake.calls if c.url.path.endswith("/comment"))
    assert comments.url.params["orderBy"] == "-created" and comments.url.params["maxResults"] == "30"


async def test_data_center_epic_link_wiki_comments_and_attachments_on_this_site_only(linked):
    tc, fake, cfg = await build(SERVER, fixture("jira_context_server.json"))
    text = jira.assemble("# ABC-12", jira.context_sections(linked, cfg, tc), cfg.context_max_bytes)
    assert all(c.url.path.startswith(("/rest/api/2/", "/secure/attachment/")) for c in fake.calls)
    links = next(c for c in fake.calls if c.url.path == "/rest/api/2/issue/ABC-12")
    assert "customfield_10008" in links.url.params["fields"]
    assert "## Epic: ABC-1: Upload reliability" in text and "flaky network" in text
    assert "Retries must log the {{attempt}} number." in text and "(trusted: linked to hangar person dana)" in text
    assert "### notes.md" in text and "exponential backoff" in text
    # an attachment whose URL points off this Jira is never fetched with hangar's credential
    assert "- elsewhere.txt (not included: could not be read from this Jira)" in text
    assert not any(c.url.host == "evil.example" for c in fake.calls)
    assert "- https://confluence.acme.internal/display/ENG/Uploads" in text


async def test_a_part_that_fails_is_noted_and_the_rest_still_comes(linked):
    data = fixture("jira_context_cloud.json")
    cfg = jira.JiraConfig.model_validate(CLOUD)
    inner = FakeJira(data, True)

    def fake(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/comment"):
            return httpx.Response(403, json={"errorMessages": ["You do not have permission"]})
        return inner(r)

    api = jira.JiraAPI(cfg, "tok", transport=httpx.MockTransport(fake))
    try:
        tc = await jira.fetch_context(api, cfg, "ABC-12")
    finally:
        await api.close()
    assert tc.comments == [] and tc.parent and tc.subtasks
    assert len(tc.problems) == 1 and "comments" in tc.problems[0] and "permission" in tc.problems[0]
    text = jira.assemble("# ABC-12", jira.context_sections(linked, cfg, tc), cfg.context_max_bytes)
    assert "## Not read" in text and "tok" not in text


async def test_comment_count_is_a_setting():
    cfg = jira.JiraConfig.model_validate({**CLOUD, "context_comments": 0})
    fake = FakeJira(fixture("jira_context_cloud.json"), True)
    api = jira.JiraAPI(cfg, "tok", transport=httpx.MockTransport(fake))
    try:
        tc = await jira.fetch_context(api, cfg, "ABC-12")
    finally:
        await api.close()
    assert tc.comments == [] and not any(c.url.path.endswith("/comment") for c in fake.calls)


def test_the_file_is_capped_with_a_note_saying_what_was_cut():
    sections = [("comments", "## Comments\n\n" + "x" * 30_000), ("epic", "## Epic\n\nshort"),
                ("attachments", "## Attachments\n\n" + "y" * 5_000)]
    text = jira.assemble("# ABC-12\n\ndesc", sections, 20_000)
    assert len(text.encode()) <= 20_000
    assert "## Cut" in text and "Left out: the rest of comments, epic, attachments." in text
    assert jira.assemble("# h", sections[1:2], 20_000) == "# h\n\n## Epic\n\nshort\n"


def test_settings_are_bounded():
    with pytest.raises(ValueError):
        jira.JiraConfig.model_validate({**CLOUD, "context_max_bytes": 100})
