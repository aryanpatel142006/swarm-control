# Agent rules for this repo

You are one worker among several autonomous agents. Read `PLAN.md` for the product, `docs/ARCHITECTURE.md` for how pieces fit, `docs/CONTRACTS.md` for API and event shapes, and `docs/DESIGN.md` for UX rules. Your task prompt tells you which of these matter for your task.

- Stay inside your task's Scope. Commit as you go. Never push, never open PRs, never switch branches.
- Run `bash scripts/verify_fast.sh` before you finish. Fix what it reports.
- List every temporary hack (mock, hardcode, placeholder, skipped test, assumption) in your report.
- Record every product or interface decision in your report with its impact.
- Do not edit `PLAN.md`. Frontend tasks may edit `docs/DESIGN.md`; backend tasks may edit `docs/CONTRACTS.md` and must say which consumers are affected.
- If you cannot proceed without a human decision, stop and report `blocked` with a `blocking` question. Nobody is watching a chat window.
- The harness (swarm-control), its prompts, `.swarm/`, `.claude/skills` and the verify scripts are self-improving and owned by the orchestrator agent. Do not edit them from a task. Report anything about them that was wrong, missing or wasted your time in `harness_feedback`; the orchestrator fixes it and tells the humans.
