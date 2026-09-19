"""The local statistics database."""

from __future__ import annotations

import pytest

from nextron.core.exceptions import StorageError
from nextron.storage import paths
from nextron.storage.database import Database


@pytest.fixture
async def database():
    instance = Database()
    await instance.connect()
    await instance.start_session("tor_only", "1.0.0")
    yield instance
    await instance.end_session()
    await instance.close()


async def test_database_is_created_under_the_config_root(database):
    assert paths.database_file().is_file()
    assert database.session_id == 1


async def test_rotations_and_ips_are_recorded(database):
    await database.record_rotation(
        "tor", detail="circuit-7", exit_ip="185.1.2.3", exit_country="NL"
    )
    await database.record_rotation("vpn", detail="berlin")
    await database.record_ip("tor", "185.1.2.3", "NL")

    stats = await database.stats()
    assert stats.tor_rotations == 1
    assert stats.vpn_switches == 1
    assert stats.unique_exit_ips == 1
    assert stats.countries == ("NL",)


async def test_dns_counters_accumulate_per_day(database):
    await database.bump_dns_counters(queries=100, blocked=25)
    await database.bump_dns_counters(queries=40, blocked=15)
    stats = await database.stats()
    assert stats.dns_queries == 140
    assert stats.dns_blocked == 40
    assert stats.dns_block_rate == pytest.approx(28.57, abs=0.01)


async def test_verifications_are_recorded_both_ways(database):
    await database.record_verification("tor_only", True, [{"name": "bootstrap"}])
    await database.record_verification("vpn_only", False, [{"name": "tunnel"}])
    stats = await database.stats()
    assert stats.verifications_passed == 1
    assert stats.verifications_failed == 1


async def test_recent_rotations_are_newest_first(database):
    await database.record_rotation("tor", detail="first")
    await database.record_rotation("tor", detail="second")
    rows = await database.recent_rotations(limit=2)
    assert [row["detail"] for row in rows] == ["second", "first"]


async def test_events_round_trip(database):
    await database.record_event("route.up", "Tor Only established", payload={"x": 1})
    rows = await database.recent_events(limit=1)
    assert rows[0]["event_type"] == "route.up"
    assert rows[0]["message"] == "Tor Only established"


async def test_operations_before_connect_are_rejected():
    with pytest.raises(StorageError):
        await Database().stats()


async def test_close_is_idempotent(database):
    await database.close()
    await database.close()
    assert not database.connected


async def test_purge_keeps_the_newest_sessions(database):
    for _ in range(4):
        await database.start_session("vpn_only", "1.0.0")
    removed = await database.purge(keep_sessions=2)
    assert removed == 3
    stats = await database.stats()
    assert stats.sessions == 2
