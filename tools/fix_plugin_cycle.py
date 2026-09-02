"""Apply small asserted fixes before the platform verification run."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "src/opencollate/plugins.py"
plugin_text = PLUGIN_PATH.read_text(encoding="utf-8")
plugin_replacements = (
    (
        '''from opencollate.model import CanonicalDesign, DesignContract, ViewObservation
from opencollate.parsers.base import ViewParser

if TYPE_CHECKING:
    from opencollate.config import ProjectConfig
''',
        '''from opencollate.model import CanonicalDesign, DesignContract, ViewObservation

if TYPE_CHECKING:
    from opencollate.config import ProjectConfig
    from opencollate.parsers.base import ViewParser
''',
    ),
    (
        '''CheckerCallable = Callable[["CheckerContext"], Iterable[Diagnostic]]


def _normalized_name''',
        '''CheckerCallable = Callable[["CheckerContext"], Iterable[Diagnostic]]


def _is_parser(value: Any) -> bool:
    return isinstance(getattr(value, "format_name", None), str) and callable(
        getattr(value, "parse", None)
    )


def _normalized_name''',
    ),
    (
        '''        if not isinstance(self.parser, ViewParser):
''',
        '''        if not _is_parser(self.parser):
''',
    ),
    (
        '''    elif not isinstance(candidate, (ParserPluginSpec, ViewParser)) and callable(candidate):
''',
        '''    elif (
        not isinstance(candidate, ParserPluginSpec)
        and not _is_parser(candidate)
        and callable(candidate)
    ):
''',
    ),
)
for old, new in plugin_replacements:
    count = plugin_text.count(old)
    if count != 1:
        raise RuntimeError(f"plugins.py fix expected one target, found {count}")
    plugin_text = plugin_text.replace(old, new)
PLUGIN_PATH.write_text(plugin_text, encoding="utf-8", newline="\n")

DISPATCH_PATH = ROOT / "src/opencollate/parsers/dispatch.py"
dispatch_text = DISPATCH_PATH.read_text(encoding="utf-8")
dispatch_replacements = (
    (
        '''        if format_name in registrations:
            owner = registrations[format_name]
            conflicts.append(f"format {format_name!r} is already owned by {owner.provider}")
''',
        '''        if format_name in registrations:
            format_owner = registrations[format_name]
            conflicts.append(
                f"format {format_name!r} is already owned by {format_owner.provider}"
            )
''',
    ),
    (
        '''            owner = aliases.get(token)
            if owner is not None and owner != format_name:
                conflicts.append(f"alias {alias!r} is already owned by {owner!r}")
''',
        '''            token_owner = aliases.get(token)
            if token_owner is not None and token_owner != format_name:
                conflicts.append(f"alias {alias!r} is already owned by {token_owner!r}")
''',
    ),
    (
        '''            owner = extensions.get(extension)
            if owner is not None and owner != format_name:
                conflicts.append(f"extension {extension!r} is already owned by {owner!r}")
''',
        '''            extension_owner = extensions.get(extension)
            if extension_owner is not None and extension_owner != format_name:
                conflicts.append(
                    f"extension {extension!r} is already owned by {extension_owner!r}"
                )
''',
    ),
)
for old, new in dispatch_replacements:
    count = dispatch_text.count(old)
    if count != 1:
        raise RuntimeError(f"dispatch.py fix expected one target, found {count}")
    dispatch_text = dispatch_text.replace(old, new)
DISPATCH_PATH.write_text(dispatch_text, encoding="utf-8", newline="\n")
