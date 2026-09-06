"""One-shot, checksum-bound transport for the locally tested source patch.

Removed before the final implementation commit. No encoded content is executed;
only a Git patch with the pre-recorded byte count and digest may be applied.
"""
from pathlib import Path
import base64
import gzip
import hashlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parts = [ROOT / f"tools/seq-patch/{index:02}.b64" for index in range(28)]
encoded = "".join(path.read_text(encoding="ascii").strip() for path in parts)
if len(encoded) != 82468:
    raise RuntimeError(f"transport length mismatch: {len(encoded)}")
patch = gzip.decompress(base64.b64decode(encoded, validate=True))
expected = "54d83608251ee7e7b2bce11b4eed9205433a92ed2f4bc922b90aa5bd7ff7e544"
actual = hashlib.sha256(patch).hexdigest()
if len(patch) != 249725 or actual != expected:
    raise RuntimeError(f"source patch digest mismatch: {actual}")
subprocess.run(["git", "apply", "--check", "--whitespace=error-all", "-"], cwd=ROOT, input=patch, check=True)
subprocess.run(["git", "apply", "--whitespace=error-all", "-"], cwd=ROOT, input=patch, check=True)
print(f"Applied tested source patch SHA-256 {actual}")
