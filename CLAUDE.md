# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

> **This repo has no broker credentials and places no orders.** It owns strategy/indicator logic,
> backtesting, screening, and the AI shadow-judgment research pipeline — shared by `kr-trading-bot` (KIS)
> and `kr-trading-bot-toss` (Toss), which pip-install it. See `docs/planning/` for the split rationale.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding
**Don't assume. Don't hide confusion. Surface tradeoffs.**
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.
- **Conflict Resolution:** If instructions seem to conflict, default to §3 (Surgical Changes) — do less, not more. Ask for clarification on ambiguous business decisions; resolve clear technical errors independently.

## 2. Simplicity First
**Minimum code that solves the problem. Nothing speculative.**
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines *for the new implementation* and it could be 50, rewrite it (Do not rewrite adjacent existing code).

## 3. Surgical Changes
**Touch only what you must. Clean up only your own mess.**
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

## 4. Goal-Driven Execution
**Define success criteria. Loop until verified.**

Transform vague tasks into verifiable goals:
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```
Weak criteria ("make it work") require constant clarification. Strong criteria let you loop independently.

## 5. File Header Comments in Korean
**First line of every new source file: a one-line Korean comment stating its role.**
- Python: `# KIS API 호출을 비동기로 래핑하는 클라이언트`
- Skip config files.

## 6. Plan + Checklist + Context Notes
**Before any non-trivial task, produce three artifacts.**
- **Plan**: What and why.
- **Checklist (checklist.md)**: Concrete tasks.
- **Context Notes (context-notes.md)**: Decisions and reasoning.

If the user provides a plan and asks you to start coding immediately, ask: *"Should I create the checklist and context notes first?"* **(Exception: Proceed immediately if the owner explicitly says "Skip documents" or "Skip artifacts".)**

## 7. Run Tests Before Marking Complete
**If you touched code, run the tests before saying "done".**
- Run proactively — before the user signals done, not after.
- This is the step LLMs skip most often. Treat it as non-negotiable.
- This repo is a pip-installed package consumed by `kr-trading-bot`/`kr-trading-bot-toss` — after any change
  here, re-verify their import chains still resolve too (they pin a git tag; a local `pip install -e .` in
  each sibling repo lets you test against your in-progress changes before tagging a release).

## 8. Semantic Commits
**Commit when one logical change is complete.**
- Tag a release (`git tag vX.Y.Z && git push --tags`) when a change should propagate to the ops repos —
  they pin an exact tag in `requirements.txt`, so committing alone does not update them.

## 9. Read Errors, Don't Guess
**Read the actual error/log line. Don't pattern-match from memory.**
- Read the full error message and stack trace.
- Don't apply "common fixes" before confirming the cause.
- LLMs guess from error keywords and apply the most-recent-pattern fix. That's how a one-line bug becomes a three-file refactor.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## 10. Package Structure
- `src/kr_research/{trading,core,bot}/` — the installable library (`pip install git+https://github.com/payak95/kr-trading-research.git@<tag>`). Import as `kr_research.trading.X` / `kr_research.core.X`.
- `tools/*.py` — cron/daemon entry-point scripts (NOT part of the installed package — run from a checkout of this repo, e.g. `python tools/screen_universe.py`). They import the library via `kr_research.*` and each other via flat `tools.*` (matching the sibling repos' existing script convention).
- `tests/` — mirrors both.
- No broker client, no `main.py`, no live order path — if a change needs KIS/Toss credentials, it belongs in `kr-trading-bot`/`kr-trading-bot-toss`, not here.

## 11. Markdown Authoring Rules
- UTF-8 encoding (no BOM), LF line endings.
- Location is `docs/`; naming is policy docs = `UPPERCASE_SNAKE.md`, working docs = `lowercase-kebab.md`.
- Temporary artifacts (`checklist.md`·`context-notes.md`) are deleted when the task completes.
- **Keep docs in sync with reality**: when you change code, structure, or policy, update the owning doc in the *same commit*.
- **This file (CLAUDE.md) is English-only**; all other docs are written in Korean.

## 12. Cross-machine Memory
This repo syncs across computers via git, but Claude Code's local memory (`~/.claude/.../memory`) is
**per-machine and does not sync**.
- **Record project-wide lessons, decisions, and pitfalls in `docs/planning/PROJECT_MEMORY.md`** (in the repo, synced).
- **Consult `docs/planning/PROJECT_MEMORY.md` before starting work.**
