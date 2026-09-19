"""The VPN shuffle engine.

Four selection algorithms, all independent from Tor's rotation:

``Random``
    Pick uniformly at random, never returning the profile that is already
    active (unless the pool holds a single profile).

``Sequential``
    Walk the pool in its declared order, starting from the position of the
    active profile, and wrap around at the end.

``Round Robin``
    Keep an internal queue so every profile in the pool is used exactly once
    per cycle regardless of which profile happens to be active. Fairer than
    ``Sequential`` when the active profile changes for other reasons.

``No Repeat``
    Shuffle the pool into a random permutation and consume it; when the
    permutation is exhausted, reshuffle. Random-looking, but with a guarantee
    that no profile repeats until every other one has been used.
"""

from __future__ import annotations

import logging
import random
from collections import deque
from dataclasses import dataclass, field

from nextron.core.config import ShuffleAlgorithm
from nextron.vpn.profiles import VPNProfile

log = logging.getLogger(__name__)

__all__ = ["ShuffleEngine", "ShuffleState"]


@dataclass(slots=True)
class ShuffleState:
    """Bookkeeping for the current shuffle cycle."""

    algorithm: ShuffleAlgorithm = ShuffleAlgorithm.RANDOM
    cycle: int = 0
    selections: int = 0
    history: list[str] = field(default_factory=list)

    @property
    def last(self) -> str | None:
        return self.history[-1] if self.history else None


class ShuffleEngine:
    """Choose the next VPN profile according to the configured algorithm."""

    #: How much history to retain for the Logs screen.
    HISTORY_LIMIT = 100

    def __init__(
        self,
        algorithm: ShuffleAlgorithm = ShuffleAlgorithm.RANDOM,
        profiles: list[VPNProfile] | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._algorithm = algorithm
        self._profiles: list[VPNProfile] = list(profiles or [])
        self._rng = rng or random.Random()
        self._queue: deque[str] = deque()
        self._state = ShuffleState(algorithm=algorithm)
        self._reseed()

    # -- properties --------------------------------------------------------- #

    @property
    def algorithm(self) -> ShuffleAlgorithm:
        return self._algorithm

    @property
    def state(self) -> ShuffleState:
        return self._state

    @property
    def pool(self) -> tuple[VPNProfile, ...]:
        return tuple(self._profiles)

    @property
    def pool_size(self) -> int:
        return len(self._profiles)

    @property
    def remaining_in_cycle(self) -> int:
        return len(self._queue)

    # -- configuration ------------------------------------------------------ #

    def set_algorithm(self, algorithm: ShuffleAlgorithm) -> None:
        if algorithm is self._algorithm:
            return
        self._algorithm = algorithm
        self._state.algorithm = algorithm
        self._reseed()
        log.debug("Shuffle algorithm set to %s", algorithm.label)

    def set_pool(self, profiles: list[VPNProfile]) -> None:
        """Replace the rotation pool, preserving cycle position where possible."""
        self._profiles = list(profiles)
        surviving = {profile.id for profile in self._profiles}
        self._queue = deque(pid for pid in self._queue if pid in surviving)
        if not self._queue:
            self._reseed()

    # -- selection ---------------------------------------------------------- #

    def next(self, current_id: str | None = None) -> VPNProfile | None:
        """Return the next profile to connect to, or ``None`` for an empty pool."""
        if not self._profiles:
            return None
        if len(self._profiles) == 1:
            chosen = self._profiles[0]
            self._record(chosen)
            return chosen

        if self._algorithm is ShuffleAlgorithm.RANDOM:
            chosen = self._pick_random(current_id)
        elif self._algorithm is ShuffleAlgorithm.SEQUENTIAL:
            chosen = self._pick_sequential(current_id)
        elif self._algorithm is ShuffleAlgorithm.ROUND_ROBIN:
            chosen = self._pick_from_queue(current_id, shuffle=False)
        else:  # NO_REPEAT
            chosen = self._pick_from_queue(current_id, shuffle=True)

        self._record(chosen)
        return chosen

    def peek(self, current_id: str | None = None) -> VPNProfile | None:
        """Preview the next selection without consuming it."""
        snapshot_queue = deque(self._queue)
        snapshot_state = ShuffleState(
            algorithm=self._state.algorithm,
            cycle=self._state.cycle,
            selections=self._state.selections,
            history=list(self._state.history),
        )
        try:
            return self.next(current_id)
        finally:
            self._queue = snapshot_queue
            self._state = snapshot_state

    # -- algorithms --------------------------------------------------------- #

    def _pick_random(self, current_id: str | None) -> VPNProfile:
        candidates = [p for p in self._profiles if p.id != current_id] or self._profiles
        return self._rng.choice(candidates)

    def _pick_sequential(self, current_id: str | None) -> VPNProfile:
        ids = [profile.id for profile in self._profiles]
        index = (ids.index(current_id) + 1) % len(ids) if current_id in ids else 0
        return self._profiles[index]

    def _pick_from_queue(self, current_id: str | None, *, shuffle: bool) -> VPNProfile:
        if not self._queue:
            self._reseed(shuffle=shuffle)
        # Avoid handing back the profile that is already connected.
        if len(self._queue) > 1 and self._queue[0] == current_id:
            self._queue.rotate(-1)
        profile_id = self._queue.popleft()
        if not self._queue:
            self._state.cycle += 1
            self._reseed(shuffle=shuffle)
        return self._by_id(profile_id) or self._profiles[0]

    def _reseed(self, *, shuffle: bool | None = None) -> None:
        """Refill the internal cycle queue."""
        ids = [profile.id for profile in self._profiles]
        if shuffle is None:
            shuffle = self._algorithm is ShuffleAlgorithm.NO_REPEAT
        if shuffle:
            self._rng.shuffle(ids)
        self._queue = deque(ids)

    # -- helpers ------------------------------------------------------------ #

    def _by_id(self, profile_id: str) -> VPNProfile | None:
        return next((p for p in self._profiles if p.id == profile_id), None)

    def _record(self, profile: VPNProfile) -> None:
        self._state.selections += 1
        self._state.history.append(profile.id)
        if len(self._state.history) > self.HISTORY_LIMIT:
            del self._state.history[0]
