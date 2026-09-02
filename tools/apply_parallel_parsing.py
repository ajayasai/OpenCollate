"""Apply deterministic parallel parsing integration with exact source anchors."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one target, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace_once(
    "src/opencollate/plugins.py",
    '''    provider: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
''',
    '''    provider: str | None = None
    version: str | None = None
    parallel_safe: bool = False

    def __post_init__(self) -> None:
''',
)
replace_once(
    "src/opencollate/plugins.py",
    '''        if self.version is not None and not self.version.strip():
            raise PluginContractError("parser plugin version must not be empty")
        object.__setattr__(self, "aliases", aliases)
''',
    '''        if self.version is not None and not self.version.strip():
            raise PluginContractError("parser plugin version must not be empty")
        if type(self.parallel_safe) is not bool:
            raise PluginContractError("parser plugin parallel_safe must be a boolean")
        object.__setattr__(self, "aliases", aliases)
''',
)
replace_once(
    "src/opencollate/plugins.py",
    '''            "provider": self.provider,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class CheckerContext:
''',
    '''            "provider": self.provider,
            "version": self.version,
            "parallel_safe": self.parallel_safe,
        }


@dataclass(frozen=True, slots=True)
class CheckerContext:
''',
)

replace_once(
    "src/opencollate/parsers/dispatch.py",
    '''    plugin_name: str | None = None
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
''',
    '''    plugin_name: str | None = None
    builtin: bool = False
    parallel_safe: bool = False

    def to_dict(self) -> dict[str, Any]:
''',
)
replace_once(
    "src/opencollate/parsers/dispatch.py",
    '''            "plugin": self.plugin_name,
            "builtin": self.builtin,
        }
''',
    '''            "plugin": self.plugin_name,
            "builtin": self.builtin,
            "parallel_safe": self.parallel_safe,
        }
''',
)
replace_once(
    "src/opencollate/parsers/dispatch.py",
    '''        provider="OpenCollate",
        builtin=True,
    )
''',
    '''        provider="OpenCollate",
        builtin=True,
        parallel_safe=True,
    )
''',
)
replace_once(
    "src/opencollate/parsers/dispatch.py",
    '''            plugin_name=spec.name,
            builtin=False,
        )
''',
    '''            plugin_name=spec.name,
            builtin=False,
            parallel_safe=spec.parallel_safe,
        )
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''from opencollate.engine import ComparisonEngine, EngineResult, write_contract
from opencollate.model import ViewObservation
''',
    '''from opencollate.engine import ComparisonEngine, EngineResult, write_contract
from opencollate.execution import MAX_PARSE_JOBS, ordered_parallel_map, parse_job_count
from opencollate.model import ViewObservation
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "config",
        nargs="?",
        help="project configuration (default: opencollate.toml)",
    )
    parser.add_argument("-c", "--config", dest="config_option", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
''',
    '''def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "config",
        nargs="?",
        help="project configuration (default: opencollate.toml)",
    )
    parser.add_argument("-c", "--config", dest="config_option", help=argparse.SUPPRESS)


def _job_count(value: str) -> int:
    try:
        return parse_job_count(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _jobs_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jobs",
        type=_job_count,
        default=1,
        metavar="N",
        help=f"parse independent safe views with N workers, 1-{MAX_PARSE_JOBS} (default: 1)",
    )


def build_parser() -> argparse.ArgumentParser:
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    _config_argument(check)
    check.add_argument(
''',
    '''    _config_argument(check)
    _jobs_argument(check)
    check.add_argument(
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    _config_argument(review)
    review.add_argument("--baseline", required=True, help="prior OpenCollate JSON report")
''',
    '''    _config_argument(review)
    _jobs_argument(review)
    review.add_argument("--baseline", required=True, help="prior OpenCollate JSON report")
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    demo = subparsers.add_parser("demo", help="run a self-contained synthetic demonstration")
    demo.add_argument("--output-dir", help="keep generated sources in this directory")
''',
    '''    demo = subparsers.add_parser("demo", help="run a self-contained synthetic demonstration")
    _jobs_argument(demo)
    demo.add_argument("--output-dir", help="keep generated sources in this directory")
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    build = contract_subparsers.add_parser("build", help="build a contract from configured views")
    _config_argument(build)
    build.add_argument("-o", "--output", default="contract.oc.json")
''',
    '''    build = contract_subparsers.add_parser("build", help="build a contract from configured views")
    _config_argument(build)
    _jobs_argument(build)
    build.add_argument("-o", "--output", default="contract.oc.json")
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''def _load_observations(config: ProjectConfig) -> tuple[ViewObservation, ...]:
    return tuple(_parse_source(source) for source in config.sources)


def _run(config: ProjectConfig) -> EngineResult:
    return ComparisonEngine(config).run(_load_observations(config))
''',
    '''def _source_parallel_safe(source: SourceConfig) -> bool:
    from opencollate.parsers import UnsupportedFormatError, get_registration

    try:
        return get_registration(source.view.kind).parallel_safe
    except UnsupportedFormatError:
        return False


def _load_observations(
    config: ProjectConfig,
    *,
    jobs: int = 1,
) -> tuple[ViewObservation, ...]:
    return ordered_parallel_map(
        config.sources,
        _parse_source,
        jobs=jobs,
        parallel_safe=_source_parallel_safe,
    )


def _run(config: ProjectConfig, *, jobs: int = 1) -> EngineResult:
    return ComparisonEngine(config).run(_load_observations(config, jobs=jobs))
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    result = _run(config)
    _emit(_render(result, args.format, verbose=args.verbose), args.output)
''',
    '''    result = _run(config, jobs=args.jobs)
    _emit(_render(result, args.format, verbose=args.verbose), args.output)
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    result = _run(config)
    current = result.to_dict()
''',
    '''    result = _run(config, jobs=args.jobs)
    current = result.to_dict()
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    result = _run(load_config(root / "opencollate.toml"))
''',
    '''    result = _run(load_config(root / "opencollate.toml"), jobs=args.jobs)
''',
)
replace_once(
    "src/opencollate/cli.py",
    '''    result = _run(config)
    if result.exit_code == 2:
''',
    '''    result = _run(config, jobs=args.jobs)
    if result.exit_code == 2:
''',
)

replace_once(
    "tests/test_plugins.py",
    '''        provider="test-suite",
        version="1.0",
    )
''',
    '''        provider="test-suite",
        version="1.0",
        parallel_safe=True,
    )
''',
)
replace_once(
    "tests/test_plugins.py",
    '''            "plugin": "toy_parser",
            "builtin": False,
        }
''',
    '''            "plugin": "toy_parser",
            "builtin": False,
            "parallel_safe": True,
        }
''',
)
replace_once(
    "tests/test_plugins.py",
    '''def test_plugin_api_version_is_explicitly_rejected() -> None:
    with pytest.raises(PluginContractError, match="unsupported"):
        ParserPluginSpec(parser=_ToyParser(), api_version=PLUGIN_API_VERSION + 1)
''',
    '''def test_plugin_api_version_is_explicitly_rejected() -> None:
    with pytest.raises(PluginContractError, match="unsupported"):
        ParserPluginSpec(parser=_ToyParser(), api_version=PLUGIN_API_VERSION + 1)
    with pytest.raises(PluginContractError, match="parallel_safe"):
        ParserPluginSpec(parser=_ToyParser(), parallel_safe=1)  # type: ignore[arg-type]
''',
)

replace_once(
    "docs/extension-api.md",
    '''    aliases=("oas",),
    extensions=(".oas", ".oasis"),
)
''',
    '''    aliases=("oas",),
    extensions=(".oas", ".oasis"),
    parallel_safe=True,
)
''',
)
replace_once(
    "docs/extension-api.md",
    '''The entry point may expose a `ParserPluginSpec`, a parser instance, a parser class, or a zero-argument
factory. A spec is preferred because it declares aliases and filename extensions.
''',
    '''The entry point may expose a `ParserPluginSpec`, a parser instance, a parser class, or a zero-argument
factory. A spec is preferred because it declares aliases, filename extensions, and whether distinct
parser calls can safely overlap. Plugins default to `parallel_safe=False`; set it to true only when
the parser has no shared mutable state and its dependencies explicitly support concurrent calls.
Built-in parsers declare themselves safe. `--jobs N` never overlaps an unsafe plugin with any other
parser work.
''',
)
replace_once(
    "docs/extension-api.md",
    '''- plugin order is deterministic;
- plugin exceptions remain fatal and retain provider/version metadata;
''',
    '''- plugin order and parallel result order are deterministic;
- plugins remain serial unless they explicitly declare `parallel_safe=True`;
- plugin exceptions remain fatal and retain provider/version metadata;
''',
)

replace_once(
    "README.md",
    '''opencollate check [CONFIG]                  # default: opencollate.toml
''',
    '''opencollate check [CONFIG] [--jobs N]       # default: opencollate.toml, one parser worker
''',
)
replace_once(
    "README.md",
    '''- **CI native:** deterministic text, JSON, Markdown, SARIF, report-diff, and contract artifacts.
''',
    '''- **CI native:** deterministic text, JSON, Markdown, SARIF, report-diff, and contract artifacts.
- **Bounded parallel parsing:** opt-in workers preserve configuration order and isolate unsafe plugins.
''',
)

replace_once(
    "CHANGELOG.md",
    '''### Added

- An oracle-backed semantic mutation benchmark with 34 paired mutants and clean controls across
''',
    '''### Added

- Opt-in deterministic parallel parsing through `--jobs N` for check, review, demo, and contract
  build. Built-ins are marked parallel-safe; third-party parsers remain hard serial barriers unless
  they explicitly declare `parallel_safe=True` in their versioned capability metadata.
- An oracle-backed semantic mutation benchmark with 34 paired mutants and clean controls across
''',
)
