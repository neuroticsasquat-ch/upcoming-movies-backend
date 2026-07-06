# Issue tracker: Linear

Issues and PRDs for this repo live in **Linear**, not GitHub Issues. Work is organised under:

- **Team**: Neuroticsasquatch
- **Initiative**: Upcoming Movies Tracker
- **Ticket IDs**: `NEU-###` (Linear assigns them)

Operate on Linear through the **Linear MCP tools** (`mcp__linear-server__*`). A **GitHub↔Linear connector** links branches/PRs to tickets and moves each ticket through its **workflow state** (In Progress / In Review / Done) automatically — **never set workflow state by hand** and never offer to; it's handled for you. The triage labels in `triage-labels.md` are a separate layer applied *on top of* that workflow state.

## Conventions

- **Create an issue**: `save_issue` with `team: "Neuroticsasquatch"`, a `title`, and a markdown `description`. Attach it to the Upcoming Movies Tracker initiative/project and set a `milestone` where relevant. **Always set the assignee to the user** (tom@tomboone.com).
- **Read an issue**: `get_issue` by `NEU-###`; `list_comments` for the discussion. Relations (`blocks`/`blockedBy`) don't echo back on write — re-read to confirm they landed.
- **List issues**: `list_issues` filtered by `team`, `assignee`, `state`, `label`, `project`, or `initiative`.
- **Comment on an issue**: `save_comment` with the issue id and a markdown `body`.
- **Apply / remove labels**: `save_issue` with the desired `labels` set — see `triage-labels.md` for the triage role strings. (The MCP input uses `milestone`, not `projectMilestone`.)
- **Close / resolve**: move a ticket to Done/Canceled **only** when no PR would move it automatically (e.g. a research/spike ticket). For anything shipped via a PR, let the connector close it.

House rules that always apply to Linear writes:

- **Assignee/lead → the user** on every create and update.
- **Wire real dependencies** with Linear `blocks`/`blockedBy` relations whenever one ticket genuinely depends on another.
- **`Story` label** on user-story parent issues (keeps the "Ready to start" view filter clean).
- **Split cross-stack stories** into per-stack sub-issues — 1 ticket = 1 PR = 1 repo.
- **Branch per ticket** — use the branch name Linear provides.
- **Footer** on ticket descriptions and comments:

  🤖 Generated with [Claude Code](https://claude.com/claude-code)

## When a skill says "publish to the issue tracker"

Create a Linear issue with `save_issue` (team Neuroticsasquatch, assignee = the user), attached to the Upcoming Movies Tracker initiative/project.

## When a skill says "fetch the relevant ticket"

`get_issue` for the `NEU-###` id, plus `list_comments` for the thread.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a parent Linear issue; **children** are Linear sub-issues.

- **Map**: a parent issue holding the Notes / Decisions-so-far / Fog body. Label it `wayfinder:map`.
- **Child ticket**: a Linear sub-issue of the map (set `parentId`). Label its type `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). On claim, assign it to the driving dev (the user).
- **Blocking**: Linear **`blockedBy`/`blocks`** relations — the canonical, UI-visible representation. A ticket is unblocked when every blocker is Done/Canceled.
- **Frontier query**: `list_issues` for the map's open sub-issues; drop any with an open blocker or an assignee; first in map order wins.
- **Claim**: assign the sub-issue to the driving dev — the session's first write.
- **Resolve**: `save_comment` with the answer, then move the sub-issue to Done manually (research/spike tickets have no PR, so the connector won't move them), then append a context pointer to the map's Decisions-so-far.
