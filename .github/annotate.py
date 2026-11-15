"""Turn unittest failures into GitHub annotations so they show on the run summary page."""
import re
import sys

log = open(sys.argv[1], errors="replace").read()
blocks = re.split(r"^={50,}\n", log, flags=re.M)[1:]
if not blocks:
    blocks = ["Test run failed\n" + log[-3000:]]
for block in blocks[:10]:
    title, _, body = block.partition("\n")
    body = re.split(r"^-{50,}\nRan \d+ tests", body, flags=re.M)[0].strip()[-3000:]
    esc = body.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={title.strip()[:120]}::{esc}")
