"""Internal provenance for explicitly requested synthetic demo commands."""

from contextlib import contextmanager
from contextvars import ContextVar

demo_seed = ContextVar("demo_seed", default=False)


@contextmanager
def demo_activity():
    token = demo_seed.set(True)
    try:
        yield
    finally:
        demo_seed.reset(token)
