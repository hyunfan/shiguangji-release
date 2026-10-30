"""Shared stop/budget check for the archive and public-source HTTP clients."""
from shiguangji.core.core_errors import Cancelled


class CancellableClient:
    """Mixin for clients that own ``stop`` (a threading.Event) and an optional ``budget``."""

    def check(self):
        if self.stop.is_set():
            raise Cancelled()
        if self.budget:
            self.budget.check()
