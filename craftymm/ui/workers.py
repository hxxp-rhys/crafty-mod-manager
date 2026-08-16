"""Run blocking work off the GUI thread.

Usage::

    task = Task(manager.scan)
    task.signals.progress.connect(...)
    task.signals.done.connect(...)
    task.signals.failed.connect(...)
    POOL.start(task)

If the callable accepts a ``progress`` keyword it is given one that emits the
``progress`` signal, so long jobs can drive the status bar.
"""
from __future__ import annotations

import inspect
import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

log = logging.getLogger(__name__)


class TaskSignals(QObject):
    progress = Signal(str, int, int)  # message, current, total
    done = Signal(object)
    failed = Signal(str, str)  # short message, traceback
    finished = Signal()


class Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = TaskSignals()
        # Qt would delete the C++ QRunnable - and with it the TaskSignals object
        # that owns the queued connections - the moment run() returns, which is
        # usually *before* the GUI thread has dispatched done/finished. Keep the
        # object alive ourselves and drop it in run_task() once finished lands.
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:  # pragma: no cover - thread body
        try:
            try:
                params = inspect.signature(self.fn).parameters
            except (TypeError, ValueError):
                params = {}
            if "progress" in params and "progress" not in self.kwargs:
                self.kwargs["progress"] = self._emit_progress
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            log.exception("background task failed")
            self._emit(
                "failed",
                str(exc) or exc.__class__.__name__,
                traceback.format_exc(),
            )
        else:
            self._emit("done", result)
        finally:
            self._emit("finished")

    def _emit(self, name: str, *args) -> None:
        """Emit a signal, tolerating a window that closed mid-task (Qt deletes
        the C++ side and further emits would raise)."""
        try:
            getattr(self.signals, name).emit(*args)
        except RuntimeError:
            log.debug("dropped '%s' signal - receiver already gone", name)

    def _emit_progress(self, message: str, current: int = 0, total: int = 0) -> None:
        self._emit("progress", str(message), int(current), int(total))


POOL = QThreadPool.globalInstance()
POOL.setMaxThreadCount(max(4, POOL.maxThreadCount()))

# Strong references to in-flight tasks, so neither the QRunnable nor its
# TaskSignals is garbage collected before the GUI thread runs the callbacks.
_ACTIVE: set[Task] = set()


def run_task(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    on_finished: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Task:
    task = Task(fn, *args, **kwargs)
    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    if on_progress:
        task.signals.progress.connect(on_progress)
    if on_finished:
        task.signals.finished.connect(on_finished)
    # Connected last so the caller's handlers have already run by the time the
    # task is released.
    task.signals.finished.connect(lambda: _ACTIVE.discard(task))
    _ACTIVE.add(task)
    POOL.start(task)
    return task


def active_count() -> int:
    """How many tasks are still in flight (used by tests and shutdown)."""
    return len(_ACTIVE)


def defer(fn: Callable[..., Any], *args: Any) -> None:
    """Run ``fn`` on the next event-loop turn.

    ``done`` is delivered before ``finished``, so anything chained off a task's
    result would still see the busy flag set and be skipped by re-entrancy
    guards. Deferring puts the call after the already-queued ``finished``.
    """
    QTimer.singleShot(0, lambda: fn(*args))
