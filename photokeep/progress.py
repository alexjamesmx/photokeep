"""Progress lines for long runs, at most every few seconds."""
import time
from collections.abc import Callable, Iterable, Iterator

Say = Callable[[str], None]
_EVERY = 5.0  # seconds between progress lines


def ticking(it: Iterable, total: int, say: Say, what: str) -> Iterator:
    """Yield from it, telling `say` how far it got at most every few seconds: a whole library takes minutes."""
    last = time.monotonic()
    for n, x in enumerate(it, 1):
        yield x
        if n == total or time.monotonic() - last >= _EVERY:
            say(f"{what}: {n}/{total}")
            last = time.monotonic()
