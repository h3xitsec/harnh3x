---
name: python
description: Run a throwaway Python snippet to compute a number, date, statistic, or data transform. Not for programs the user keeps; use coding.
allowed-tools: run_python
---

## Conventions

- Only the standard library is available. Do not import third-party packages.
- Compute instead of estimating: when a number matters, run the code and report the printed value.
- Use it for CSV, JSON, or text that is too large or too fiddly to transform by hand, and for anything a blocked shell pipeline would have done.
- Keep snippets small and self-contained. If a snippet fails, read the traceback, fix it, and run it again.
- Report the exit status and show the output to the user.
