"""UpdatePilot — dependency update bot whose every PR arrives pre-reviewed.

Two jobs, one bot:

1. **scan** — read the manifests, ask the registries what changed, and open update
   PRs under the repository's own policy (exact pins respected, majors isolated,
   minor/patch grouped).
2. **review** — run a full-file AI review over every PR it just opened, before the
   human ever sees it: complete file contents, a coverage table, and deterministic
   downgrade rules the model cannot argue with.

The bot never merges and never approves. It brings the proposal to your door with
its own homework already done; the merge button stays yours.
"""

__version__ = "1.4.6"
