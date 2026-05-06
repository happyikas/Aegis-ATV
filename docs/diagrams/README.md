# Aegis Diagrams

Visual references for the Aegis architecture. Each diagram has a
`draw_*.py` companion that regenerates the PNG byte-stably.

## Files

| Diagram | Source | Description |
|---------|--------|-------------|
| [`atv_2080_v1.png`](atv_2080_v1.png) | [`draw_atv_2080_v1.py`](draw_atv_2080_v1.py) | ATV-2080-v1 structure as currently shipped: 30 subfields × 2,080 float32 + ATVHeader + Ed25519/SHA3 footer + 16-step firewall pipeline. Mirrors the visual language of the patent figure but with the actual production layout. |

## Style conventions

* **Mirror the patent figure layout** when ATV/firewall topics are
  involved — title strip, top-row colour-coded sections, info boxes,
  horizontal pipeline at the bottom.
* **Pillow only** for portability — no matplotlib, no Graphviz, no
  external rendering tooling. `uv run python <script>` from a fresh
  clone (after `uv sync`) reproduces every PNG bit-for-bit.
* **Fixed canvas dimensions** in each script (declared at top), so the
  same script never produces a differently-sized image.
* **System fonts on macOS** (`HelveticaNeue.ttc`, `Menlo.ttc`) with
  graceful fallback to `ImageFont.load_default()` on platforms that
  lack them — output stays sensible on Linux CI.

## Regenerating

```bash
# Install Pillow into the project venv (one-off):
uv pip install Pillow

# Render any diagram by running its script:
uv run python docs/diagrams/draw_atv_2080_v1.py
# wrote docs/diagrams/atv_2080_v1.png (2400x1900)
```

## Adding a new diagram

1. Drop `draw_<topic>.py` next to this README.
2. Mirror the existing script's structure (constants at top, `main()`
   at bottom that writes to `Path(__file__).resolve().parent / "<name>.png"`).
3. Add a row to the table above.
4. Commit both the script and the rendered PNG so reviewers don't
   need Pillow to see the result.

The source-script-+-PNG-pair convention keeps reviews diffable
(Markdown link to the PNG works in GitHub's web UI) while still
making every diagram editable in plain Python.
