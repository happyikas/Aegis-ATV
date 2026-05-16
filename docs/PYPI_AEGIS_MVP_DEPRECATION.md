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
   `pip install aegis-mvp` stops resolving anything. Each yank's
   *reason* string is shown to pip users as a redirect message.
2. ~~Update the project description on `aegis-mvp`.~~ **Not possible
   from the web UI** — PyPI sources description from the latest
   non-yanked release's `pyproject.toml`. To change it, you'd have
   to publish a new release (covered by step 3 below). For most
   projects step 1 is enough.
3. *(Optional)* publish a final `aegis-mvp` version whose `__init__`
   raises with a redirect message — for users who pinned an exact
   version and bypass yanking. This also has the side effect of
   updating the displayed project description.
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

1. Scroll to the blue **"Yank release"** section (NOT the red
   "Delete release" section — that's irreversible and frees up the
   name for hijacking).
2. Click **"Yank release"**. A confirmation modal opens.
3. **Reason** (optional but recommended):
   ``Renamed to aegis-atv on PyPI; install with `pip install aegis-atv`.``
4. **Version** field — type **just the version number** (e.g.
   `0.4.0`), NOT `aegis-mvp 0.4.0`. The placeholder text shows the
   latter format but PyPI accepts the bare version. The
   confirmation button stays disabled until the version field
   exactly matches.
5. Click the (now-enabled) **"Yank release"** button in the modal.

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

## Step 2 — Update the project description ~~(web UI)~~ — NOT POSSIBLE

This step **does not work the way the playbook originally claimed.**
We attempted it on 2026-05-16 and discovered that PyPI's settings
page (<https://pypi.org/manage/project/aegis-mvp/settings/>) is
explicit:

> "To set the 'aegis-mvp' description, author, links, classifiers,
> and other details for **your next release**, use the project
> metadata fields in your `pyproject.toml` file. Updating these
> fields will **not change the metadata for past releases**."

In other words — PyPI doesn't expose a "edit description" form for
already-published projects. The Summary / long description always
comes from the most recent non-yanked release's `pyproject.toml`.

**Two takeaways:**

1. **For most deprecation flows, step 1 (yank-with-reason) is
   enough.** The reason text propagates to pip's error message
   automatically. Anyone running `pip install aegis-mvp` sees the
   redirect. The displayed project page still carries the old
   description, but the "Releases: 0" badge in the sidebar + the
   yank strikethrough on every version make the deprecation clear.

2. **If you need to update the displayed description,** the only
   path is **step 3** — publish a new release whose `pyproject.toml`
   has the deprecation text. That release is the new "latest", so
   PyPI surfaces its description.

## Step 3 (optional) — Final "redirect" version

If you want pinned-version users to hit a clear error too, ship
`aegis-mvp 0.4.1` as a one-file shim that raises `ImportError`.

### Implemented as a separate package under this repo

The shim lives at **`aegis-mvp-shim/`** in the Aegis-ATV repo so the
canonical package (`aegis-atv`) and the deprecation shim stay
version-controlled together. Files:

* `aegis-mvp-shim/aegis_mvp/__init__.py` — raises `ImportError` with
  the redirect message
* `aegis-mvp-shim/pyproject.toml` — name=aegis-mvp, version=0.4.1,
  zero dependencies, `Development Status :: 7 - Inactive` classifier
* `aegis-mvp-shim/README.md` — PyPI long-description with the
  rename notice + migration steps
* `.github/workflows/release-aegis-mvp-shim.yml` — tag-triggered
  publish, fires only on `aegis-mvp-v*` tags (distinct from the
  `v*` tags that drive `release-pypi.yml` for aegis-atv)

### One-time PyPI setup (before pushing the first `aegis-mvp-v*` tag)

The `aegis-mvp` project's Trusted Publishers need an entry that
points at the **new** workflow filename (not the original
`release-pypi.yml` that published aegis-mvp up to 0.4.0).

1. <https://pypi.org/manage/project/aegis-mvp/settings/publishing/>
2. Add a new publisher:

   | field | value |
   |---|---|
   | Owner | `happyikas` |
   | Repository | `Aegis-ATV` |
   | Workflow filename | `release-aegis-mvp-shim.yml` |
   | Environment name | `pypi-aegis-mvp` |

### Publishing

After the Trusted Publisher is configured:

```bash
git tag aegis-mvp-v0.4.1
git push origin aegis-mvp-v0.4.1
```

The workflow builds `aegis-mvp-shim/`, publishes to PyPI via
trusted-publisher OIDC, and surfaces the result on the
`aegis-mvp` project page.

### Important

* **Do not yank** `aegis-mvp 0.4.1` after publish. That's the
  version pinned operators hit, and the whole point is for them to
  see the redirect message.
* Re-publishing the shim (e.g. fixing a typo in the README) requires
  a version bump to `0.4.2` — PyPI rejects re-uploads of the same
  version, and the shim workflow has `skip-existing: false` to
  hard-fail rather than silently skip.

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

- [x] All 5 `aegis-mvp` releases yanked, 2026-05-16 (step 1)
- [n/a] PyPI short description update — **not possible from web
  UI** (see step 2 above); skipped as not worth a redirect-shim
  release.
- [x] Redirect shim implemented at `aegis-mvp-shim/` + workflow
  `release-aegis-mvp-shim.yml` (step 3 — ready to publish once the
  Trusted Publisher is configured on the `aegis-mvp` PyPI project)
- [x] Project NOT deleted, verified 2026-05-16 (step 4)

`pip install aegis-mvp` now fails with the redirect text:

```
WARNING: aegis-mvp was yanked. Reason: Renamed to aegis-atv on
PyPI; install with pip install aegis-atv.
ERROR: No matching distribution found for aegis-mvp
```

The deprecation is functionally complete. Anyone landing on the
PyPI project page sees the Releases:0 badge + yanked strikethrough
on every version; anyone reaching for `pip install` sees the
redirect message above. The project name remains locked.
