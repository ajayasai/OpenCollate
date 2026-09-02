"""Apply the contract-v2 integration using exact, fail-fast source anchors."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one replacement target, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace_once(
    "src/opencollate/model.py",
    '''from typing import Any, Generic, TypeVar


class FactState''',
    '''from typing import Any, Generic, TypeVar

from opencollate.contracts import (
    CONTRACT_SCHEMA_VERSION,
    ContractViewSnapshot,
    normalize_contract_extensions,
)


class FactState''',
)

replace_once(
    "src/opencollate/model.py",
    '''@dataclass(frozen=True, slots=True)
class DesignContract:
    components: tuple[ContractComponent, ...]
    schema_version: int = 1
    generated_by: str = "OpenCollate"
    registers: tuple[ContractRegister, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        object.__setattr__(self, "registers", tuple(self.registers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_by": self.generated_by,
            "components": [
                item.to_dict()
                for item in sorted(self.components, key=lambda item: item.canonical_name)
            ],
            "registers": [
                item.to_dict()
                for item in sorted(
                    self.registers,
                    key=lambda item: (
                        item.component,
                        item.memory_map or "",
                        item.address_block or "",
                        item.canonical_name,
                    ),
                )
            ],
        }

''',
    '''@dataclass(frozen=True, slots=True)
class DesignContract:
    components: tuple[ContractComponent, ...]
    schema_version: int = CONTRACT_SCHEMA_VERSION
    generated_by: str = "OpenCollate"
    registers: tuple[ContractRegister, ...] = ()
    views: tuple[ContractViewSnapshot, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("contract schema_version must be an integer")
        if self.schema_version not in {1, CONTRACT_SCHEMA_VERSION}:
            raise ValueError(
                f"unsupported contract schema_version {self.schema_version}; "
                f"this release supports 1 and {CONTRACT_SCHEMA_VERSION}"
            )
        if not isinstance(self.generated_by, str):
            raise TypeError("contract generated_by must be a string")
        components = tuple(self.components)
        registers = tuple(self.registers)
        views = tuple(self.views)
        if not all(isinstance(item, ContractViewSnapshot) for item in views):
            raise TypeError("contract views must contain ContractViewSnapshot values")
        view_names = [item.view for item in views]
        if len(view_names) != len(set(view_names)):
            raise ValueError("contract views must have unique view identities")
        extensions = normalize_contract_extensions(self.extensions)
        if self.schema_version == 1 and (views or extensions):
            raise ValueError("contract schema_version 1 cannot contain views or extensions")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "registers", registers)
        object.__setattr__(self, "views", tuple(sorted(views, key=lambda item: item.view)))
        object.__setattr__(self, "extensions", extensions)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "generated_by": self.generated_by,
            "components": [
                item.to_dict()
                for item in sorted(self.components, key=lambda item: item.canonical_name)
            ],
            "registers": [
                item.to_dict()
                for item in sorted(
                    self.registers,
                    key=lambda item: (
                        item.component,
                        item.memory_map or "",
                        item.address_block or "",
                        item.canonical_name,
                    ),
                )
            ],
        }
        if self.schema_version >= CONTRACT_SCHEMA_VERSION:
            result["views"] = [item.to_dict() for item in self.views]
            result["extensions"] = dict(self.extensions)
        return result

''',
)

replace_once(
    "src/opencollate/model.py",
    '''        if schema_version != 1:
            raise ValueError(
                f"unsupported schema_version {schema_version}; this release supports 1"
            )
''',
    '''        if schema_version not in {1, CONTRACT_SCHEMA_VERSION}:
            raise ValueError(
                f"unsupported schema_version {schema_version}; "
                f"this release supports 1 and {CONTRACT_SCHEMA_VERSION}"
            )
''',
)

replace_once(
    "src/opencollate/model.py",
    '''        return cls(
            tuple(components),
            schema_version=schema_version,
            generated_by=generated_by,
            registers=tuple(registers),
        )
''',
    '''        if schema_version == 1:
            if "views" in data or "extensions" in data:
                raise ValueError("contract schema_version 1 cannot contain views or extensions")
            views: tuple[ContractViewSnapshot, ...] = ()
            extensions: dict[str, Any] = {}
        else:
            if "views" not in data:
                raise TypeError("views must be an array for contract schema_version 2")
            raw_views = data.get("views")
            if not isinstance(raw_views, (list, tuple)):
                raise TypeError("views must be an array")
            views = tuple(
                ContractViewSnapshot.from_dict(item)
                if isinstance(item, Mapping)
                else _raise_contract_view_type(index)
                for index, item in enumerate(raw_views)
            )
            if "extensions" not in data:
                raise TypeError("extensions must be an object for contract schema_version 2")
            raw_extensions = data.get("extensions")
            if not isinstance(raw_extensions, Mapping):
                raise TypeError("extensions must be an object")
            extensions = normalize_contract_extensions(raw_extensions)
        return cls(
            tuple(components),
            schema_version=schema_version,
            generated_by=generated_by,
            registers=tuple(registers),
            views=views,
            extensions=extensions,
        )
''',
)

replace_once(
    "src/opencollate/model.py",
    '''
def decoded_identifier(name: str) -> str:
''',
    '''
def _raise_contract_view_type(index: int) -> ContractViewSnapshot:
    raise TypeError(f"views[{index}] must be an object")


def decoded_identifier(name: str) -> str:
''',
)

replace_once(
    "src/opencollate/engine.py",
    '''from opencollate.config import AliasRule, ProjectConfig, Waiver
from opencollate.diagnostics import (
''',
    '''from opencollate.config import AliasRule, ProjectConfig, Waiver
from opencollate.contracts import CONTRACT_SCHEMA_VERSION, snapshots_from_observations
from opencollate.diagnostics import (
''',
)

replace_once(
    "src/opencollate/engine.py",
    '''        return DesignContract(
            tuple(components),
            registers=self._build_contract_registers(observations),
        )
''',
    '''        return DesignContract(
            tuple(components),
            schema_version=CONTRACT_SCHEMA_VERSION,
            registers=self._build_contract_registers(observations),
            views=snapshots_from_observations(observations),
        )
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''from opencollate.config import ConfigError, ProjectConfig, SourceConfig, load_config
from opencollate.demo import write_demo
''',
    '''from opencollate.config import ConfigError, ProjectConfig, SourceConfig, load_config, load_contract
from opencollate.contracts import upgrade_contract
from opencollate.demo import write_demo
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''    build.add_argument("-o", "--output", default="contract.oc.json")
    build.set_defaults(handler=_command_contract_build)
    return parser
''',
    '''    build.add_argument("-o", "--output", default="contract.oc.json")
    build.set_defaults(handler=_command_contract_build)
    migrate = contract_subparsers.add_parser(
        "migrate",
        help="upgrade a v1 contract to the current schema without inventing unavailable facts",
    )
    migrate.add_argument("input", help="existing OpenCollate contract JSON")
    migrate.add_argument("-o", "--output", help="destination (default: INPUT stem plus .v2)")
    migrate.set_defaults(handler=_command_contract_migrate)
    return parser
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''    print(f"Wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
''',
    '''    print(f"Wrote {target}")
    return 0


def _command_contract_migrate(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser().resolve()
    contract = load_contract(source)
    upgraded = upgrade_contract(contract)
    destination = (
        Path(args.output).expanduser().resolve()
        if args.output
        else source.with_name(f"{source.stem}.v2{source.suffix}")
    )
    try:
        target = write_contract(upgraded, destination)
    except OSError as error:
        raise CliError(f"cannot write contract {destination}: {error}") from error
    print(f"Wrote schema {upgraded.schema_version} contract to {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
''',
)

replace_once(
    "tests/test_config_edges.py",
    '''        ('{"schema_version": 2, "components": []}', "schema_version"),
''',
    '''        ('{"schema_version": 3, "components": []}', "schema_version"),
''',
)
