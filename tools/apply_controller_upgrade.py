"""One-shot transport for the locally tested controller patch; removed before commit."""
from pathlib import Path
import base64
import hashlib
import lzma
import subprocess

root = Path(__file__).resolve().parents[1]
encoded = "".join((root / f"tools/controller-patch/{i:02}.b64").read_text(encoding="ascii").strip() for i in range(7))
if len(encoded) != 49408:
    raise RuntimeError(f"transport length mismatch: {len(encoded)}")
patch = lzma.decompress(base64.b64decode(encoded, validate=True))
expected = "f6c1da59af3b8670748269eaa1b845967e55bb252a9d350256e424791a54284a"
if len(patch) != 174593 or hashlib.sha256(patch).hexdigest() != expected:
    raise RuntimeError("tested source patch checksum mismatch")
subprocess.run(["git", "apply", "--check", "--whitespace=error-all", "-"], cwd=root, input=patch, check=True)
subprocess.run(["git", "apply", "--whitespace=error-all", "-"], cwd=root, input=patch, check=True)
print(f"Applied exact tested source SHA-256 {expected}")
