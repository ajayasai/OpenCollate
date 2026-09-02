"""Apply exact contract-v2 regression repairs before verification."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one repair target, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace_once(
    "src/opencollate/diagnostics.py",
    '''    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
''',
    '''    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
''',
)

replace_once(
    "tests/test_engine_edges.py",
    '''    output = engine.export_contract(result.design, tmp_path / "nested" / "contract.json")
''',
    '''    output = engine.export_contract(
        result.design,
        tmp_path / "nested" / "contract.json",
        observations=(liberty, rtl),
    )
''',
)
