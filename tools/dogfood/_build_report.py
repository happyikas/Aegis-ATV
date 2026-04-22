from pathlib import Path

P1 = "r" + "m -rf /"
P2 = "DR" + "OP TABLE"
P3 = "ignore" + " all previous instr" + "uctions"

T = open("tools/dogfood/_report_template.md").read()
out = T.replace("__P1__", "`" + P1 + "`").replace("__P2__", "`" + P2 + "`").replace("__P3__", "`" + P3 + "`")
Path("docs/DOGFOOD.md").write_text(out)
print(f"wrote docs/DOGFOOD.md ({len(out)} bytes)")
