# aegis-mvp — DEPRECATED

> This package has been **renamed to `aegis-atv`** (PyPI: <https://pypi.org/project/aegis-atv/>).
>
> All historical `aegis-mvp` releases (0.2.0 → 0.4.0) on PyPI have been yanked. The CLI command (`aegis`) and module interface are unchanged.

## Install the new package

```bash
pip install aegis-atv

# or with uv:
uv tool install aegis-atv
```

## Why this 0.4.1 release exists

If you pinned `aegis-mvp==0.4.0` (or another specific version) in `requirements.txt` or a lockfile, the yank on the old releases doesn't reach you — you can still install them via the explicit pin. **0.4.1 is a one-file shim** whose only purpose is to raise `ImportError` with the redirect message the moment you `import aegis_mvp`, so you see the rename even if your dependency resolver skipped the yank notice.

```text
ImportError: aegis-mvp has been renamed to aegis-atv. Install the new
package with: pip install aegis-atv
The CLI command 'aegis' is unchanged. Project: https://github.com/happyikas/Aegis-ATV
```

## Migration

```bash
pip uninstall aegis-mvp
pip install aegis-atv
```

Update any `requirements.txt` / `pyproject.toml` / `Pipfile` entries:

```diff
- aegis-mvp==0.4.0
+ aegis-atv>=0.5.0
```

The `aegis` CLI command, the Python module name (`aegis`, not `aegis_mvp`), and all features are unchanged. See <https://github.com/happyikas/Aegis-ATV> for full release notes, docs, and the canonical issue tracker.

## License

Apache-2.0, identical to the upstream `aegis-atv` package.
