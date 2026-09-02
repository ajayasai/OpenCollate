"""Tighten the ParserPluginSpec anchor in the one-shot transformation."""

from pathlib import Path


PATH = Path(__file__).resolve().parent / "apply_parallel_parsing.py"
text = PATH.read_text(encoding="utf-8")
old = '''    ''' + "'''" + '''    provider: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
''' + "'''" + ''',
    ''' + "'''" + '''    provider: str | None = None
    version: str | None = None
    parallel_safe: bool = False

    def __post_init__(self) -> None:
''' + "'''" + ''',
'''
new = '''    ''' + "'''" + '''class ParserPluginSpec:
    """A parser plus the names and suffixes it owns.

    ``provider`` and ``version`` are normally filled from package metadata at
    discovery time. A parser plugin cannot silently override a built-in format,
    alias, or extension; the complete registration is rejected on any conflict.
    """

    parser: ViewParser
    aliases: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    api_version: int = PLUGIN_API_VERSION
    name: str | None = None
    provider: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
''' + "'''" + ''',
    ''' + "'''" + '''class ParserPluginSpec:
    """A parser plus the names and suffixes it owns.

    ``provider`` and ``version`` are normally filled from package metadata at
    discovery time. A parser plugin cannot silently override a built-in format,
    alias, or extension; the complete registration is rejected on any conflict.
    """

    parser: ViewParser
    aliases: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    api_version: int = PLUGIN_API_VERSION
    name: str | None = None
    provider: str | None = None
    version: str | None = None
    parallel_safe: bool = False

    def __post_init__(self) -> None:
''' + "'''" + ''',
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one transform anchor, found {text.count(old)}")
PATH.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
