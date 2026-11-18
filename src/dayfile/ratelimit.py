"""In-memory limiters: fixed-window request budgets and caps on simultaneously held requests."""

from collections.abc import Callable
from itertools import count


class Window:
    """Fixed-window budgets per key; a charge that does not fit is refused and spends nothing."""

    def __init__(self, monotonic: Callable[[], float], prune_at: int = 10_000) -> None:
        self._now = monotonic
        self._prune_at = prune_at
        self._windows: dict[object, tuple[float, int, int]] = {}  # key -> (start, used, span)

    def __len__(self) -> int:
        return len(self._windows)

    def charge(self, key: object, amount: int, limit: int, span: int) -> int:
        """Spend `amount` from the key's window; return 0, or the seconds until it may retry."""
        now = self._now()
        start, used, _ = self._windows.get(key, (now, 0, span))
        if now - start >= span:
            start, used = now, 0
        if used + amount > limit:
            return max(1, int(start + span - now) + 1)
        self._windows[key] = (start, used + amount, span)
        if len(self._windows) > self._prune_at:
            self._prune(now)
        return 0

    def _prune(self, now: float) -> None:
        stale = [k for k, (start, _, span) in self._windows.items() if now - start >= span]
        for key in stale:
            del self._windows[key]


Lease = tuple[object, int]


class Held:
    """Caps how many requests a key may hold open at once (streams, long-polls).

    Every lease carries a deadline, so one that is never released (a client that vanished before
    its response began) frees itself instead of using up the cap for good.
    """

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._now = monotonic
        self._ids = count(1)
        self._leases: dict[object, dict[int, float]] = {}  # key -> {lease id: deadline}

    def acquire(self, key: object, limit: int, ttl: float) -> Lease | None:
        """Take a slot for up to `ttl` seconds, or return None if the key is at its cap."""
        now = self._now()
        live = {i: d for i, d in self._leases.get(key, {}).items() if d > now}
        if len(live) >= limit:
            self._leases[key] = live
            return None
        lease_id = next(self._ids)
        live[lease_id] = now + ttl
        self._leases[key] = live
        return key, lease_id

    def release(self, lease: Lease | None) -> None:
        """Give a slot back; releasing twice, or None, does nothing."""
        if lease is None:
            return
        key, lease_id = lease
        slots = self._leases.get(key)
        if slots is not None:
            slots.pop(lease_id, None)
            if not slots:
                del self._leases[key]
