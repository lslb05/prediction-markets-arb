# Repository instructions for Codex

## Goal

Implement only the concrete task delegated by Claude Code or the user. Read
`CLAUDE.md` for the project contract and do not fill unresolved product or
architecture decisions by guessing.

## Stack and layout

- Python 3.12 managed with `uv`. Production code lives in
  `src/prediction_markets`; tests in `tests`.
- TypeScript under `trader/`, strict `tsc` and vitest. It is a separate
  program, not a binding: it reaches the Python half only through the
  `core.matched_markets` table.
- Notebooks may import production code, never the reverse.
- Use Ruff, strict mypy, pytest, Pydantic and Polars.
- Configuration comes from environment variables. Never commit secrets, and
  never commit a `.pem` -- Kalshi issues the key once and it cannot be
  re-downloaded.
- `arbitrage/pricing.py` and `trader/src/strategy/spread.ts` are the same
  function in two languages, kept in step by shared test vectors. Changing one
  without the other is a defect even when both suites pass.

## Working rules

- Preserve user changes and keep the diff inside the delegated scope.
- Avoid unrelated refactors, renames and dependency additions.
- Validate external input and handle network, file and API failures explicitly.
- Never use workplace data, institutional credentials or private methodologies.
- Explain uncertainty instead of inventing repository facts.

## Style

This is public portfolio code, read by people. Restraint is the standard.

- Comments explain *why*, never *what*. Delete any comment that paraphrases the
  line below it; if a line needs paraphrasing, rename the variable instead.
- Docstrings only where the signature is not enough: modules, public classes,
  functions with a non-obvious contract (units, timezone, side effects, raises).
  A three-line private helper with a good name needs none.
- No banner comments, no decorative sections, no bare `# noqa`.
- Tests assert behaviour that can break. `assert __version__ == "0.1.0"` breaks
  on every bump and protects nobody. Cover boundaries instead: malformed input,
  limits, timezones, external failure.
- One test per behaviour, not one per line. Coverage is a consequence of testing
  what matters, never a target to inflate.
- Short test names: `test_rejects_future_date`, not
  `test_should_raise_value_error_when_the_date_is_in_the_future`.
- Never catch an exception you cannot handle. `except Exception` that logs and
  continues hides bugs; handle the concrete failure at the boundary.
- Do not abstract for a single caller.
- No emoji, no decorative prints, no narrating logs.

## Commit authorship

Every commit has a single author and committer: the repository owner.

Never add `Co-Authored-By:` lines, session trailers, "Generated with" notes, or
any mention of Claude, Codex or other tooling — not in the subject, body or
trailers. Do not change `user.name` or `user.email`.

## Verification

Run the narrowest relevant test while iterating, then run:

```bash
uv run python scripts/check.py
```

In the final response, report the outcome, touched files, checks performed and
residual risks. Do not claim a check passed unless it was executed.

## Code Review Rules

Prioritize correctness, data integrity, time-series leakage, silent external
failures, secrets and missing tests. Ground each finding in a file and line.
Review only; do not apply fixes unless the task explicitly requests a fix.

