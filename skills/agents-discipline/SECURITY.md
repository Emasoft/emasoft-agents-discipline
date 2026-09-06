# Security model

agents-discipline executes repository-described checks. Its safety boundary is explicit review and approval, not command sandboxing.

## `CHECK:` lines are code

`gate-check.mjs` runs each `CHECK:` through a shell with the checker's user permissions and inherited environment. A command can access files, network connections, credentials, and developer tools available to that process.

Before using an inherited ledger:

1. Run `node <skill-dir>/scripts/gate-check.mjs --status <gate-file>` to parse and display status without executing checks.
2. Read every `CHECK:`, `EXPECT:`, and `CWD:`. Inspect any script called by a check, including generated or ignored files.
3. Determine the shell from `--shell`, `AGENTS_DISCIPLINE_SHELL`, or the platform default, and inspect the inherited `PATH`. For a new oracle with no exact approval, normal mode prints the resolved values without running it. Normal mode is not a universal dry run because an existing exact approval permits execution.
4. Run with `--approve` only when the complete resolved oracle is expected and understood.

Approval records live under `~/.agents-discipline/approved` by default. `AGENTS_DISCIPLINE_APPROVAL_DIR` may select another directory only when it is a real, owner-private directory whose canonical target is outside the canonical repository root. The checker rejects symlinked stores and accepts a record only through a no-follow descriptor that still names the same owner-private, single-link regular file after reading. An approval is specific to the absolute ledger and gate, exact command and expectation, resolved working directory and shell, timeout, output and regex limits, regex startup/concurrency limits, platform, and full inherited `PATH`. A change to any bound input requires review and approval again. An approval is consent to execute; it is not evidence that the command matches the English gate title.

Approval does not snapshot files that a command invokes. If a referenced script, generated file, executable, fixture, or dependency changes while the approved command text remains the same, the old approval can still authorize the changed bytes. Inspect those dependencies again before running the command. Automatic ledger evidence carries a separate environment-independent digest of parsed `CHECK:`, `EXPECT:`, and raw `CWD:`; `--status` rejects stale or unbound definitions without executing, but does not revalidate artifacts. The digest is unkeyed and therefore detects definition drift rather than authenticating a result against a ledger editor. Run `--reverify` after dependency or input changes. When a workflow needs machine-enforced dependency currentness, put the expected dependency digests directly in approval-bound `CHECK:` text and validate them with a separately trusted tool or runtime. That remains user-designed coverage, not transitive tracing by this skill.

Approval and lease locks fail closed instead of being stolen automatically. If an owning process terminates unexpectedly, verify the PID recorded in that specific lock is no longer running and that no operation can still own it before removing the abandoned lock manually. Do not bulk-delete lock directories while this skill is active.

Do not run untrusted checks merely to learn what they do. Review them as source first. Use a disposable environment or stronger sandbox when source trust is uncertain.

## Shell and environment

Shell resolution follows `--shell`, then `AGENTS_DISCIPLINE_SHELL`, then Node's platform default. The child inherits the current environment, including `PATH`. Changing the terminal used to launch the checker can change which external tools resolve, especially on Windows.

Prefer repository-owned Node scripts and explicit `CWD:` values. A shell override does not install missing utilities, clean the environment, or restrict command access. The execution transcript shows the resolved `PATH`, capped for display. Persisted evidence includes resolved shell, working directory, exit status, a short `PATH` fingerprint, the match result, and a SHA-256/byte-count fingerprint of the exact canonical string supplied to `EXPECT:`. Both the raw stdout/stderr payload and that string's UTF-8 representation must fit the 1 MiB limit; invalid-byte replacement and the synthetic inter-stream newline count toward the latter, and an overflow is rejected before matching rather than truncated. Raw successful output is not echoed or written to the ledger. Failure diagnostics remain console-only, bounded, and stripped of terminal control, Unicode line-separator, and bidirectional-override characters. Gate-lint also caps each rendered field and the number of returned findings after escaping, while preserving full counts and exit semantics.

Regular-expression expectations run in at most four disposable workers. A separate five-second worker-startup limit applies before the 250ms match budget begins, so high `--jobs` concurrency cannot consume the backtracking budget merely by delaying worker startup. A timed-out worker is terminated and cannot certify a gate.

See [references/gates.md](references/gates.md) for the full shell and success contract.

## The delegation ledger checker executes acceptance commands

`ledger-check.mjs` re-runs the acceptance command of every `verified` row in `DELEGATION.md` through `/bin/bash -o pipefail -c` with the checker's user permissions and inherited environment, and it has no approval store: the delegation ledger is written by the coordinator, so its acceptance cells are treated as commands the coordinator wrote. Treat an inherited `DELEGATION.md` exactly like an inherited gate ledger: read every acceptance cell before running the checker, or set `AGENTS_DISCIPLINE_SKIP_RERUN=1` to check structure only (the checker announces the skip in its output). The checker requires `/bin/bash`; on a host without it every re-run is reported as a failure, not as a pass.

## Scopes and leases are not a sandbox

Scopes limit this skill's gate discovery, log target, dispatch waves, and lease labels. Ownership leases and dispatch launch barriers coordinate tools that voluntarily use the protocol. Neither mechanism prevents a process from reading or writing another path.

Separate worktrees can reduce ordinary path contention, but they may still share external caches and services. Use operating-system, container, or virtual-machine isolation for untrusted code. See [references/parallel.md](references/parallel.md).

## Local state files

Runtime, dispatch, and append-only audit files live under `.agents-discipline/` in scoped mode. Ledger, lease, and dispatch reads are bounded and require an unchanged regular single-link file; repository-discovered inputs must also remain within the canonical repository root. Named invalid inputs fail closed instead of disappearing as an empty pipeline, including a pinned scope whose named entry exists but is linked, special, unreadable, or outside the root. Nonblocking opens keep FIFOs from wedging the checker. State writes reject symlink directories and targets; status append also rejects multi-link files and verifies that its opened descriptor still names the same single-link regular file before writing.

On Windows, named-entry `lstat` remains the type/symlink/link-count guard, while same-file decisions compare strict BigInt `dev` plus `ino` from the original descriptor and a second non-creating descriptor opened from the current name. Bracketing named-entry snapshots, a precise inode bridge, and repeated descriptor snapshots make ordinary link and replacement races fail closed without comparing the affected path-stat `dev` to descriptor-stat `dev`. These are snapshot checks, not atomic path isolation. An adversary who can rename or redirect the Windows path during the checks could present one cross-volume, same-inode target to both opens while both `lstat` calls observe the original, or replace the name after its final snapshot; Node 16 exposes neither a Windows no-follow open nor native handle-to-name/volume primitives to close those races without a native dependency. Unix retains `O_NOFOLLOW` and its existing descriptor-to-path checks. Node/libuv's exposed `st_dev`/`st_ino` abstraction can also be weaker than a native volume GUID plus 128-bit file ID; BigInt preserves every exposed bit but cannot recover identifiers the runtime does not expose. Keep `.agents-discipline/` in the project's ignore rules. Native agent ids in dispatch waves are routing values, not secrets or authentication tokens.

Each check runs beneath a detached Node supervisor that remains the process-group leader until the shell and every inherited stdout/stderr descriptor close. POSIX group cleanup is attempted only while that exact supervisor is still observed live; after exit, its numeric PID/PGID is never signalled because it may have been reused. On Windows timeout cleanup, agents-discipline accepts only the drive-root `<drive>:\Windows\System32\taskkill.exe` when the host-provided `SystemRoot`, `WINDIR`, and `SystemDrive` values agree; arbitrary, missing, or inconsistent roots are rejected, and it never searches the check's current directory or `PATH`. These launcher environment values are a consistency boundary, not cryptographic proof of OS identity. If the location cannot be established, cleanup falls back to the already-held child handle and the checker still settles on its own bounded timer. A successful signal request is not treated as proof of process exit.

## Evidence and logs

Command output can contain private paths or other sensitive text. Successful output is consumed only for matching and then represented by a digest and byte count; it is not copied into terminal success lines or gate evidence. Failure diagnostics are still visible in the local terminal, so checks must not emit secrets on either path. Dispatch state contains timestamps and opaque host handles. Never put prompts, credentials, or result bodies in a handle. Design checks to emit a concise success marker and avoid printing secrets. Review ledgers, dispatch state, and status logs before committing or sharing them.

A sealed wave proves only that the host returned a distinct native start handle for every declared leaf before agents-discipline accepted a return. It does not prove exact CPU overlap, worker honesty, filesystem isolation, successful gates, or correct integration. `dispatch.json` is the transition authority; `status.log` is a later audit append. If that append is refused after a committed transition, the command succeeds with a bounded warning so callers inspect state instead of blindly replaying the transition.

agents-discipline does not intentionally collect telemetry or send approval, gate, or dispatch records to a service. A `CHECK:` command can perform its own network or logging activity because it is arbitrary code.

## Reporting a vulnerability

For ordinary defects, open a GitHub issue with a minimal reproduction. For a vulnerability whose reproduction would expose a secret or enable abuse, use GitHub's private vulnerability reporting for this repository if it is available. If it is not available, open a minimal issue asking the maintainer for a private contact method and omit sensitive details until a private channel exists.
