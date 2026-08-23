# Test Log

Manual/integration test cases run against the real target application, kept
separate from the Phase-3+ automated unit test suite (`test_extractor.py`,
`test_health_engine.py`, etc. — not built yet) since these depend on a live
authenticated session and real equipment data.

Each test case gets its own numbered folder: `tc_NNN_short_name/`, containing:

- `description.md` — test number, objective, preconditions, steps, expected result
- `run.py` — the script that performs the test
- `results/` — timestamped output from each run (git-ignored — may contain
  real equipment data from the target application)

## Index

| # | Name | Objective | Status |
|---|------|-----------|--------|
| [TC-001](tc_001_target_connection/description.md) | Target connection & fault extraction | Establish/reuse an authenticated session, reach Device Information, and extract current equipment malfunction status | **PASS** (run 2) — see description.md "Result" |
