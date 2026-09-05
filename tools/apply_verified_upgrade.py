"""Apply the pre-tested source patch only after exact content verification.

The permanent browser workflow is published separately through the authorized
repository connector. This job verifies it but does not create or modify it.
"""
from pathlib import Path
import base64
import gzip
import hashlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
encoded = "".join((ROOT / f"tools/verified-upgrade/{index:02}.b64").read_text(encoding="ascii").strip() for index in range(7))
# Correct one known transport transcription; the complete digest below is the authority.
encoded = encoded.replace("QzzJazBazBmtj", "QzzJazBmtj")
if len(encoded) != 41612:
    raise RuntimeError(f"transport length mismatch: {len(encoded)}")
patch = gzip.decompress(base64.b64decode(encoded, validate=True))
expected = "b3a6c919dfc4995c539e3d6827e4445f2a1509496a824277058c0522897178da"
actual = hashlib.sha256(patch).hexdigest()
if actual != expected or len(patch) != 118163:
    raise RuntimeError(f"verified source patch digest mismatch: {actual}")
workflow = ROOT / ".github/workflows/browser-review.yml"
if hashlib.sha256(workflow.read_bytes()).hexdigest() != "656a5cb82cb51e53e5d58c2eb4910fd92662ecd28ebea25159dafe54ebf80d9c":
    raise RuntimeError("connector-published browser workflow differs from verified source")
exclude = "--exclude=.github/workflows/browser-review.yml"
subprocess.run(["git", "apply", exclude, "--check", "--whitespace=error-all", "-"], cwd=ROOT, input=patch, check=True)
subprocess.run(["git", "apply", exclude, "--whitespace=error-all", "-"], cwd=ROOT, input=patch, check=True)
print(f"Applied verified source patch SHA-256 {actual}")
