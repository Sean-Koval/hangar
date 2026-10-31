# A2A endpoint (removed)

hangar's A2A endpoint (`/a2a` and `/.well-known/agent-card.json`) was removed after v0.2.0 (KAN-217). It had no known consumer. Both routes now answer 404, and its `HANGAR_A2A_*` settings stop hangar from starting until they are unset.

Hand hangar work over [MCP](mcp.md) (`submit_job`) or [REST](jobs/README.md) instead. [Removed surfaces](upgrading.md#removed-surfaces) lists every removed setting and route.

## What was on this page

| Section | Now |
| --- | --- |
| <span id="protocol-version"></span>Protocol version | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="discovery-and-auth"></span>Discovery and auth | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="sending-work"></span>Sending work | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="structured-results"></span>Structured results | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="tasks-contexts-and-follow-ups"></span>Tasks, contexts and follow-ups | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="streaming-and-push-notifications"></span>Streaming and push notifications | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="examples"></span>Examples | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="curl"></span>curl | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="google-adk"></span>Google ADK | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="the-a2a-python-sdk"></span>The A2A Python SDK | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="settings"></span>Settings | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="known-limits"></span>Known limits | [Removed surfaces](upgrading.md#removed-surfaces) |
