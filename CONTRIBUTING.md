# Contributing

Thanks for helping. afield is small on purpose: one stdlib-only Python file (`bin/afield`),
the contract tests (`lib/afield_contracts.py`), systemd units and two test files.

Before a pull request:

```
python3 -I tests/test_afield.py
python3 -I tests/test_contracts.py
afield doctor            # on a machine where afield runs
```

- **Decisions are pure functions** at the top of `bin/afield`, each with a test. Keep side effects
  (systemctl, tmux, files, the API) in the thin layer below them.
- **Relying on something new in Claude Code, Even Terminal, tmux or systemd** (a field, flag, file,
  on-screen text, API)? Add a contract: a check in `lib/afield_contracts.py`, a test that feeds it a broken
  fake, a `### ID` section in `CONTRACTS.md`, and a `# contract: ID` comment where the code relies on
  it. The tests fail if one of those is missing.
- **A contract broke after an update?** Follow the playbook in `CONTRACTS.md`. Fix afield, not the
  check, unless the check itself was wrong; then `afield doctor --update-snapshot` and commit
  `contracts/snapshot.json`.
- **New settings** go in `CONFIG_DEFAULTS` with a safe default, a test, and a row in the README table.
- No new dependencies: it must run with the system Python 3.10+.
- Add a line under "Unreleased" in `CHANGELOG.md`.
