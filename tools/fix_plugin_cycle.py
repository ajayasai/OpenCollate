"""Remove the runtime parser-package import from the extension contract module."""

from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "src/opencollate/plugins.py"
text = PATH.read_text(encoding="utf-8")
replacements = (
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
for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"plugins.py cycle fix expected one target, found {count}")
    text = text.replace(old, new)
PATH.write_text(text, encoding="utf-8", newline="\n")
