# Strategy C native-window hold: atomic sidecar publication (closing-review P1)

Prepared 2026-10-30 (Friday, +08:00). All changes are uncommitted, for independent review.

## Starting point

- Checkout: `C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\temp\rebase-check`
- Branch `fix/native-window-hold-rebased`, HEAD `451e5008`, confirmed before writing.
- Uncommitted at the start (all preserved): the four scripts of the second-review fix, `tests/test_native_review_2.py`, and `handoffs/2026-10-30_native-window-review-2-fixes.md`.
- No reset, stash, switch, rebase, merge, fetch, pull, commit or push. No network: every run loaded the `block_network` plugin.

## The finding

**P1: overlapping partial sidecar writes could produce a false READY.** CONFIRMED and RESOLVED.

`price_source.write_cache_source` wrote the published sidecar in place with `Path.write_text`. The sequence:

1. B's parquet is written.
2. B opens and truncates the sidecar.
3. A completes a genuine `write_cache_source` while B's handle is still open.
4. B writes its prefix, including B's `content_sha256`, then fails before `"lines"`.

The file is left as valid JSON carrying B's digest and A's observation dates. `content_sha256` binds the record to the frame, not the record's fields to each other, so B's frame matched it. A's Good Friday observation was then accepted for B's withdrawn bar.

### Reproduction, through the producer and the restart reader

Script: `repro3_torn_sidecar.py` (isolation folder). Log: `2026-10-30_repro-torn-sidecar-before-after.log`.

**How the race is driven.**
- Both writers are the real `price_source.write_cache_source`.
- The only injection is at the file boundary: `io.open`, which both `Path.write_text` and `os.fdopen` call.
- When B makes its first write-mode open, A's genuine `write_cache_source` runs to completion. B then writes up to `"lines"` and raises.

**How the restart is read.** The reader is the production path:
- `run_thematic_rotation._cached_native_observations` (the cache-hit check);
- `compute_signal`;
- `live_targets._rank`, with production `top_k_equal_weight(HEADLINE_K)` and `sleeve_gate_state`, exactly as `live_targets.py` lines 498 to 505 call it;
- `strategy_freshness.native_cache_reach_or_carried` for the freshness verdict.

| | Before (this tree before the fix) | After |
|---|---|---|
| Published sidecar after the overlap | B's digest, A's dates (Good Friday present) | A's whole record; B's temporary file removed |
| Restart: record reused | yes | no (the cache-hit path refreshes) |
| Restart: rank | **READY**, `{"SHY": 1.0}` | HOLD, `{}` |
| Restart: freshness | **current**, 2026-04-17 | unknown |
| Control: B frame with B's own record | HOLD, `{}`, behind 2026-04-02 | unchanged |

The before figures are the same as Codex's.

## The fix

**1. `scripts/price_source.py`: atomic publication.**
- A new `publish_text(path, text)` creates the writer's own temporary file with `tempfile.mkstemp(dir=path.parent, prefix=<name>., suffix=.tmp)`, so concurrent writers never share a name.
- It writes the text in full, then flushes, fsyncs and closes the file, and swaps it in with `os.replace`.
- On any failure it removes only its own temporary file, and only if that file has not already been published, then re-raises. The published sidecar is untouched.
- `os.replace` is retried on `PermissionError` only, at most 5 times at 50 ms intervals. On Windows a reader holding the file open refuses the replace for a moment. When the attempts run out, the writer fails closed.
- `write_cache_source` now ends with `publish_text(path, json.dumps(payload, indent=2))`. The payload is unchanged.

**2. `scripts/native_sessions.py`: the record is bound together.**
- A new `record_digest(rec)` hashes, as canonical JSON (sorted keys, `allow_nan=False`), the fields `parse_sidecar` relies on: `construction`, `through`, `window_days`, `content_sha256` and `lines`.
- `sidecar_record` stores the result as `record_sha256`.
- `parse_sidecar` refuses any record whose `record_sha256` does not match, or that cannot be serialised (for example, NaN).
- This closes the reader side of the same defect. A record not written in one piece is refused however it arose: an old-code writer still running during deployment, a hand edit, or a sync tool.
- Kept as before:
  - the native-column-plus-index binding (`content_digest` is unchanged);
  - the construction check;
  - compatibility with US-column repairs (tested).
- `parse_sidecar` is the only reader of the record. Its callers are `run_thematic_rotation._cached_native_observations` (the cache hit and the refused-fetch fallback) and `strategy_freshness`, so every path is covered.

**3. `.gitignore`: one pattern added, `data/*.source.json.*.tmp`.**
A temporary file is left behind only if the process is killed between creating and replacing it. `daily_live_track.yml` stages all of `data/`, so without this pattern such a file would be committed.

Out of scope and unchanged:
- signal construction, ranking, weights, gates, rebalance rules and other sleeves;
- the seven acknowledged decisions;
- the sibling branch.

## Changed files

Cumulative and uncommitted:

| File | Status | Note |
|---|---|---|
| `scripts/price_source.py` | modified | new in this round |
| `scripts/native_sessions.py` | modified | second-review changes plus `record_digest` |
| `.gitignore` | modified | new in this round |
| `tests/test_sidecar_publication.py` | new, 25 tests | new in this round |
| `handoffs/2026-10-30_sidecar-atomic-publication.md` | new | this file |
| `scripts/strategy_freshness.py` | modified | from the second review, unchanged here |
| `scripts/export_holdings_prices.py` | modified | from the second review, unchanged here |
| `scripts/combined_book_risk.py` | modified | from the second review, unchanged here |
| `tests/test_native_review_2.py` | new | from the second review, unchanged here |
| `handoffs/2026-10-30_native-window-review-2-fixes.md` | new | from the second review; the prompt edit came before this round |

## Regressions: `tests/test_sidecar_publication.py`

- **The reproduced overlap**, driven through the real writer:
  - one whole record is published;
  - a restart gives HOLD, `{}` and unknown;
  - the same holds with no prior sidecar.
- **Controls:** the B pair HOLDs, behind; the matching A pair is reused, READY, `{"SHY": 1.0}`, current.
- **A partial write that fails:** an existing sidecar stays byte-identical; with none, nothing is published. Neither case leaves a temporary file.
- **Replacement failure**, with and without an existing sidecar.
- **`PermissionError` on replace:** a persistent one is retried 5 times and then fails closed; a transient one is retried and the record publishes.
- **Two overlapping complete publications, in both orders:**
  - the temporary names are distinct and in the same directory;
  - the last replace wins whole;
  - the winning record is reused only beside its own frame;
  - the losing frame gives HOLD and unknown.
- **Four threads publishing distinct payloads 10 times each:** no errors, one payload whole, no temporary files left.
- **Every splice of one writer's prefix onto the other's suffix** that is still valid JSON, in both directions and at every byte offset: refused beside either frame, unless it is byte-for-byte one whole record beside its own frame.
- **The mixed record (B's digest, A's dates) placed on disk:** the cache hit refreshes, the rank HOLDs and freshness is unknown.
- **`record_sha256` binds each field:**
  - changing `window_days`, `through` or `content_sha256` is refused;
  - adding A's date under B's line name, with `content_sha256` still matching B's frame, is refused;
  - a missing, wrong or non-string `record_sha256` is refused;
  - NaN is refused.
- **A US-column repair** keeps the record: reused, READY.
- **A source check** that the engine's single sidecar write goes through `publish_text`.

## Validation

Interpreter: `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe`.
Environment: `PYTHONNOUSERSITE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<isolation>\regress-v7\plugin`.
Flags: `-p no:cacheprovider -p block_network --basetemp=<isolation>\temp\bt-p1\...`

**New regressions on the fix: 25 passed.**

**The same file on the pre-fix tree (`temp/before-atomic`): 20 failed, 5 passed.**
- 13 failures reproduce the defect:
  - the torn record, with and without a prior sidecar (two);
  - READY and current on restart after the overlap;
  - an existing sidecar damaged by a failed write;
  - a sidecar created by a failed write;
  - no error raised on replacement failure, twice;
  - the mixed or altered record accepted, six cases.
- 7 failures are missing names only (`ps.time`, `ps.tempfile`, `record_sha256`, `publish_text`).
- The 5 passes are the controls, the US repair, the threaded writers and two field mutations already refused before the fix.
- Log: `2026-10-30_sidecar-tests-on-pre-fix-tree.log`.

**Affected suites: 52 files (every test file naming `price_source`, `native_sessions`, `write_cache_source`, `sidecar`, `strategy_freshness`, `run_thematic_rotation`, `combined_book_risk` or `export_holdings_prices`): 1,937 passed, 1 skipped, exit 0.**
- File list: `2026-10-30_sidecar-affected-files.txt`.
- Log: `2026-10-30_sidecar-affected-suites.log`.

**Full suite: 4,298 passed, 28 skipped, 0 failed, exit 0, 888.07 s.** That is the earlier 4,273 plus the 25 new tests.
- Log: `2026-10-30_sidecar-full-suite.log`.

## Protected state (checked 2026-10-30 10:02 +08:00, Friday)

**`C:\dev\breadth-thrust-etf-sched`**
- main `49fe3261`, 16 modified, 4 stashes;
- the 16 JSONs still equal the verified backup (`compare16`: "all work==backup: True").

**`C:\dev\breadth-thrust-etf-wt-native-window`**
- `fix/native-window-hold` `4308a672`, three modified data JSONs;
- 110 ignored files present;
- SHA-256 prefixes unchanged:

| File | SHA-256 prefix |
|---|---|
| `live_targets.json` | `c67c61e4f3452846` |
| `strategy_freshness.json` | `98ecba0782e80880` |
| `thematic_rotation.json` | `7e216b92a6fef06f` |
| parquet | `e621fb97ab45f079` |
| sidecar | `85d99d007aecbcc1` |

**Writes outside this checkout:** only under `<isolation>\temp\` and the isolation folder's logs and scripts.

## Residual limitations

1. **The parquet and the sidecar are still two files.** Two complete overlapping writers can leave one generation's frame beside the other's record. `content_sha256` then refuses the record, so the result is a refresh, unknown or HOLD, never READY (tested). A lock spanning both files would remove the race; it was not added, to keep the change scoped.
2. **Windows replace refusal.** `os.replace` is refused while another process holds the sidecar open. After 5 attempts (about 0.2 s) the writer raises. The parquet has already been replaced, so the run aborts and the next run refreshes. That is fail-closed, but the in-place write never had this failure mode.
3. **A killed writer leaves a temporary file** (`*.source.json.<random>.tmp`). It is ignored by git and read by nothing, but nothing removes it automatically.
4. **File mode on POSIX.** `mkstemp` creates the file with mode 0600, so on POSIX runners (GitHub Actions) the sidecar becomes readable by its owner only. The same user reads it, and it is gitignored.
5. **No directory fsync.** After a power loss a POSIX filesystem may hold either the old or the new sidecar, but never a mixture.
6. **One-off refresh.** `record_sha256` makes every existing sidecar fail validation once. That is the same refresh the second-review digest change already forces, so there is no extra operational cost.
7. **Other in-place writers are unchanged.** `freeze_btc_proxy_history`'s frozen sidecar (written once) and the many JSON outputs written with `write_text` (`live_targets.json`, `strategy_freshness.json` and others) were not converted.
8. **The cache-hit path reads the sidecar twice** (`run_thematic_rotation.py` lines 733 and 745). If the sidecar is replaced in between, the second read yields either None (HOLD) or a genuine record for the same frame. That is fail-closed, so it was left unchanged.
9. **Operational follow-ups:** rebuild the saved combined-book risk snapshot after its calculator change, and note the SSE/XSHG projection caution for sibling integration. Both are left for authorised follow-up, as instructed.

## Artefacts (isolation folder)

- `repro3_torn_sidecar.py` and `2026-10-30_repro-torn-sidecar-before-after.log`
- `2026-10-30_sidecar-tests-on-pre-fix-tree.log`
- `2026-10-30_sidecar-affected-files.txt` and `2026-10-30_sidecar-affected-suites.log`
- `2026-10-30_sidecar-full-suite.log`
- `temp/before-atomic/`: a copy of this tree's `scripts/` taken before this round, plus the new test file, used for the before runs

## Next review prompt

Review the uncommitted changes on `fix/native-window-hold-rebased` (HEAD `451e5008`) in
`C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\temp\rebase-check`
as an independent, adversarial reviewer. This round closes your closing-review P1 (overlapping partial
sidecar writes giving a false READY). The new changes are in `scripts/price_source.py`
(`publish_text`), `scripts/native_sessions.py` (`record_digest`, `record_sha256` in `sidecar_record`
and `parse_sidecar`), `.gitignore`, and `tests/test_sidecar_publication.py`. Context is
`handoffs/2026-10-30_sidecar-atomic-publication.md`; the earlier changes are as you reviewed them.

Read-only: do not edit, commit, stash, reset, rebase, fetch, pull or push. Do not switch or write
`C:\dev\breadth-thrust-etf-sched` (16 modified JSONs) or `C:\dev\breadth-thrust-etf-wt-native-window`
(three modified data JSONs and ignored caches). Run nothing that reaches the network.

First re-run your own P1 reproduction (the real writer with the injected partial write, then the real
restart reader) and say whether it is closed. Then try to break the fix:
- publish_text: any path on which a failed write, a failed fsync, a failed replace or an exception
  after replace damages the published sidecar, removes another writer's file, or leaves a temporary
  file behind; whether concurrent writers can ever share a temporary name; whether the
  PermissionError retry can mask a real failure or publish stale content.
- record_sha256: any mixture of two records, or any edit to a field parse_sidecar uses, that is still
  accepted; any genuine record now refused; whether US-column repair compatibility and the
  native-column-plus-index binding are intact.
- Any remaining path to READY or current with native provenance that does not belong to the frame in
  hand, including two complete overlapping writers and the two sidecar reads in the cache-hit path.
- Confirm nothing changes signal construction, ranking, weights, gates, rebalance rules or other sleeves.

Run tests with `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe`, PYTHONNOUSERSITE=1,
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONDONTWRITEBYTECODE=1,
PYTHONPATH=<isolation>\regress-v7\plugin, flags `-p no:cacheprovider -p block_network
--basetemp=<your own folder>`. Results to verify, not trust: test_sidecar_publication.py 25 passed
(20 fail on the pre-fix tree, 7 of them for missing names only); affected suites 1,937 passed,
1 skipped; full suite 4,298 passed, 28 skipped, 0 failed.

The seven acknowledged decisions, the SSE/XSHG projection caution and the risk-snapshot rebuild are
known and are not findings unless you show a live defect. Mark each finding BLOCKING (a wrong live
target, a wrong freshness verdict, a crash, or a fail-open path) or FOLLOW-UP (hardening that can
land after the commit).

Output: findings ranked by severity, each with file:line, the triggering input or state and the wrong
output, marked CONFIRMED (reproduced) or PLAUSIBLE and BLOCKING or FOLLOW-UP; then a one-line verdict:
SAFE TO COMMIT, or NOT SAFE with the blocking finding numbers. No contractions, British spelling.
Modify no file.
