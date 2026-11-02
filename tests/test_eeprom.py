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
    monkeypatch.setattr(eeprom.time, "time", lambda: now[0])
    hass = SimpleNamespace(async_add_executor_job=execute)
    return hass, now


@pytest.mark.asyncio
async def test_hourly_limit_restart_and_stove_isolation(budget_env):
    hass, now = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    for _ in range(15):
        await guard.reserve()
    restarted = eeprom.EepromBudget(hass, "one")
    with pytest.raises(eeprom.AguaIOTError, match="limit reached"):
        await restarted.reserve()
    await eeprom.EepromBudget(hass, "two").reserve()
    now[0] += 3600
    await restarted.reserve()


@pytest.mark.asyncio
async def test_daily_limit_and_clock_rollback(budget_env):
    hass, now = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    for _ in range(5):
        for _ in range(10):
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
@pytest.mark.parametrize("data", ["bad", [True], [float("nan")], [-1]])
async def test_invalid_history_blocks_writes(budget_env, data):
    hass, _ = budget_env
    guard = eeprom.EepromBudget(hass, "one")
    await guard.store.async_save(data)
    with pytest.raises(eeprom.AguaIOTError, match="No command was sent"):
        await guard.reserve()
