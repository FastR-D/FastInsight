"""Cooperative work cancellation and admission of irreversible effects."""
import threading
from contextlib import contextmanager


class Cancelled(RuntimeError):
    pass


class Operation:
    def __init__(self):
        self.stop = threading.Event()
        self._effects = threading.Lock()

    def checkpoint(self):
        if self.stop.is_set():
            raise Cancelled("Operation cancelled")

    @contextmanager
    def effect(self):
        with self._effects:
            self.checkpoint()
            yield

    def cancel(self):
        self.stop.set()
        # Drain effects already admitted before acknowledging cancellation.
        with self._effects:
            pass
