from __future__ import annotations

import inspect
import json
from abc import ABC, abstractmethod
from asyncio import Queue, Task, iscoroutine
from dataclasses import dataclass, field
from enum import Enum
from operator import attrgetter
from textwrap import dedent
from typing import Optional, List, Any, Dict, Tuple, Callable, Union, Awaitable, Type, cast, Set

from aiohttp import ClientSession, TCPConnector
from aiostream import stream
from aiostream.core import Stream
from rich.jupyter import JupyterMixin
from parsy import test_char, string

from resotocore.analytics import AnalyticsEventSender
from resotocore.cli import JsGen, T, Sink
from resotocore.config import ConfigHandler
from resotocore.core_config import CoreConfig, AliasTemplateConfig, AliasTemplateParameterConfig
from resotocore.db.db_access import DbAccess
from resotocore.error import CLIParseError
from resotocore.console_renderer import ConsoleRenderer, ConsoleColorSystem
from resotocore.message_bus import MessageBus
from resotocore.parse_util import l_curly_dp, r_curly_dp
from resotocore.model.model_handler import ModelHandler
from resotocore.query.model import Query, variable_to_absolute, PathRoot
from resotocore.query.template_expander import TemplateExpander, render_template
from resotocore.task import TaskHandler
from resotocore.types import Json, JsonElement
from resotocore.util import AccessJson
from resotocore.worker_task_queue import WorkerTaskQueue


class MediaType(Enum):
    Json = 1
    FilePath = 2

    @property
    def json(self) -> bool:
        return self == MediaType.Json

    @property
    def file_path(self) -> bool:
        return self == MediaType.FilePath

    def __repr__(self) -> str:
        return "application/json" if self == MediaType.Json else "application/octet-stream"


no_closing_p = test_char(lambda x: x != "}", "No closing bracket").at_least(1).concat()
no_bracket_p = test_char(lambda x: x not in ("{", "}"), "No opening bracket").at_least(1).concat()
double_curly_open_dp = string("{{")
double_curly_close_dp = string("}}")
l_or_r_curly_dp = string("{") | string("}")


@dataclass(frozen=True)
class CLIContext:
    env: Dict[str, str] = field(default_factory=dict)
    uploaded_files: Dict[str, str] = field(default_factory=dict)  # id -> path
    query: Optional[Query] = None
    query_options: Dict[str, Any] = field(default_factory=dict)
    console_renderer: Optional[ConsoleRenderer] = None

    def variable_in_section(self, variable: str) -> str:
        # if there is no query, always assume the root section
        section = self.env.get("section") if self.query else PathRoot
        return variable_to_absolute(section, variable)

    def render_console(self, element: Union[str, JupyterMixin]) -> str:
        if self.console_renderer:
            return self.console_renderer.render(element)
        elif isinstance(element, JupyterMixin):
            return str(element)
        else:
            return element

    def supports_color(self) -> bool:
        return (
            self.console_renderer is not None
            and self.console_renderer.color_system is not None
            and self.console_renderer.color_system != ConsoleColorSystem.monochrome
        )

    def formatter(self, format_string: str) -> Callable[[Json], str]:
        return self.formatter_with_variables(format_string, False)[0]

    def formatter_with_variables(
        self, format_string: str, collect_variables: bool = True
    ) -> Tuple[Callable[[Json], str], Optional[Set[str]]]:
        """
        A renderer can be used to string format objects based on a provided format string.
        """

        variables: Optional[Set[str]] = set() if collect_variables else None

        def format_variable(name: str) -> str:
            assert "__" not in name, "No dunder attributes allowed"
            in_section = self.variable_in_section(name)
            if collect_variables:
                variables.add(in_section)  # type: ignore
            return "{" + in_section + "}"

        def render_simple_property(prop: Any) -> str:
            return json.dumps(prop) if isinstance(prop, bool) else str(prop)

        variable = (l_curly_dp >> no_closing_p << r_curly_dp).map(format_variable)
        token = double_curly_open_dp | double_curly_close_dp | no_bracket_p | variable | l_or_r_curly_dp
        format_string_parser = token.many().concat()
        formatter: str = format_string_parser.parse(format_string)

        def format_object(obj: Any) -> str:
            return formatter.format_map(AccessJson.wrap(obj, "null", render_simple_property))

        return format_object, variables


EmptyContext = CLIContext()


class CLIEngine(ABC):
    @abstractmethod
    async def evaluate_cli_command(
        self, cli_input: str, context: CLIContext = EmptyContext, replace_place_holder: bool = True
    ) -> List[ParsedCommandLine]:
        pass


class CLIDependencies:
    def __init__(self, **deps: Any) -> None:
        self.lookup: Dict[str, Any] = deps

    def extend(self, **deps: Any) -> CLIDependencies:
        self.lookup = {**self.lookup, **deps}
        return self

    @property
    def config(self) -> CoreConfig:
        return self.lookup["config"]  # type: ignore

    @property
    def message_bus(self) -> MessageBus:
        return self.lookup["message_bus"]  # type:ignore

    @property
    def event_sender(self) -> AnalyticsEventSender:
        return self.lookup["event_sender"]  # type:ignore

    @property
    def db_access(self) -> DbAccess:
        return self.lookup["db_access"]  # type:ignore

    @property
    def model_handler(self) -> ModelHandler:
        return self.lookup["model_handler"]  # type:ignore

    @property
    def task_handler(self) -> TaskHandler:
        return self.lookup["task_handler"]  # type:ignore

    @property
    def worker_task_queue(self) -> WorkerTaskQueue:
        return self.lookup["worker_task_queue"]  # type:ignore

    @property
    def template_expander(self) -> TemplateExpander:
        return self.lookup["template_expander"]  # type:ignore

    @property
    def forked_tasks(self) -> Queue[Tuple[Task[JsonElement], str]]:
        return self.lookup["forked_tasks"]  # type:ignore

    @property
    def cli(self) -> CLIEngine:
        return self.lookup["cli"]  # type:ignore

    @property
    def config_handler(self) -> ConfigHandler:
        return self.lookup["config_handler"]  # type:ignore

    @property
    def http_session(self) -> ClientSession:
        session: Optional[ClientSession] = self.lookup.get("http_session")
        if not session:
            connector = TCPConnector(limit=0, ssl=False, ttl_dns_cache=300)
            session = ClientSession(connector=connector)
            self.lookup["http_session"] = session
        return session

    async def stop(self) -> None:
        if "http_session" in self.lookup:
            await self.http_session.close()


@dataclass
class CLICommandRequirement:
    name: str


@dataclass
class CLIFileRequirement(CLICommandRequirement):
    path: str  # local client path


class CLIAction(ABC):
    def __init__(
        self, produces: MediaType, requires: Optional[List[CLICommandRequirement]], envelope: Optional[Dict[str, str]]
    ) -> None:
        self.produces = produces
        self.required = requires or []
        self.envelope: Dict[str, str] = envelope or {}

    @staticmethod
    def make_stream(in_stream: JsGen) -> Stream:
        return in_stream if isinstance(in_stream, Stream) else stream.iterate(in_stream)


class CLISource(CLIAction):
    def __init__(
        self,
        fn: Callable[[], Union[Tuple[Optional[int], JsGen], Awaitable[Tuple[Optional[int], JsGen]]]],
        produces: MediaType = MediaType.Json,
        requires: Optional[List[CLICommandRequirement]] = None,
        envelope: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(produces, requires, envelope)
        self._fn = fn

    async def source(self) -> Tuple[Optional[int], Stream]:
        res = self._fn()
        count, gen = await res if iscoroutine(res) else res
        return count, self.make_stream(await gen if iscoroutine(gen) else gen)

    @staticmethod
    def with_count(
        fn: Callable[[], Union[JsGen, Awaitable[JsGen]]],
        count: Optional[int],
        produces: MediaType = MediaType.Json,
        requires: Optional[List[CLICommandRequirement]] = None,
        envelope: Optional[Dict[str, str]] = None,
    ) -> CLISource:
        async def combine() -> Tuple[Optional[int], JsGen]:
            res = fn()
            gen = await res if iscoroutine(res) else res
            return count, gen

        return CLISource(combine, produces, requires, envelope)

    @staticmethod
    def single(
        fn: Callable[[], Union[JsGen, Awaitable[JsGen]]],
        produces: MediaType = MediaType.Json,
        requires: Optional[List[CLICommandRequirement]] = None,
        envelope: Optional[Dict[str, str]] = None,
    ) -> CLISource:
        return CLISource.with_count(fn, 1, produces, requires, envelope)

    @staticmethod
    def empty() -> CLISource:
        return CLISource.with_count(stream.empty, 0)


class CLIFlow(CLIAction):
    def __init__(
        self,
        fn: Callable[[JsGen], Union[JsGen, Awaitable[JsGen]]],
        produces: MediaType = MediaType.Json,
        requires: Optional[List[CLICommandRequirement]] = None,
        envelope: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(produces, requires, envelope)
        self._fn = fn

    async def flow(self, in_stream: JsGen) -> Stream:
        gen = self._fn(self.make_stream(in_stream))
        return self.make_stream(await gen if iscoroutine(gen) else gen)


class CLICommand(ABC):
    """
    The CLIPart is the base for all participants of the cli execution.
    Source: generates a stream of objects
    Flow: transforms the elements in a stream of objects
    Sink: takes a stream of objects and creates a result
    """

    def __init__(self, dependencies: CLIDependencies):
        self.dependencies = dependencies

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    def help(self) -> str:
        # if not defined in subclass, fallback to inline doc
        doc = inspect.getdoc(type(self))
        return doc if doc else f"{self.name}: no help available."

    def rendered_help(self, ctx: CLIContext) -> str:
        text = f"\n**{self.name}: {self.info()}**\n\n{self.help()}"
        return ctx.render_console(text)

    @abstractmethod
    def info(self) -> str:
        pass

    @abstractmethod
    def parse(self, arg: Optional[str] = None, ctx: CLIContext = EmptyContext, **kwargs: Any) -> CLIAction:
        pass


@dataclass(order=True, unsafe_hash=True, frozen=True)
class AliasTemplateParameter:
    name: str
    description: str
    default: Optional[JsonElement] = None

    def example_value(self) -> JsonElement:
        return self.default if self.default else f"test_{self.name}"


@dataclass(order=True, unsafe_hash=True, frozen=True)
class AliasTemplate:
    name: str
    info: str
    template: str
    parameters: List[AliasTemplateParameter] = field(default_factory=list)

    def render(self, props: Json) -> str:
        return render_template(self.template, props)

    def help(self) -> str:
        args = ", ".join(f"{arg.name}=<value>" for arg in self.parameters)

        def param_info(p: AliasTemplateParameter) -> str:
            default = f" [default: {p.default}]" if p.default else ""
            return f"- `{p.name}`{default}: {p.description}"

        indent = "                "
        arg_info = f"\n{indent}".join(param_info(arg) for arg in sorted(self.parameters, key=attrgetter("name")))
        minimal = ", ".join(f'{p.name}="{p.example_value()}"' for p in self.parameters if p.default is None)
        return dedent(
            f"""
                {self.name}: {self.info}
                ```shell
                {self.name} {args}
                ```
                ## Parameters
                {arg_info}

                ## Template
                ```shell
                > {self.template}
                ```

                ## Example
                ```shell
                # Executing this alias template
                > {self.name} {minimal}
                # Will expand to this command
                > {self.render({p.name: p.example_value() for p in self.parameters})}
                ```
                """
        )

    def rendered_help(self, ctx: CLIContext) -> str:
        return ctx.render_console(self.help())

    @staticmethod
    def from_config(cfg: AliasTemplateConfig) -> AliasTemplate:
        def arg(p: AliasTemplateParameterConfig) -> AliasTemplateParameter:
            return AliasTemplateParameter(p.name, p.description, p.default)

        return AliasTemplate(cfg.name, cfg.info, cfg.template, [arg(a) for a in cfg.parameters])


class InternalPart(ABC):
    """
    Internal parts can be executed but are not shown via help.
    They usually get injected by the CLI Interpreter to ease usability.
    """


class OutputTransformer(ABC):
    """
    Mark all commands that transform the output stream (formatting).
    """


class PreserveOutputFormat(ABC):
    """
    Mark all commands where the output should not be flattened to default line output.
    """


class NoTerminalOutput(ABC):
    """
    Mark all commands where the output should not contain any terminal escape codes.
    """


@dataclass
class ParsedCommand:
    cmd: str
    args: Optional[str] = None


@dataclass
class ParsedCommands:
    commands: List[ParsedCommand]
    env: Json = field(default_factory=dict)


@dataclass
class ExecutableCommand:
    name: str  # the name of the command or alias
    command: CLICommand
    arg: Optional[str]
    action: CLIAction


@dataclass
class ParsedCommandLine:
    """
    The parsed command line holds:
    - ctx: the resulting environment coming from the parsed environment + the provided environment
    - commands: all commands this command is defined from
    - generator: this generator can be used in order to execute the command line
    """

    ctx: CLIContext
    parsed_commands: ParsedCommands
    executable_commands: List[ExecutableCommand]
    unmet_requirements: List[CLICommandRequirement]
    envelope: Dict[str, str]

    def __post_init__(self) -> None:
        def expect_action(cmd: ExecutableCommand, expected: Type[T]) -> T:
            action = cmd.action
            if isinstance(action, expected):
                return action
            else:
                message = "must be the first command" if issubclass(type(action), CLISource) else "no source data given"
                raise CLIParseError(f"Command >{cmd.command.name}< can not be used in this position: {message}")

        if self.executable_commands:
            expect_action(self.executable_commands[0], CLISource)
            for command in self.executable_commands[1:]:
                expect_action(command, CLIFlow)

    async def to_sink(self, sink: Sink[T]) -> T:
        _, generator = await self.execute()
        return await sink(generator)

    @property
    def commands(self) -> List[CLICommand]:
        return [part.command for part in self.executable_commands]

    @property
    def produces(self) -> MediaType:
        # the last command in the chain defines the resulting media type
        return self.executable_commands[-1].action.produces if self.executable_commands else MediaType.Json

    async def execute(self) -> Tuple[Optional[int], Stream]:
        if self.executable_commands:
            source_action = cast(CLISource, self.executable_commands[0].action)
            count, flow = await source_action.source()
            for command in self.executable_commands[1:]:
                flow_action = cast(CLIFlow, command.action)
                flow = await flow_action.flow(flow)
            return count, flow
        else:
            return 0, stream.empty()
