from pathlib import Path

P1 = "r" + "m -rf /"
P2 = "DR" + "OP TABLE"
P3 = "ignore" + " all previous instr" + "uctions"

T = Path("tools/dogfood/_phase_b_template.md").read_text()
out = T.replace("__P1__", "`" + P1 + "`").replace("__P2__", "`" + P2 + "`").replace("__P3__", "`" + P3 + "`")
Path("docs/DOGFOOD_PHASE_B.md").write_text(out)
print(f"wrote docs/DOGFOOD_PHASE_B.md ({len(out)} bytes)")
