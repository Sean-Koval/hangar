# Sandboxes and terminals in the console

What you can do to a sandbox from an opened agent on the [Fleet](../console.md#fleet). Then how the terminal
drawer works, and what Hide, Destroy and Snapshot keep.

## Sandbox actions

On a running sandbox:

- **Terminal**: a shell;
- **Run agent**: a headless agent on a task, in its own terminal, which opens;
- **Run command**;
- **Fork**: snapshot it and start copies with the same files, dependencies and repo state;
- **Snapshot**, **Pause** and **+1 h** on its lease.

A paused one offers **Resume** and **+1 h**. Every sandbox that isn't stopped has **Destroy**. **Open job** or
**Open session** goes to the work's own page.

## Terminals

Terminals open in a drawer at the bottom of the console, one tab each; drag its top edge to resize it. Opening an
agent run attaches to the same terminal the agent uses: you see what it types and can type back.

**Terminal** opens a plain shell. To switch to a terminal an agent or an MCP client is using, choose **Sessions…**,
pick it and **Attach**. A closed connection reconnects when you press Enter. Escape inside a terminal goes to the
program running there (vim, less), not to the console.

## Hide, Destroy and Snapshot

- **Hide**, in the terminal drawer, only hides the drawer. The terminals stay open and the sandbox keeps running.
  Close a terminal with its tab's ×.
- **Destroy**, in an opened agent, deletes the sandbox and its filesystem, after you confirm, and closes its
  terminals. Push or save what you want to keep first. A job's branch and a session's conversation are kept. A
  session's sandbox is checkpointed before it goes, so the session suspends and resumes later from that checkpoint.
- **Snapshot** keeps the sandbox's state without stopping it: a full snapshot where the provider has them, otherwise
  the workspace's files. Snapshots are listed under **Snapshots**, where **Start copy** starts a new sandbox from one.
  Take one before you destroy a sandbox whose files you may need.

From the CLI, `hangar sb rm <name>` removes a sandbox.
