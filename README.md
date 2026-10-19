# Cycle Control

A desktop dashboard for your Linear cycle that starts Claude Code builders in their own git worktrees, runs several in parallel and lets you answer any of them in an embedded terminal.

## Run

```bash
cp .env.example .env
uv run main.py            # window
uv run main.py --demo     # fake Linear data and fake builders, no keys needed
uv run main.py --no-window  # serve http://127.0.0.1:8770/ for a browser
```

Paste a Linear personal API key on first launch (Linear, Settings, Security and access, Personal API keys). It is saved to `.env`.

## What it does

- Shows the current cycle for the team in `LINEAR_TEAM_KEY`, plus every open issue assigned to you, marked off-cycle when it is not in the cycle. Mine and Team filter. Refreshes every two minutes.
- Start on an issue runs `git fetch origin develop`, then `git worktree add ~/code/spp-<n> -b <branch> origin/develop` (reused if it exists), moves the issue to In Progress and opens `claude --agent spp-builder` there. Plan-first stops after the plan and waits for you. Autopilot carries on.
- Each builder is a row: dot (working, waiting on you, done), phase, latest line. Waiting rows sort first and send a Mac notification.
- Open a row to see its plan, gates and review badges, PR link and its live terminal. Type into the terminal to answer or approve.
- Resume reopens a builder in an existing worktree with `claude --continue`.

Builders run with `--dangerously-skip-permissions` by default so they can run unattended. Set `SKIP_PERMISSIONS=false` to approve each tool call instead. The `spp-builder` hooks still block merging, `mergequeue` and bare `--force`.

Builders write `spp-phase.json`, `spp-plan.json`, `spp-gates.json` and `spp-review.json` into the worktree's git dir, which is where the phase, plan and badges come from.

## Teams

`teams.json` maps a Linear team key to its repo, GitHub repo, builder agent, base branch and worktree prefix. Only teams listed there can be started; Start is disabled, with a tooltip, for every other team.

```json
{ "PP": { "repo": "~/code/street-partner-platform", "github": "AgentSoftware/street-partner-platform", "agent": "spp-builder", "baseBranch": "develop", "worktreePrefix": "spp" } }
```

## Sub-issues and stacks

A parent with sub-issues shows as one collapsible row under Stacks, with one strip segment per child. Order comes from Linear blocked-by relations, falling back to `stack[].after` in the parent builder's `spp-plan.json`.

- A child with no blockers starts from the base branch.
- A child with one blocker starts from that blocker's PR branch once the PR is open, or from the base branch once it is merged.
- A child with several blockers waits until all are merged.
- Start ready on the parent queues every ready child in order, each with its base branch.

At most `MAX_BUILDERS` (3) builders with containers run at once. A builder at the `human` phase has run `just worktree-down` and does not count. The rest queue and start as slots free up. The queue survives a restart.

## Review comments

Every `COMMENT_POLL_SECONDS` (300) the app runs one `gh api graphql` call per open builder PR for unresolved review threads, top-level comments and review bodies, including CodeRabbit's. Nothing is spent unless something is new: the last-seen items are kept per PR in `~/.cycle-control/comments.json`.

- New feedback is written to `spp-feedback/<time>.md` in the builder's git dir, and the builder is told to read it. It follows the Review comments rules in `spp-builder.md`.
- If the builder is busy, the feedback waits and is delivered when it is idle.
- If the builder's session has ended, it is resumed with `claude --resume <session id>` (re-provisioning the worktree first if it was never provisioned). If that fails, a new `spp-builder` is started in the same worktree with the PR number.
- The builder records each item with `comment.sh`. The lane shows "addressed N · M needs you"; click it for each comment, the action and the reply. A needs-you item shows the drafted reply to edit and post, or dismiss.
- The builder's replies end with `<!-- spp-builder -->`, so they are never delivered back as new feedback. Set `AUTO_COMMENTS=false` to turn polling off.

## Approval, ready to merge and re-queue

Our convention: an approval with comments means address the feedback, then it is ready to merge. A reviewer approves when their latest review is APPROVED, or COMMENTED after they approved; only a latest CHANGES_REQUESTED blocks. The feedback file tells the builder which case applies, so it re-requests review only when the PR is not approved.

When the PR is approved, every thread is resolved, CI is green and no needs-you item is left, the lane shows **ready to merge** and you get one notification. The app never adds `mergequeue` and never merges.

If you applied `mergequeue` and the merge bot added `blocked`, the app runs `~/.claude/spp/requeue.sh` itself once the PR is approved, every thread is resolved and CI is green (once per commit, and only while the builder is idle). The builder is also told to run it after a batch. Set `AUTO_REQUEUE=false` to stop the app doing it. The script re-queues only if you applied the label, the PR is approved, every thread is replied to and resolved, the fixes are pushed and CI is green. The lane then shows **re-queued**. If any condition fails it does nothing, you are notified, and the lane shows **ready to merge** or **re-queue refused**.

Posting a drafted reply from the app adds the `<!-- spp-builder -->` marker and marks the thread seen, so it is never handed back to the builder.

## One builder per worktree

Each running builder holds `spp-builder.lock` in the worktree's git dir. A new builder is refused while the lock holder is alive, or while any other `claude` process has the worktree as its working directory. The resume fallback starts a new `spp-builder` only after confirming the old process is gone.

## Rebase on a moved base (off by default)

With `AUTO_REBASE=true`, every `REBASE_POLL_SECONDS` the app compares each stacked builder's base branch with the sha it was built on. If it moved or its PR merged, it replays the builder's commits in a scratch worktree. A clean result becomes an instruction to the builder (rebase `--onto`, retarget the PR, fast gates, `--force-with-lease`). A conflict is never given to an agent: the lane shows Needs you, with an "I resolved it" button.

## Develop

```bash
uv run pytest
uv run ruff check .
```
