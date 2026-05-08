# Contributing to Aegis

Thanks for considering a contribution. Aegis is a security tool, and
that shapes how we accept changes — please read this short guide
before opening a PR.

## TL;DR

```bash
git clone https://github.com/happyikas/Aegis-ATV.git && cd Aegis-ATV
uv sync
uv run pytest                    # full unit + integration suite
uv run ruff check . && uv run mypy src
uv run python -m demo.macmini all   # 90-case regression
```

If all four pass, you're ready to open a PR.

## Ways to help

* **Bug reports** — open a GitHub issue using the `bug-report`
  template. Include OS, Python version, install mode (`sidecar` /
  `local`), and a reproduction. For security issues see
  [SECURITY.md](SECURITY.md) — do **not** file a public issue.
* **Feature requests** — open a `feature-request` issue. Please
  describe the user-visible behaviour first; implementation can
  follow.
* **Detection rules** — new entries in
  [`policies/rag_corpus/rules.jsonl`](policies/rag_corpus/rules.jsonl)
  or `policies/safe_actions.json` are very welcome. Each rule must
  ship with a unit test and a one-line incident reference (where the
  pattern was observed in the wild).
* **Documentation** — typo fixes, clarifications, and quickstart
  improvements need no prior issue.
* **Localization** — Korean and English are first-class; other
  languages welcome under `docs/<locale>/`.

## Development setup

* Python 3.11 (see `pyproject.toml`).
* `uv` for dependency management — `uv sync` installs everything
  including dev extras.
* No system-wide installs required; everything lives in `.venv/`.

```bash
# One-time
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
uv sync

# Verify
uv run pytest -x
uv run ruff check .
uv run mypy src
```

## Project conventions

These are summarised from `CLAUDE.md` — read that file for the
authoritative version.

* **Type hints required** on all public functions.
* **Pydantic v2** for any data crossing a process boundary
  (HTTP body, hook IPC, audit record).
* **`async`** is reserved for FastAPI handlers. Internal logic
  stays sync — easier to test, easier to reason about under the
  firewall pipeline.
* **External calls** (OpenAI, Anthropic, network) must use the
  retry + timeout helpers in `src/aegis/llm/`. Never call
  `httpx.post(...)` directly from a step.
* **Logging**: `structlog.get_logger()`. No `print()` in shipped
  code paths.
* **Audit log is append-only.** Never write code that mutates or
  deletes existing records — even tests should use a fresh tmp dir.
* **Keys never in git.** `keys/*.pem`, `keys/*.key`, `.env` are
  gitignored. CI verifies on every PR.
* **No literal destructive patterns in source.** The firewall scans
  Write/Edit content for step310/311 dangerous patterns; if your
  change must include a regex test fixture, use the token-splitting
  helpers in `tests/conftest.py` (`_join("rm", "-" + "rf", ...)`).

## Tests

* **Every new firewall step** ships with `tests/unit/test_stepXXX.py`.
* **Coverage floor**: 70% on changed code (CI enforces).
* **Integration tests** must mock the LLM provider — live API calls
  in CI are forbidden. Use `respx` (httpx) or the dummy provider.
* **The macmini suite** (`uv run python -m demo.macmini all`) is the
  end-to-end gate: 90 deterministic cases must all PASS for any PR
  that touches `src/aegis/firewall/`.

```bash
# Quick loop
uv run pytest tests/unit/test_step310.py -x

# Full gate
uv run pytest && uv run python -m demo.macmini all
```

## Pull request checklist

Before requesting review:

- [ ] `uv run pytest` passes locally.
- [ ] `uv run ruff check . && uv run mypy src` clean.
- [ ] `uv run python -m demo.macmini all` is 90/90 (firewall changes).
- [ ] New step / rule has a paired test.
- [ ] CHANGELOG entry under `## [Unreleased]` if user-visible.
- [ ] No keys, `.env` content, or real customer data in the diff.
- [ ] Commit message references a milestone (`step310`, `M14`, etc.)
      or a PR-tracker number.

PR titles follow the project convention — see `git log --oneline -20`
for examples. Common prefixes:

| Prefix | Meaning |
|--------|---------|
| `feat:` | new user-visible capability |
| `fix:` | bug fix |
| `cli:` | `aegis` CLI changes |
| `docs:` | documentation only |
| `demo:` | demo / recording / fixture assets |
| `test:` | test-only changes |
| `chore:` | tooling, deps, CI |

## Code review

* All non-trivial PRs need at least one approval.
* Security-sensitive PRs (anything touching `src/aegis/firewall/`,
  `src/aegis/audit/`, `src/aegis/keys/`, `tools/aegis_local_hook.py`)
  get extra scrutiny — please be patient.
* The maintainer may request changes that look like duplication of
  effort. Usually this is because the suggested approach has a
  subtle interaction with another step; ask "why" and we'll explain
  rather than re-roll silently.

## Reporting good-faith bugs vs. security issues

| Kind | Where to report |
|------|------------------|
| Crash, wrong rule fires, ergonomics bug | Public GitHub issue. |
| Firewall bypass, audit forgery, key leak | **Private email — see [SECURITY.md](SECURITY.md).** |
| Unsure | Email; we'll move it public if appropriate. |

## Code of conduct

This project follows the Contributor Covenant — see
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Reports of unacceptable
behaviour go to the SECURITY.md contact address with subject prefix
`[aegis-conduct]`.

## License

This project is licensed under the **Apache License, Version 2.0**
([LICENSE](LICENSE) + [NOTICE](NOTICE)). By submitting a contribution
you agree that your contribution is licensed under the same terms.

Apache-2.0 was chosen for three reasons:

1. **Explicit patent grant.** The Aegis architecture corresponds to
   the AegisData patent v4. Apache-2.0 is the standard OSS license
   that grants every recipient an explicit license to all patent
   claims that the contributor can license — preventing patent
   disputes that plain MIT/BSD leave open.
2. **Commercial-friendly without copyleft surprises.** Downstream
   users can integrate Aegis into proprietary code, and the project
   can later split out an Enterprise / Sidecar tier without
   re-licensing.
3. **Compatibility with downstream packagers.** Homebrew-core, PyPI,
   Linux distributions, and corporate review boards all accept
   Apache-2.0 without friction.

Contributions that introduce additional third-party dependencies
must use Apache-2.0-compatible licenses (Apache-2.0, MIT, BSD-2/3,
ISC). GPL/AGPL/SSPL/BSL dependencies are not accepted in the core
Personal MVP — open an issue first if your contribution requires one.
