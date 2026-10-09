# sb-escalation

Escalation runner for the Second Brain control plane (Step 5). An escalation is an issue assigned to the owner by `github-actions[bot]`, so it arrives as a GitHub mobile push. It is re-alerted at the same interval (P1 15 min, P2 1 h, P3 4 h; at most 2 re-alerts; P2/P3 hold for quiet hours, 22:00–07:00 America/Denver). If nobody acknowledges it, the project's work is **parked**.

- **Acknowledge:** the owner comments on the issue (or closes it). Comments from anyone else are ignored.
- **Content is deliberately minimal:** severity, event type, project, task id. No logs, diffs or secrets.
- **Design and evidence:** in the owner's Second Brain vault (`.claude/ESCALATION.md`).
