"""EEPROM budgets survive restarts and fail closed on storage errors."""

import json
from types import SimpleNamespace

import pytest

from tests.helpers import load_component_module

eeprom = load_component_module("eeprom")


@pytest.fixture
def budget_env(monkeypatch, tmp_path):
    class FakeStore:
        def __init__(self, hass, version, key, **kwargs):
            self.path = str(tmp_path / key)

        async def async_load(self):
            try:
                with open(self.path) as source:  # noqa: ASYNC230 - tiny local test fixture
                    return json.load(source)["data"]
            except FileNotFoundError:
                return None

        async def async_save(self, data):
            with open(self.path, "w") as target:  # noqa: ASYNC230 - tiny local test fixture
                json.dump({"data": data}, target)

    async def execute(fn, *args):
        return fn(*args)

    monkeypatch.setattr(eeprom, "Store", FakeStore)
    now = [100000.0]
    # + 0.0 copies the value into a new float, as time.time() does.
    monkeypatch.setattr(eeprom.time, "time", lambda: now[0] + 0.0)
    hass = SimpleNamespace(async_add_executor_job=execute)
    return hass, now


@pytest.mark.asyncio
async def test_hourly_limit_restart_and_stove_isolation(budget_env):
    hass, now = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    for _ in range(eeprom.HOURLY_LIMIT):
        await guard.reserve()
    restarted = eeprom.EepromBudget(hass, "one")
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await restarted.reserve()
    await eeprom.EepromBudget(hass, "two").reserve()
    now[0] += 3600
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await restarted.reserve()
    now[0] += 86400
    await restarted.reserve()


@pytest.mark.asyncio
async def test_daily_limit_and_clock_rollback(budget_env):
    hass, now = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    await guard.reserve()
    now[0] += 3600
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await guard.reserve()
    now[0] -= 20000
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await guard.reserve()
    now[0] += 110000
    await guard.reserve()


@pytest.mark.asyncio
async def test_silent_storage_failure_blocks_reservation(budget_env, monkeypatch):
    hass, _ = budget_env
    guard = eeprom.EepromBudget(hass, "one")

    async def discard(_data):
        pass

    monkeypatch.setattr(guard.store, "async_save", discard)
    with pytest.raises(eeprom.AguaIOTError, match="No command was sent"):
        await guard.reserve()
    assert len(guard.history) == 1


@pytest.mark.asyncio
async def test_ram_write_ignores_the_block_and_the_budget(budget_env):
    hass, _ = budget_env
    hass.data = {}
    await eeprom.reserve_eeprom_write(hass, "DEV_01", [33], allowed=False)
    assert hass.data == {}


@pytest.mark.asyncio
async def test_cloud_and_mqtt_share_one_stove_budget(budget_env):
    hass, _ = budget_env
    hass.data = {}
    for _ in range(eeprom.DAILY_LIMIT):
        await eeprom.reserve_eeprom_write(hass, "DEV_01", [32768])
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await eeprom.reserve_eeprom_write(
            hass, "DEV_01", [32768], legacy_id="AABBCCDDEEFF"
        )


@pytest.mark.asyncio
async def test_old_mqtt_history_still_counts(budget_env):
    hass, now = budget_env
    hass.data = {}
    old = eeprom.Store(hass, 1, "aguaiot.mqtt.AABBCCDDEEFF.eeprom")
    await old.async_save([now[0]] * 15)
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await eeprom.reserve_eeprom_write(
            hass, "DEV_01", [32768], legacy_id="AABBCCDDEEFF"
        )


@pytest.mark.asyncio
async def test_release_returns_one_slot_at_the_hourly_cap(budget_env, monkeypatch):
    monkeypatch.setattr(eeprom, "HOURLY_LIMIT", 2)
    monkeypatch.setattr(eeprom, "DAILY_LIMIT", 2)
    hass, _ = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    stamps = [await guard.reserve() for _ in range(2)]
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await guard.reserve()
    await guard.release(stamps[0])
    await guard.reserve()
    await guard.release(stamps[0])
    await guard.release(None)
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await guard.reserve()
    assert any(value is stamps[1] for value in guard.history)
    assert all(value is not stamps[0] for value in guard.history)
    assert len(guard.history) == 2


@pytest.mark.asyncio
async def test_failed_release_keeps_the_reservation(budget_env, monkeypatch):
    hass, _ = budget_env
    hass.data = {}
    device = SimpleNamespace(id_device="DEV_01", product_serial="AABBCCDDEEFF")
    stamp = await eeprom.reserve_device_write(hass, device, [32768], {})
    budget = hass.data["aguaiot"]["eeprom_budgets"]["DEV_01"]

    async def boom(_data):
        raise OSError("disk")

    monkeypatch.setattr(budget.store, "async_save", boom)
    with pytest.raises(OSError):
        await budget.release(stamp)
    assert any(value is stamp for value in budget.history)
    await eeprom.release_device_write(hass, device, stamp)
    assert any(value is stamp for value in budget.history)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["bad", [True], [float("nan")], [-1]])
async def test_invalid_history_blocks_writes(budget_env, data):
    hass, _ = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    await guard.store.async_save(data)
    with pytest.raises(eeprom.AguaIOTError, match="No command was sent"):
        await guard.reserve()
