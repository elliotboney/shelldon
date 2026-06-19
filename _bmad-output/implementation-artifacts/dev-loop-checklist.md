# Dev-loop review self-checklist

**Purpose:** the recurring review-finding classes, captured once so the dev self-applies them **before requesting review** — instead of review catching the same things every story. Born from Epic 3 action #5 (which didn't stick as prose) and Epic 4's retro (same classes recurred in 4.1, 4.4, 4.5, 3.4).

**How to use:** load this at story-build (create-story / dev-story). Tick each before flipping a story to `review`. If an item genuinely doesn't apply, say why in the story's Dev Notes.

## Resilience / error handling
- [ ] Every **best-effort path** (history write, memory apply, file read, recall) is wrapped `try/except + log + skip` — **never raises** into the turn loop (mirror `_record_turn` / 4.1).
- [ ] Catch the **real** exception types: `(OSError, UnicodeError)` on file reads, `EOFError`/`OSError` on transport (4.4, 4.5 both missed these first pass).
- [ ] Any **longer-lived await** (reading a completion, a socket) has a timeout backstop — no path that can block forever (4.5 worker-wedge).
- [ ] Counts/sizes are **capped with a logged overflow** — no silent truncation.

## Tests assert the real thing
- [ ] Assertions check **real values, not truthiness** (`== 0o700`, not `assert mode`).
- [ ] No **false-positive masking** — verify the unique token isn't matched by an example/system-instruction string; add a **negative check** (4.4 CAP-6 false positive).
- [ ] **Ordering / sequencing guarantees** are enforced by a spy, not assumed (4.5 "ops applied AFTER reply").
- [ ] **Rejection paths** are tested (invalid/empty/duplicate/out-of-range op → no write, turn survives) (3.4 missing these).

## Inputs & safety
- [ ] Input/range guards present (empty, whitespace-only, `lo == hi`, oversized).
- [ ] Path/Unicode safety: human-facing names Unicode-preserving + path-traversal rejected; internal keys ASCII-only (4.2 `_safe_filename`, 4.3 vault keys).

## Isolation (same change!)
- [ ] Any new core **file-write default path** is redirected in the conftest autouse fixture **in this same change** — never discovered in verify (Epic 3 action #3; held all of Epic 4 — keep it).

## Hygiene
- [ ] No **WHAT-comments** (comment the why, not the what).
- [ ] Shared test helpers live in **conftest**, not duplicated per file.

## Before flipping to `done`
- [ ] Isolation + error-handling sweep complete (the 4.4 "mis-marked done" gap).
- [ ] Tests assert real values and exercise rejection/failure branches — not just the happy path.
- [ ] Full suite green; import-linter contracts **KEPT**.
