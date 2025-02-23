from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import random
import string
import uuid
from asyncio import Task, Future
from collections import defaultdict
from collections.abc import Iterable
from contextlib import suppress
from datetime import timedelta, datetime, timezone
from typing import (
    Any,
    Callable,
    Optional,
    Awaitable,
    TypeVar,
    Mapping,
    AsyncGenerator,
    Dict,
    List,
    Tuple,
    AsyncIterator,
    Iterator,
    Union,
    Sequence,
)

import sys
from dateutil.parser import isoparse

from resotocore.durations import parse_duration
from resotocore.error import RestartService
from resotocore.types import JsonElement, Json

log = logging.getLogger(__name__)

AnyT = TypeVar("AnyT")
AnyR = TypeVar("AnyR")


def identity(o: AnyT) -> AnyT:
    return o


def count_iterator(start: int = 0) -> Iterator[int]:
    return iter(range(start, sys.maxsize))


def json_hash(js: Json) -> str:
    sha256 = hashlib.sha256()
    sha256.update(json.dumps(js, sort_keys=True).encode("utf-8"))
    return sha256.hexdigest()


def pop_keys(d: Dict[AnyT, AnyR], keys: List[AnyT]) -> Dict[AnyT, AnyR]:
    res = dict(d)
    for key in keys:
        res.pop(key, None)  # type: ignore
    return res


UTC_Date_Format = "%Y-%m-%dT%H:%M:%SZ"


def rnd_str(str_len: int = 10) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(str_len))


def utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_str(dt: datetime = utc()) -> str:
    return dt.strftime(UTC_Date_Format)


def from_utc(date_string: str) -> datetime:
    return isoparse(date_string)


def duration(d: str) -> timedelta:
    return parse_duration(d)


def uuid_str(from_object: Optional[Any] = None) -> str:
    if from_object:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, from_object))
    else:
        return str(uuid.uuid1())


def group_by(f: Callable[[AnyT], AnyR], iterable: Iterable) -> Dict[AnyR, List[AnyT]]:  # type: ignore # pypy
    """
    Group iterable by key provided by given key function.
    :param f: the function to be applied on every element that yields the key.
    :param iterable: the iterable to walk.
    :return: a dictionary with the keys provided by the key function and the values from the iterable.
    """
    v = defaultdict(list)
    for item in iterable:
        key = f(item)
        v[key].append(item)
    return v


def non_empty(el: Iterable) -> bool:  # type: ignore # pypy
    return bool(el)


def empty(el: Iterable) -> bool:  # type: ignore # pypy
    return not non_empty(el)


def combine_optional(
    left: Optional[AnyT], right: Optional[AnyT], combine: Callable[[AnyT, AnyT], AnyT]
) -> Optional[AnyT]:
    if left and right:
        return combine(left, right)
    elif left:
        return left
    else:
        return right


def interleave(elements: List[AnyT]) -> List[Tuple[AnyT, AnyT]]:
    if len(elements) < 2:
        return []
    else:
        nxt = iter(elements)
        next(nxt)
        return list(zip(elements, nxt))


# we expect a callable that returns a truthy value.
# Due to limitations of lambda expressions we use Any here.
def exist(f: Callable[[Any], Any], iterable: Iterable) -> bool:  # type: ignore # pypy
    """
    Items are passed to the callable as long as it returns False.
    Return True once the callable finds one True, otherwise return False.
    :param f: the callable that needs to accept items from Iterable.
    :param iterable: walk over this iterable
    :return: True if the item exist, otherwise False.
    """
    for a in iterable:
        if f(a):
            return True
    return False


# we expect a callable that returns a truthy value.
# Due to limitations of lambda expressions we use Any here.
def first(f: Callable[[Any], Any], iterable: Iterable) -> Optional[Any]:  # type: ignore # pypy
    for a in iterable:
        if f(a):
            return a
    return None


def if_set(x: Optional[AnyT], func: Callable[[AnyT], Any], if_not: Any = None) -> Any:
    """
    Conditional execute based if the option is defined.
    :param x: the value to check.
    :param func: the function to call if the item is defined.
    :param if_not: the value to return if the item is not defined.
    :return: the result of the function or if_not
    """
    return func(x) if x is not None else if_not


def value_in_path_get(element: JsonElement, path_or_name: Union[List[str], str], if_none: AnyT) -> AnyT:
    result = value_in_path(element, path_or_name)
    return result if result and isinstance(result, type(if_none)) else if_none


def value_in_path(element: JsonElement, path_or_name: Union[List[str], str]) -> Optional[Any]:
    path = path_or_name if isinstance(path_or_name, list) else path_or_name.split(".")
    at = len(path)

    def at_idx(current: JsonElement, idx: int) -> Optional[Any]:
        if at == idx:
            return current
        elif current is None or not isinstance(current, dict) or path[idx] not in current:
            return None
        else:
            return at_idx(current[path[idx]], idx + 1)

    return at_idx(element, 0)


def deep_merge(left: Json, right: Json) -> Json:
    """
    Merge the right json into the left json.
    All values in right will be set on the left side.
    All values not existing in right will be preserved on the left side.
    :return: the deeply merged json object.
    """

    def merge(key: str) -> JsonElement:
        left_value: JsonElement = left.get(key)
        right_value: JsonElement = right.get(key)
        if isinstance(right_value, dict):
            left_value = left_value if isinstance(left_value, dict) else {}
            # noinspection PyTypeChecker
            return deep_merge(left_value, right_value)
        elif right_value:
            return right_value
        else:
            return left_value

    return {k: merge(k) for k in set(left.keys()).union(right.keys())}


def set_value_in_path(element: JsonElement, path_or_name: Union[List[str], str], js: Optional[Json] = None) -> Json:
    path = path_or_name if isinstance(path_or_name, list) else path_or_name.split(".")
    at = len(path) - 1

    def at_idx(current: Json, idx: int) -> None:
        if at == idx:
            current[path[-1]] = element
        else:
            value = current.get(path[idx])
            if not isinstance(value, dict):
                value = {}
                current[path[idx]] = value
            at_idx(value, idx + 1)

    js = js if js is not None else {}
    at_idx(js, 0)
    return js


def del_value_in_path(element: JsonElement, path: List[str]) -> JsonElement:
    pl = len(path) - 1

    def at_idx(current: JsonElement, idx: int) -> JsonElement:
        if current is None or not isinstance(current, dict) or path[idx] not in current:
            return element
        elif pl == idx:
            current.pop(path[-1], None)
            return element
        else:
            result = at_idx(current[path[idx]], idx + 1)
            if not current[path[idx]]:
                current[path[idx]] = None
            return result

    return at_idx(element, 0)


async def force_gen(gen: AsyncIterator[AnyT]) -> AsyncIterator[AnyT]:
    async def with_first(elem: AnyT) -> AsyncGenerator[AnyT, None]:
        yield elem
        async for a in gen:
            yield a

    try:
        return with_first(await gen.__anext__())
    except StopAsyncIteration:
        return gen


def set_future_result(future: Future, result: Any) -> None:  # type: ignore # pypy
    if not future.done():
        if isinstance(result, Exception):
            future.set_exception(result)
        else:
            future.set_result(result)


def __mute_async_exception_reporting_on_current_loop() -> None:
    # Exceptions happening in the async loop during shutdown.
    def exception_handler(_: Any, context: Any) -> None:
        log.debug(f"Error from async loop during shutdown: {context}")

    log.info("Shutdown initiated for current process.")
    with suppress(Exception):
        loop = asyncio.get_running_loop()
        if loop:
            loop.set_exception_handler(exception_handler)


def restart_service(reason: str) -> None:
    __mute_async_exception_reporting_on_current_loop()
    raise RestartService(reason)


def shutdown_process(exit_code: int) -> None:
    __mute_async_exception_reporting_on_current_loop()
    sys.exit(exit_code)


class Periodic:
    """
    Periodic execution of a function based on a defined frequency that can be started and stopped.
    """

    def __init__(self, name: str, func: Callable[[], Any], frequency: timedelta, first_run: Optional[timedelta] = None):
        self.name = name
        self.func = func
        self.frequency = frequency
        self.first_run = first_run if first_run else frequency
        self._task: Optional[Task] = None  # type: ignore # pypy

    @property
    def started(self) -> bool:
        return self._task is not None

    async def start(self) -> None:
        if self._task is None:
            # Start task to call func periodically:
            self._task = asyncio.ensure_future(self._run())
            log.info(f"Periodic task {self.name} has been started.")

    async def stop(self) -> None:
        # Stop task and await it stopped:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        await asyncio.sleep(self.first_run.total_seconds())
        while True:
            log.debug(f"Execute periodic task {self.name}.")
            try:
                result = self.func()
                if isinstance(result, Awaitable):
                    await result
            except Exception as ex:
                log.error(f"Periodic function {self.name} caught an exception: {ex}", exc_info=ex)
            await asyncio.sleep(self.frequency.total_seconds())


class AccessNone:
    def __init__(self, not_existent: Any = None):
        self.__not_existent = not_existent

    def __getitem__(self, item: Any) -> Any:
        return self

    def __getattr__(self, name: Any) -> Any:
        return self

    def __str__(self) -> str:
        return str(self.__not_existent)

    def __eq__(self, other: Any) -> bool:
        return other is None or isinstance(other, AccessNone)

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> None:
        raise StopIteration()

    @property
    def is_none(self) -> bool:
        return True


class AccessJson(Dict[Any, Any]):
    """
    Extend dict in order to allow python like property access
    as well as exception safe access for non existent properties.
    """

    def __init__(
        self, mapping: Mapping[Any, Any], not_existent: Any = None, simple_formatter: Callable[[Any], Any] = identity
    ) -> None:
        super().__init__(mapping)
        self.__not_existent = AccessNone(not_existent)
        self.__simple_formatter = simple_formatter

    def __getitem__(self, item: Any) -> Any:
        if item in self:
            iv = super().__getitem__(item)
            return (
                AccessJson.wrap(iv, self.__not_existent, self.__simple_formatter)
                if iv is not None
                else self.__not_existent
            )
        else:
            return self.__not_existent

    def __getattr__(self, name: Any) -> Any:
        return self.__getitem__(name)

    def __str__(self) -> str:
        return json.dumps(self)

    @property
    def is_none(self) -> bool:
        return False

    @staticmethod
    def wrap(obj: Any, not_existent: Any = AccessNone(None), simple_formatter: Callable[[Any], Any] = identity) -> Any:
        if isinstance(obj, (str, int, float, AccessJson)):
            return simple_formatter(obj)
        # dict like data structure -> wrap whole element
        elif isinstance(obj, Mapping):
            return AccessJson(obj, not_existent, simple_formatter)
        # list like data structure -> wrap all elements
        elif isinstance(obj, Sequence):
            return [AccessJson.wrap(item, not_existent, simple_formatter) for item in obj]
        # simply return the object
        else:
            return obj

    @staticmethod
    def wrap_list(
        obj: Any, not_existent: Any = AccessNone(None), simple_formatter: Callable[[Any], Any] = identity
    ) -> List[AccessJson]:
        # only here for a typed result
        return AccessJson.wrap(obj, not_existent, simple_formatter)  # type: ignore

    @staticmethod
    def wrap_object(
        obj: Any, not_existent: Any = AccessNone(None), simple_formatter: Callable[[Any], Any] = identity
    ) -> AccessJson:
        # only here for a typed result
        return AccessJson.wrap(obj, not_existent, simple_formatter)  # type: ignore
