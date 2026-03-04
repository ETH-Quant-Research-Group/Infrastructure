from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    import types

T = TypeVar("T")


def discover_subclasses(package: types.ModuleType, base: type[T]) -> list[type[T]]:
    """Import every module in *package* and return all concrete subclasses of *base*.

    Walks the full subclass tree recursively, so grandchildren (e.g.
    ``BinanceFuturesClient`` → ``BaseCryptoFuturesClient`` → ``BaseCryptoClient``) are
    included when searching for ``BaseCryptoClient``.
    """
    for _, modname, _ in pkgutil.walk_packages(
        package.__path__, package.__name__ + "."
    ):
        importlib.import_module(modname)
    return _collect(base)


def _collect(base: type[T]) -> list[type[T]]:
    result: list[type[T]] = []
    for sub in base.__subclasses__():
        if not inspect.isabstract(sub):
            result.append(sub)
        result.extend(_collect(sub))
    return result
