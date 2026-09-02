"""Fix StrEnum normalization before contract-v2 verification."""

from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "src/opencollate/diagnostics.py"
text = PATH.read_text(encoding="utf-8")
old = '''    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
'''
new = '''    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
'''
if text.count(old) != 1:
    raise RuntimeError("expected exactly one json_safe primitive/StrEnum ordering target")
PATH.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
