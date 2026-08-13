# Phase 4, Step 10 — full regression

**Status:** run and green. This document records what was executed and what it
established. It adds no code and changes no behaviour.

Step 10's definition is a verification step, not an implementation one: run the
Phase 4, Phase 3, Obsidian, memory, knowledge and permission/security suites,
then the complete suite. Nothing here required a production change, and none
was made.

---

## 1. Results

Every group run separately, then the whole suite. Browser groups are run one at
a time on purpose — `test_repeated_cycles_do_not_accumulate_processes`
enumerates Chromium PIDs globally, so two concurrent browser runs make it
report another run's processes as a leak.

| Group | Tests | Result | Warnings |
|---|---:|---|---:|
| Phase 4 — browser | 345 | passed (see §2) | 1 |
| Phase 3 — computer | 171 | passed | 1 |
| Obsidian | 183 | passed | 1 |
| Memory | 92 | passed | 5 |
| Knowledge | 75 | passed | 0 |
| Permission / security | 73 | passed | 1 |
| Core (orchestrator, providers, tasks, tools, database failures) | 87 | passed | 0 |
| **Complete suite** | **1026** | **1026 passed, 0 failed, 0 skipped** | 5 |

The seven groups sum exactly to 1026, so every test in the repository belongs
to one of them and none was counted twice or missed.

The 5 warnings are pre-existing Starlette deprecations
(`HTTP_422_UNPROCESSABLE_ENTITY`, and `httpx` under `starlette.testclient`).
They are library-level and unrelated to JARVIS.

Chromium processes: **0 before every group, 0 after every group, 0 at the end.**

## 2. One non-reproducing failure set, diagnosed rather than dismissed

The first Phase 4 group run reported **3 failed, 342 passed**. The same code
then passed four subsequent runs, including an exact re-run of the same file
ordering. The failures are recorded here with their cause rather than quietly
dropped.

**What failed:** `test_a_later_turn_does_not_inherit_the_previous_turns_taint`
and `test_within_one_turn_the_taint_does_carry` (the first two tests of the
first browser file in that ordering), and
`test_a_permitted_action_records_the_authority_it_acted_under`. Two carried
`ToolTimeoutError: Tool 'browser_open' exceeded 5.0s`.

**Why 5 seconds.** That is a *test* setting — `conftest.py` sets
`tool_timeout_seconds=5.0` so a hung tool fails a test quickly. Production
defaults to 30. `browser_open` is the call that launches Chromium the first
time, so it is the one tool call in the suite whose duration depends on process
startup rather than on JARVIS.

**Reproduction attempts, all on the same commit:**

| Run | Result |
|---|---|
| Complete suite | 1026 passed |
| `test_browser_agent_integration.py` alone | 12 passed |
| `test_browser_agent_integration.py` alone, again | 12 passed |
| **Exact failing group ordering, quiet machine** | **345 passed** |

**Classification: environment, not defect.** A cold Chromium launch exceeded a
deliberately tight test-only timeout while the machine was busy. No product
code is implicated: the same ordering passes when nothing else is competing for
CPU, and the production timeout is six times larger.

**KNOWN LIMITATION (new, recorded here):** the browser suite is sensitive to
machine load, because the first `browser_open` in a process must launch
Chromium inside a 5-second test budget. Running browser suites concurrently
with other heavy work can produce spurious timeout failures. The mitigation is
operational — run browser suites one at a time — not a code change, and
loosening the timeout would weaken a useful guard against genuinely hung tools.

## 3. What Step 10 did not change

No production code. No test was modified, removed, or converted from real
Chromium to a mock. No Step 9 result was rewritten.

Every limitation carried into Step 10 remains exactly as documented:
cross-turn taint; `Confirmation.body` retaining the pending value;
`ActivityService.record` swallowing database errors; browser `_audit` being
stricter than that global contract; the shared `Tool` singleton test-order
pollution in `test_disabled_tools_are_not_advertised`; the SSE socket layer
being untested; and the thin `cross_page` mutation coverage.

None of them was fixed, and none is claimed to be.

## 4. Platform

- **Linux — VERIFIED.** Everything above executed here.
- **Windows — UNVERIFIED.** Nothing was executed on Windows.
- **macOS — UNVERIFIED.** Nothing was executed on macOS.
