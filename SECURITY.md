# Security boundaries

- Modern task API routes require a bearer token. Bind to loopback unless deployed with appropriate transport protection.
- Page text, Skill text and remote annotations are untrusted model context. Local policy and operator-authored outcome contracts remain authoritative.
- The modern executor exposes only backend allowlisted tools, without shell/filesystem access for the actor.
- Checkpoints do not atomically commit external effects. Missing action results become unknown and are not blindly replayed.
- Automatic Skill promotion is scoped to repeated local fixture validation. It does not authorize unattended messaging, purchases or other real-device actions.
- Task acquisition drafts locally by default; use `task.learn: false` to opt out. Evaluation runs disable acquisition. Generated drafts remain inactive until evaluated publication; rule-based eligibility does not prove general usefulness.
- Automatic post-task optimization requires an operator-supplied suite and explicit budgets. Interrupted search stages are not silently replayed; inspect their evidence and use a fresh acceptance suite for a new attempt.
- Contracts verify only the fields they cover and depend on the observation source. They do not prove arbitrary GUI success or user intent.
- Legacy chat/shell integration is outside modern tool isolation. Do not expose it publicly as though it had the modern API contract.
- Keep keys, databases, checkpoints, screenshots and private Skill history out of Git. Use private security reporting when available; do not put secrets in public issues.
