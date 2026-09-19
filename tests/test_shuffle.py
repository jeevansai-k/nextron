"""The four VPN shuffle algorithms."""

from __future__ import annotations

import itertools
import random
from datetime import datetime

import pytest

from nextron.core.config import ShuffleAlgorithm
from nextron.vpn.profiles import VPNProfile, VPNProtocol
from nextron.vpn.shuffle import ShuffleEngine


def _pool(count: int = 4) -> list[VPNProfile]:
    return [
        VPNProfile(
            id=f"id{index}",
            name=name,
            protocol=VPNProtocol.OPENVPN,
            filename=f"{name}.ovpn",
            added_at=datetime.now(),
        )
        for index, name in enumerate(["DE", "NL", "FR", "JP"][:count])
    ]


def _run(algorithm: ShuffleAlgorithm, steps: int = 12, seed: int = 7) -> list[str]:
    pool = _pool()
    engine = ShuffleEngine(algorithm, pool, rng=random.Random(seed))
    names = {profile.id: profile.name for profile in pool}
    current: str | None = None
    picks: list[str] = []
    for _ in range(steps):
        chosen = engine.next(current)
        assert chosen is not None
        current = chosen.id
        picks.append(names[chosen.id])
    return picks


@pytest.mark.parametrize("algorithm", list(ShuffleAlgorithm))
def test_never_returns_the_active_profile(algorithm):
    picks = _run(algorithm)
    assert all(a != b for a, b in itertools.pairwise(picks))


def test_sequential_follows_declared_order():
    assert _run(ShuffleAlgorithm.SEQUENTIAL, steps=6) == [
        "DE", "NL", "FR", "JP", "DE", "NL",
    ]


def test_round_robin_uses_every_profile_once_per_cycle():
    picks = _run(ShuffleAlgorithm.ROUND_ROBIN, steps=8)
    assert set(picks[:4]) == {"DE", "NL", "FR", "JP"}
    assert set(picks[4:]) == {"DE", "NL", "FR", "JP"}


def test_no_repeat_exhausts_the_pool_before_repeating():
    picks = _run(ShuffleAlgorithm.NO_REPEAT, steps=8)
    assert set(picks[:4]) == {"DE", "NL", "FR", "JP"}
    assert set(picks[4:]) == {"DE", "NL", "FR", "JP"}


def test_random_uses_the_whole_pool():
    picks = _run(ShuffleAlgorithm.RANDOM, steps=60)
    assert set(picks) == {"DE", "NL", "FR", "JP"}


def test_single_profile_pool_returns_that_profile():
    pool = _pool(1)
    engine = ShuffleEngine(ShuffleAlgorithm.NO_REPEAT, pool)
    assert engine.next(pool[0].id).id == pool[0].id


def test_empty_pool_returns_none():
    assert ShuffleEngine(ShuffleAlgorithm.RANDOM, []).next() is None


def test_peek_does_not_consume():
    pool = _pool()
    engine = ShuffleEngine(ShuffleAlgorithm.SEQUENTIAL, pool)
    preview = engine.peek()
    assert preview is not None
    assert engine.next().id == preview.id
    assert engine.state.selections == 1


def test_pool_can_shrink_without_breaking_the_cycle():
    pool = _pool()
    engine = ShuffleEngine(ShuffleAlgorithm.ROUND_ROBIN, pool)
    engine.next()
    engine.set_pool(pool[:2])
    assert engine.pool_size == 2
    assert engine.next().id in {"id0", "id1"}


def test_switching_algorithm_reseeds():
    engine = ShuffleEngine(ShuffleAlgorithm.SEQUENTIAL, _pool())
    engine.next()
    engine.set_algorithm(ShuffleAlgorithm.NO_REPEAT)
    assert engine.algorithm is ShuffleAlgorithm.NO_REPEAT
    assert engine.remaining_in_cycle == 4
