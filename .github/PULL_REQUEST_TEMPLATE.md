<!--
Thanks for the PR. A few quick notes before submitting:

* If this is a security finding, please use the private channel in
  SECURITY.md instead of a public PR.
* PR titles follow the project convention — see `git log --oneline -20`
  for examples (feat:, fix:, cli:, docs:, demo:, test:, chore:).
-->

## Summary

<!-- 1–3 bullet points. What changed and why. -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] New detection rule / playbook
- [ ] Documentation only
- [ ] Refactor / chore (no user-visible change)
- [ ] Breaking change (please describe migration path)

## Test plan

<!-- Bulleted checklist. Include the exact commands you ran. -->

- [ ] `uv run pytest`
- [ ] `uv run ruff check . && uv run mypy src`
- [ ] `uv run python -m demo.macmini all` (firewall changes only)
- [ ] Manual verification: <describe>

## Checklist

- [ ] Linked issue (if applicable): #
- [ ] Test added / updated for the change.
- [ ] CHANGELOG entry under `[Unreleased]` for user-visible changes.
- [ ] No keys, `.env` content, or real customer data in the diff.
- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md).

## Notes for reviewers

<!-- Anything unusual: trade-offs, follow-up tickets, areas you'd like
     extra scrutiny on. -->
