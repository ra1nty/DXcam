from __future__ import annotations

from typing import Any, ContextManager, Protocol, runtime_checkable


@runtime_checkable
class FrameDuplicator(Protocol):
    """Minimal capture backend contract consumed by DXCamera/runtime."""

    texture: Any

    def acquire_frame(
        self, wait_for_frame: bool = False
    ) -> ContextManager[tuple[bool, bool, int]]: ...

    def ticks_to_seconds(self, ticks: int) -> float: ...

    def release(self) -> None: ...
