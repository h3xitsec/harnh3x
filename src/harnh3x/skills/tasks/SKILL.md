# Tasks

Keep a checklist in plan.md for work that takes several steps.

## When to Use

- A request needs three or more tool calls across different skills
- The user asks for a plan, a checklist, or progress on a longer job

## Available Tools

- `write_plan` — Replace the plan with a numbered list of steps
- `update_plan` — Mark a step `done`, `todo`, or `skipped`, with an optional note
- `show_plan` — Return the plan and how many steps are done

## Conventions

- Write the plan before starting the work, with one concrete action per step.
- Mark each step as soon as it is finished, and skip a step with a note instead of silently dropping it.
- Call `show_plan` when you lose track of what remains, and before the final answer to check nothing is left.
- Finish with a short summary of what was done and what, if anything, was skipped.
