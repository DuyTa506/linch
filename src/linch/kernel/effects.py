"""Reversible effects: the teardown primitive the whole kernel is built on.

Ported from Cordis' ``fiber.effect`` (deepseek-harness/vendor/cordis/src/fiber.ts).
A :class:`Disposable` runs one teardown attempt at a time and awaits async
teardown. Successful teardown is final; a failed attempt remains observable to
its waiters and may be retried. An :class:`EffectScope` collects disposers and
unwinds them in reverse registration order, so tearing a scope down undoes its
contributions in the opposite order they were made — the discipline every
registration relies on.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

# A teardown callback: returns ``None`` for sync cleanup, or an awaitable that is
# awaited before the disposer is considered done.
DisposerFn = Callable[[], Any]


class Disposable:
    """An idempotent, async-aware teardown handle.

    ``await d.dispose()`` (or ``await d()``) coalesces concurrent calls into one
    shielded teardown attempt. Successful cleanup is final; failed cleanup is
    reported to every waiter and can be retried. This is what every ``register``/
    ``on``/``effect`` hands back so a contribution can be reversed.

    Args:
        fn: Optional synchronous or asynchronous cleanup callback.
    """

    __slots__ = ("_fn", "_disposed", "_task", "_scopes")

    def __init__(self, fn: DisposerFn | None = None) -> None:
        """Create a teardown handle around an optional cleanup callback.

        Args:
            fn: Synchronous or asynchronous callback run when disposal begins.
        """
        self._fn = fn
        self._disposed = False
        self._task: asyncio.Task[None] | None = None
        self._scopes: set[EffectScope] = set()

    @property
    def disposed(self) -> bool:
        """Whether cleanup has completed successfully.

        Returns:
            ``True`` after the cleanup callback returns successfully.
        """
        return self._disposed

    async def dispose(self) -> None:
        """Run cleanup once, coalescing concurrent callers and preserving failures.

        Raises:
            BaseException: The cleanup failure from the current attempt; a failed
                handle remains retryable.
        """
        if self._disposed:
            return

        task = self._task
        if task is None:
            task = asyncio.create_task(self._run_teardown())
            # A cancelled caller does not retrieve a later teardown failure. Mark
            # it observed while preserving normal propagation to every waiter.
            task.add_done_callback(_observe_task_failure)
            self._task = task

        # Disposal is a resource-safety boundary: cancelling one waiter must not
        # cancel the shared cleanup or affect other concurrent waiters.
        await asyncio.shield(task)

    async def _run_teardown(self) -> None:
        try:
            fn = self._fn
            if fn is not None:
                result = fn()
                if inspect.isawaitable(result):
                    await result
        except BaseException:
            # A failed cleanup remains retryable. Existing waiters still hold and
            # await this task, so they all observe the same attempt's failure.
            self._task = None
            raise
        else:
            self._disposed = True
            self._fn = None  # let captured state be collected after success
            self._task = None
            scopes = tuple(self._scopes)
            self._scopes.clear()
            for scope in scopes:
                scope._discard(self)

    def __call__(self) -> Awaitable[None]:
        """Return the awaitable for :meth:`dispose`.

        Returns:
            Awaitable that completes when cleanup succeeds or raises its failure.
        """
        return self.dispose()


def _coerce(item: Disposable | DisposerFn) -> Disposable:
    if isinstance(item, Disposable):
        return item
    if not callable(item):
        raise TypeError(f"disposer must be callable, got {type(item).__name__}")
    return Disposable(item)


class EffectScope:
    """An ordered collection of :class:`Disposable`s unwound in reverse.

    ``effect(fn)`` runs *fn* immediately; whatever teardown *fn* returns (a
    disposer, or an iterable of disposers, or ``None``) is collected and returned
    as a single composite :class:`Disposable`. Disposing the scope runs every
    collected disposer in reverse registration order, awaiting async ones.
    """

    __slots__ = ("_closed", "_disposers", "_teardown")

    def __init__(self) -> None:
        self._disposers: list[Disposable] = []
        self._closed = False
        self._teardown = Disposable(self._dispose_all)

    @property
    def disposed(self) -> bool:
        """Whether every owned disposer has completed successfully."""
        return self._teardown.disposed

    @property
    def closed(self) -> bool:
        """Whether teardown has begun and new effects are rejected."""
        return self._closed

    def ensure_active(self) -> None:
        """Raise when this scope can no longer accept registrations."""
        if self._closed:
            raise RuntimeError("effect scope is disposing or disposed")

    def add(self, disposer: Disposable | DisposerFn) -> Disposable:
        """Track an already-created disposer so the scope owns its teardown."""
        self.ensure_active()
        d = _coerce(disposer)
        self._track(d)
        return d

    def _track(self, disposer: Disposable) -> None:
        if not disposer.disposed:
            self._disposers.append(disposer)
            disposer._scopes.add(self)

    def _discard(self, disposer: Disposable) -> None:
        self._disposers[:] = [item for item in self._disposers if item is not disposer]

    def effect(self, fn: Callable[[], Any]) -> Disposable:
        """Run *fn* now and register the teardown it yields.

        *fn* may return ``None``, a single disposer (a :class:`Disposable` or a
        zero-arg callable), or an iterable of disposers. The returned composite
        disposes only this effect's group, in reverse, and is idempotent.
        """
        self.ensure_active()
        result = fn()
        group: list[Disposable] = []
        try:
            if result is None:
                pass
            elif isinstance(result, Disposable) or callable(result):
                group.append(_coerce(result))
            elif isinstance(result, Iterable):
                for item in result:
                    if item is not None:
                        group.append(_coerce(item))
            else:
                raise TypeError(
                    "effect fn must return None, a disposer, or an iterable of disposers, "
                    f"got {type(result).__name__}"
                )
        finally:
            # fn() already ran, so anything coerced before a rejected item is a
            # live registration. The scope must still own it or it can never be
            # torn down.
            for disposer in group:
                self._track(disposer)
        return Disposable(lambda: _dispose_reverse(group))

    async def dispose(self) -> None:
        # There is no await between quiescing and starting the shared teardown,
        # so synchronous add/effect calls cannot slip into a discarded snapshot.
        self._closed = True
        await self._teardown.dispose()

    async def _dispose_all(self) -> None:
        # Snapshot because successful entries remove themselves while teardown
        # runs; failed entries remain tracked for the next dispose() call.
        await _dispose_reverse(list(self._disposers))
        self._disposers.clear()


async def _dispose_reverse(disposers: list[Disposable]) -> None:
    errors: list[BaseException] = []
    for d in reversed(disposers):
        try:
            await d.dispose()
        except BaseException as exc:
            errors.append(exc)

    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise CleanupError(errors)


class CleanupError(RuntimeError):
    """Multiple teardown failures collected after every cleanup was attempted."""

    def __init__(self, errors: Iterable[BaseException]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"{len(self.errors)} cleanup operations failed")


def _observe_task_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
