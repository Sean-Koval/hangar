# E2B-compatible API (removed)

hangar's E2B-compatible API (`/e2b`, its sandbox URLs and the `sandboxes` token scope) was removed after v0.2.0 (KAN-217). It had no known consumer. Its routes now answer 404, and its settings stop hangar from starting until they are unset.

Drive sandboxes over [MCP](mcp.md) or the REST API instead. The `e2b` provider, which runs hangar's own sandboxes on E2B or CubeSandbox, stays: it is [experimental, and outside 1.0](microvm.md). [Removed surfaces](upgrading.md#removed-surfaces) lists every removed setting and route.

## What was on this page

| Section | Now |
| --- | --- |
| <span id="quick-start"></span>Quick start | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="what-works"></span>What works | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="what-doesnt-and-what-happens-instead"></span>What doesn't (and what happens instead) | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="how-e2b-maps-onto-hangar"></span>How E2B maps onto hangar | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="security"></span>Security | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="the-sandbox-router"></span>The sandbox router | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="code-interpreter"></span>Code interpreter | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="on-gke"></span>On GKE | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="migrating-from-e2b"></span>Migrating from E2B | [Removed surfaces](upgrading.md#removed-surfaces) |
| <span id="settings"></span>Settings | [Removed surfaces](upgrading.md#removed-surfaces) |
