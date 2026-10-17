---
name: tasks
description: Keep a step checklist in plan.md for a job that needs three or more tool calls, or when the user asks for a plan or progress.
allowed-tools: write_plan update_plan show_plan
---

## Conventions

- Write the plan before starting the work, with one concrete action per step.
- Mark each step as soon as it is finished, and skip a step with a note instead of silently dropping it.
- Call `show_plan` when you lose track of what remains, and before the final answer to check nothing is left.
- Finish with a short summary of what was done and what, if anything, was skipped.
