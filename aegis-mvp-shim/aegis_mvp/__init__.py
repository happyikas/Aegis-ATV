"""``aegis-mvp`` deprecation shim — renamed to ``aegis-atv``.

This is the contents of the final ``aegis-mvp 0.4.1`` release on
PyPI. The module exists for one reason: to surface a clear redirect
when someone installs ``aegis-mvp`` with a pin (``aegis-mvp==0.4.1``
or similar) and then tries to import it. The yank on releases
0.2.0–0.4.0 takes care of unpinned ``pip install aegis-mvp``; this
shim takes care of pinned installs.

Project moved: <https://github.com/happyikas/Aegis-ATV>
PyPI: ``pip install aegis-atv``
CLI: ``aegis`` (unchanged)
"""

raise ImportError(
    "aegis-mvp has been renamed to aegis-atv. "
    "Install the new package with: pip install aegis-atv\n"
    "The CLI command 'aegis' is unchanged. "
    "Project: https://github.com/happyikas/Aegis-ATV"
)
