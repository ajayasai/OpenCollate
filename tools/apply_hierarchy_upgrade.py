"""Apply an exact, locally tested source patch; removed before publication."""
from pathlib import Path
import base64
import gzip
import hashlib
import subprocess

root = Path(__file__).resolve().parents[1]
parts = [(root / f"tools/hierarchy-upgrade/{i:02}.b64").read_text(encoding="ascii").strip() for i in range(6)]
# Correct two identified transport transcription characters, never source text.
parts[0] = parts[0].replace("vrxJJuv", "vrxJuv").replace("frKJJij", "frKJij")
encoded = "".join(parts)
if len(encoded) != 34508:
    raise RuntimeError(f"transport length mismatch: {len(encoded)}")
patch = gzip.decompress(base64.b64decode(encoded, validate=True))
expected = "56de55517ca0625443dab1805b91e69739acf40af739305984c76c5af378fd65"
if len(patch) != 102263 or hashlib.sha256(patch).hexdigest() != expected:
    raise RuntimeError("tested source patch digest mismatch")
for args in (["git", "apply", "--check", "--whitespace=error-all", "-"], ["git", "apply", "--whitespace=error-all", "-"]):
    subprocess.run(args, input=patch, cwd=root, check=True)
print(f"Applied exact tested source patch SHA-256 {expected}")
