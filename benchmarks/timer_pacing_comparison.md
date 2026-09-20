# Interruptible timer pacing

The new timer makes a pending pacing wait cancellable while retaining the
existing deadline correction. In this timer-only comparison, 60/120/240 FPS
cadence stayed close to baseline and median trial p95 lateness stayed below
0.57 ms. At 1 FPS, cancellation during a wait returned in about 0.05 ms instead
of waiting for the remaining roughly 950 ms. This is a responsiveness change,
not a demonstrated CPU optimization.

## Reproduction and provenance

Measured on September 17, 2026, using Python 3.14.3 on Windows 11 build 26220,
Ryzen 9 5900X (24 logical processors). The separate capture checks used an
RTX 3060 Ti. No OpenMP settings were changed.

- Baseline: `753b1a5b5550678f6d4cc444b62332bfb5498d2c` (dev after PR #150).
- Candidate implementation: `d83bdec97df2dbd86a135a6c32f4bd128b723742`.
- Baseline timer SHA-256: `37d93822b4cb4898e36039f908b1fc31889dba16e83998279b3800deeb90774d`.
- Candidate timer SHA-256: `df4c9dd45f2c22e8df447d71d7f60fa886349f9290dab71949b6ea8f79d83df9`.
- Comparison helper SHA-256: `d296d78e3af554231d7b9c056a27c005afc1082e83d4260744b5d3b166903306`.

Export the baseline into a separate directory, then run from the candidate
checkout with its Windows Python environment:

```powershell
git archive --format=zip --output=.test/timer_baseline.zip 753b1a5b5550678f6d4cc444b62332bfb5498d2c
Expand-Archive .test/timer_baseline.zip .test/timer_baseline
.venv\Scripts\python.exe -I benchmarks/compare_timer_pacing.py --baseline-root .test/timer_baseline
```

Use an empty destination for a new export. The helper imports each timer source
directly, without loading capture backends or compiled processors. An optional
`provenance.json` in an exported source root can record its `git_head`; the exact
timer and helper hashes are recorded regardless. The raw local result is
`benchmarks/results/timer_pacing.json` (ignored by Git).

There were three paired repeats, with randomized baseline/candidate order and
shared randomized FPS order within each pair. Each trial had 0.2 s warmup and
2 s measurement after rearming. Six fresh worker processes completed all 18
pacing trials and 12 cancellation probes without errors or source changes.
Raw samples include pre-wait deadlines, wait start/finish times and subsequent
deadlines. Effective FPS uses actual elapsed time, including the last wait if
it crosses the measurement boundary. There were no early returns or deadline
resynchronizations in the pacing trials.

## Cadence and resource use

Entries are medians of three trial statistics. A median of trial percentiles
is not a percentile pooled across all ticks. Absolute interval error compares
successive returns with the requested period. Lateness compares each return
with that wait's scheduled deadline.

| Target FPS | Implementation | Effective FPS | p95 absolute interval error (ms) | p95 lateness (ms) | p99 lateness (ms) | Process CPU, % of one core |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 60 | Baseline | 59.994 | 0.419 | 0.510 | 0.592 | 0.00 |
| 60 | Candidate | 59.997 | 0.448 | 0.566 | 0.634 | 0.78 |
| 120 | Baseline | 119.980 | 0.441 | 0.518 | 0.626 | 0.00 |
| 120 | Candidate | 119.980 | 0.452 | 0.517 | 0.613 | 1.56 |
| 240 | Baseline | 239.939 | 0.478 | 0.515 | 0.613 | 0.00 |
| 240 | Candidate | 239.943 | 0.493 | 0.524 | 0.657 | 0.78 |

Across individual candidate trials, effective FPS was 59.984–59.998,
119.975–119.997 and 239.935–239.997. Candidate trial p95 lateness ranged from
0.526–0.567 ms at 60 FPS, 0.513–0.528 ms at 120 FPS, and 0.506–0.539 ms at
240 FPS. These short results support keeping the native high-resolution timer;
they do not prove timing equivalence on other machines or under contention.

CPU includes timer calls and sample bookkeeping. The Windows process CPU
measurements advanced in coarse increments here: a 0.00% reading does not mean
zero work. Baseline trials ranged from 0.00–0.78% of one logical core; candidate
trials ranged from 0.00–1.56%. The candidate adds Python locking, ctypes calls
and an event, so a small overhead is plausible, but these two-second samples
cannot establish its precise size. No GPU, power or resident-memory comparison
was performed.

Each paced worker now owns two native handles (timer and cancellation event).
A separate warmed-up create/configure/cancel/close probe completed 1,000 cycles
with the process handle count unchanged at 551. This checks the tested normal
cleanup path; injected API failures are covered separately by unit tests.

## Cancellation

Each fresh process ran two 1 FPS probes. The during-wait probe requested
cancellation after call entry and a nominal 50 ms delay. It verifies that the
call was still pending, but does not instrument the native wait itself. The
before-wait probe cancels before launching the waiting thread, so its overall
latency includes thread startup.

| Probe | Implementation | Median cancel-to-return (ms) | Range (ms) |
| --- | --- | ---: | ---: |
| During wait | Baseline | 949.381 | 942.520–950.190 |
| During wait | Candidate | 0.050 | 0.047–0.051 |
| Before wait | Baseline | 1000.251 | 1000.091–1000.359 |
| Before wait | Candidate | 0.183 | 0.156–0.189 |

The candidate's actual `wait_for_timer()` call after prior cancellation took
0.0035–0.0111 ms; the remainder was primarily thread startup. These figures end
when the timer helper returns, not when a whole camera finishes stopping.

Separate real-camera checks started and stopped the same camera three times
per backend at 1 FPS, before its first tick was due:

| Backend | Whole-camera stop range (ms) | Blocked reader awakened | Timer handles closed |
| --- | ---: | --- | --- |
| DXGI | 0.348–0.435 | 3/3 | 3/3 |
| WinRT | 0.319–0.391 | 3/3 | 3/3 |

Both backends also passed concurrent capture with four readers, fresh-frame
filtering, destination preservation on timeout, and restart at 0/120/240 FPS.
Stopping both cameras in that separate check took 13.4–15.1 ms with explicit
native acquisition waits enabled. Native capture calls already in progress
still have to return; timer cancellation alone does not bound every shutdown.

## Design and validation

The timer uses a one-shot relative waitable timer and a separate manual-reset
event. `WaitForMultipleObjects` places cancellation first, so cancellation wins
when both handles are signaled. Stopping signals the event; only the worker
closes the handles after its wait has returned. Cancelling a waitable timer
alone would not wake a pending waiter. These choices follow Microsoft's
[multiple-object wait contract](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects),
[event semantics](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createeventw)
and [timer cancellation contract](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-cancelwaitabletimer).

The negative due time is rounded up in 100 ns units, using a one-shot period of
zero. This is the API's time representation, not a guarantee of 100 ns wake-up
accuracy. The timer requests the high-resolution flag and retries without it
when older Windows rejects the flag with `ERROR_INVALID_PARAMETER`.
See [SetWaitableTimer](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-setwaitabletimer),
[CreateWaitableTimerExW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createwaitabletimerexw)
and [CPython's Windows sleep implementation](https://github.com/python/cpython/blob/v3.14.3/Modules/timemodule.c#L2297-L2333).

The full local suite passed 808 tests. New tests cover FPS validation before
startup side effects, cancellation around creation/arming/waiting/closing,
restart, native API failures, waiter notifications, error preservation, and
safe resource retention when the bounded join fails. Ruff, ty and the pdoc
build also passed. Local hardware checks used Python 3.14.3 only; older Windows
fallback behavior is mocked, not tested on an older Windows installation.
