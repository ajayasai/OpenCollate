"""One-shot transport; removed before the final source-only PR commit."""
from pathlib import Path
import base64
import hashlib
import json
import lzma
import subprocess
import urllib.request

root = Path(__file__).resolve().parents[1]
parts = []
expected_parts = [
    '590271fcaacd4fd6c7d300ebec63b24a967481344997189e4a6fadec9eef494e',
    'e4a1cc154dbd3d8985eba8afaa834f51bb3a9c89b243da64ec32fe03f63e26c2',
    '628532d97a6f43049c1caa2bb6f72cbaa03e20f3ca6bf5fdcfd778e2d2fca536',
    '68d4396381cdb46f20aa7e8540504e2ddcb8d0cd66c9c1371a85b660001376a7',
    'a64b2b32226c2de2f68c71437943cd819b01467c22484f6727071216a5e0f203',
    '42cff25b5e0838d31e791ced4c357b984293eefd1bde6c3b749166b0d31e0e1b',
    '3f13445237a7a5566c823aa80ceb088c8df526ac7fadfa34c1ba5dd34e04c8f8',
    '36066261aed5312d300eb4b0e0118ccf00b996cf5de38a73e7cca6d9fb4cf11e',
]
for index, expected in enumerate(expected_parts):
    text = (root / f'tools/certificate-upgrade/{index:02}.b64').read_text().strip()
    actual = hashlib.sha256(text.encode()).hexdigest()
    if actual != expected:
        raise RuntimeError(f'payload {index} digest mismatch: {actual}')
    parts.append(text)
encoded = ''.join(parts)
if len(encoded) != 42240:
    raise RuntimeError('transport length mismatch')
patch = lzma.decompress(base64.b64decode(encoded, validate=True))
if len(patch) != 150315 or hashlib.sha256(patch).hexdigest() != '13d99d54f46d36e7f1d89466cc7665689c221f1b0e76a23bf796e73b8fc45cfb':
    raise RuntimeError('source patch content mismatch')
subprocess.run(['git', 'apply', '--check', '--whitespace=error-all', '-'], input=patch, cwd=root, check=True)
subprocess.run(['git', 'apply', '--whitespace=error-all', '-'], input=patch, cwd=root, check=True)
fixtures = root / 'benchmarks/fixtures/sky130'
manifest = json.loads((fixtures / 'manifest.json').read_text())
if manifest['repository'] != 'google/skywater-pdk-libs-sky130_fd_sc_hd' or manifest['revision'] != 'ac7fb61f06e6470b94e8afdf7c25268f62fbd7b1':
    raise RuntimeError('unexpected upstream')
for entry in manifest['files']:
    relative = Path(entry['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise RuntimeError('invalid fixture path')
    url = f"https://raw.githubusercontent.com/{manifest['repository']}/{manifest['revision']}/{entry['path']}"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(2000001)
    if len(data) > 2000000 or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise RuntimeError(f'upstream content mismatch: {relative}')
    target = fixtures / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
print('Exact source patch and all original upstream fixture SHA-256 digests verified.')
