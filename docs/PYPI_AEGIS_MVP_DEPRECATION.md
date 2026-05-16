# Deprecating the `aegis-mvp` PyPI project

> One-time admin playbook for retiring the old `aegis-mvp` package
> on PyPI now that v0.5.0+ ships as **`aegis-atv`**.

`aegis-mvp` carries five historical releases (0.2.0 → 0.4.0). They
still work, but new users shouldn't be installing them. This
document is the deprecation checklist a project owner runs once,
from the PyPI web UI — there's no Trusted Publisher / OIDC path
for these admin actions.

## TL;DR

1. **Yank** every existing `aegis-mvp` release on PyPI so
   `pip install aegis-mvp` stops resolving anything.
2. **Update** the project description on `aegis-mvp` to point at
   `aegis-atv`.
3. *(Optional)* publish a final `aegis-mvp` version whose `__init__`
   raises with a redirect message — for users who pinned an exact
   version and bypass yanking.
4. Leave the project itself in place — **do not delete** —
   for archival reasons + so the historical name is reserved.

## Step 1 — Yank every release

Yanking is the standard PyPI deprecation signal. A yanked release:

* No longer resolves for `pip install aegis-mvp` (or `==<wildcard>`).
* Still installs for an **explicit pin** (`pip install
  aegis-mvp==0.4.0`) — protects existing CI pipelines from sudden
  breakage.
* Shows up with a strikethrough in the PyPI release list.

Walk through each version. URLs (replace 0.X.Y per row):

| Version | URL |
|---|---|
| 0.2.0 | <https://pypi.org/manage/project/aegis-mvp/release/0.2.0/> |
| 0.3.1 | <https://pypi.org/manage/project/aegis-mvp/release/0.3.1/> |
| 0.3.2 | <https://pypi.org/manage/project/aegis-mvp/release/0.3.2/> |
| 0.3.3 | <https://pypi.org/manage/project/aegis-mvp/release/0.3.3/> |
| 0.4.0 | <https://pypi.org/manage/project/aegis-mvp/release/0.4.0/> |

On each page:

1. Scroll to **"Yank"** at the bottom.
2. Reason: `Renamed to aegis-atv on PyPI; install with `pip install aegis-atv`.`
3. Confirm.

Verify after the sweep:

```bash
curl -s https://pypi.org/pypi/aegis-mvp/json \
  | python -c "import json,sys; d=json.load(sys.stdin); \
print([v for v,rs in d['releases'].items() \
       if any(r.get('yanked') for r in rs)])"
# → ['0.2.0', '0.3.1', '0.3.2', '0.3.3', '0.4.0']
```

`pip install aegis-mvp` should now fail with `No matching
distribution found for aegis-mvp` (because every available
distribution is yanked).

## Step 2 — Update the project description

The PyPI project description still says "Aegis ATV / Aegis MVP"
without explicit redirect. Add a top-of-`README` note that gets
re-uploaded with any future `aegis-mvp` publish — but since we're
**not** going to publish again, the cleanest way is to edit the
project's PyPI-side metadata directly:

1. <https://pypi.org/manage/project/aegis-mvp/settings/>
2. Update the **Short description** to:
   ```
   DEPRECATED — renamed to aegis-atv. Install with `pip install
   aegis-atv`. See https://github.com/happyikas/Aegis-ATV.
   ```
3. (Optional) Replace the **long description** with the same
   pointer. PyPI displays the long description on the project page;
   updating it via the web UI requires a fresh sdist upload, which
   we're avoiding — the short description is enough.

## Step 3 (optional) — Final "redirect" version

If you want pinned-version users to hit a clear error too:

1. In the `aegis-atv` repo, create branch `chore/aegis-mvp-deprecation-shim`.
2. Drop a single file `aegis_mvp_shim/__init__.py`:
   ```python
   raise RuntimeError(
       "aegis-mvp is renamed to aegis-atv. "
       "Install with `pip install aegis-atv` "
       "(https://github.com/happyikas/Aegis-ATV)."
   )
   ```
3. Build a wheel + sdist for `aegis-mvp 0.4.1` containing **only**
   that file.
4. Publish via the existing `aegis-mvp` Trusted Publisher (the one
   that worked for 0.4.0 — already configured on PyPI).

The previous "Add publisher" workflow we did for `aegis-atv` on
2026-05-15 is the template; just substitute `aegis-mvp` as the
project name. Note: **do not** yank 0.4.1 — that's the version
operators who pin will hit, and we want them to see the redirect.

This step is intentionally optional. The yank in step 1 stops 99 %
of new traffic; the redirect shim handles the long tail.

## Step 4 — Do NOT delete the project

PyPI lets you delete a project entirely, but doing so:

* **Frees up the name** — anyone can grab `aegis-mvp` and publish
  arbitrary code under it. Supply-chain risk.
* Breaks any `requirements.txt` that lists `aegis-mvp` with no version
  (the lockup gets a "project not found" instead of a yank notice
  — less actionable).

Yanked + redirected is the right end state. Leave the project owned
by the same account.

## Status checklist

- [ ] All 5 `aegis-mvp` releases yanked (step 1)
- [ ] PyPI short description updated to mention `aegis-atv` (step 2)
- [ ] *(optional)* `aegis-mvp 0.4.1` redirect shim published (step 3)
- [ ] Project NOT deleted (step 4 — verify)

When the four boxes are checked, the migration is complete:
`pip install aegis-mvp` fails with a redirect-able error, the
PyPI project page tells visitors where to go, and the name stays
locked to prevent supply-chain hijacking.
