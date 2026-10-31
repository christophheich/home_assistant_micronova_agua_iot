"""Unit tests for Chrono schedule programs and time entities."""

from __future__ import annotations

import asyncio
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers import build_device_from_fixture, load_component_module

local_mqtt_module = load_component_module("local_mqtt")
LocalMqttAguaIOT = local_mqtt_module.LocalMqttAguaIOT


def make_device_with_chrono():
    register_map = {
        "chrono_p1_enable_set": {
            "reg_key": "chrono_p1_enable_set",
            "offset": 32825,
            "mask": 1,
            "formula": "#",
            "formula_inverse": "#",
            "set_min": 0,
            "set_max": 1,
            "reg_type": "SET",
            "value_raw": "0",
            "value": 0,
        },
        "chrono_p1_start_set": {
            "reg_key": "chrono_p1_start_set",
            "offset": 32826,
            "mask": 255,
            "formula": "#",
            "formula_inverse": "#",
            "set_min": 0,
            "set_max": 144,
            "reg_type": "SET",
            "value_raw": "12",
            "value": 12,
        },
        "chrono_p1_stop_set": {
            "reg_key": "chrono_p1_stop_set",
            "offset": 32827,
            "mask": 255,
            "formula": "#",
            "formula_inverse": "#",
            "set_min": 0,
            "set_max": 144,
            "reg_type": "SET",
            "value_raw": "18",
            "value": 18,
        },
        "chrono_p1_day_monday_set": {
            "reg_key": "chrono_p1_day_monday_set",
            "offset": 32828,
            "mask": 1,
            "formula": "#",
            "formula_inverse": "#",
            "set_min": 0,
            "set_max": 1,
            "reg_type": "SET",
            "value_raw": "1",
            "value": 1,
        },
        "chrono_p1_day_tuesday_set": {
            "reg_key": "chrono_p1_day_tuesday_set",
            "offset": 32829,
            "mask": 1,
            "formula": "#",
            "formula_inverse": "#",
            "set_min": 0,
            "set_max": 1,
            "reg_type": "SET",
            "value_raw": "0",
            "value": 0,
        },
    }
    # Add dummy P2..P6 to verify chunking
    for p in range(2, 7):
        base_off = 32825 + (p - 1) * 10
        for idx, key in enumerate(["enable", "start", "stop", "day_monday", "day_tuesday"]):
            reg_key = f"chrono_p{p}_{key}_set"
            register_map[reg_key] = {
                "reg_key": reg_key,
                "offset": base_off + idx,
                "mask": 255,
                "formula": "#",
                "formula_inverse": "#",
                "set_min": 0,
                "set_max": 144,
                "reg_type": "SET",
                "value_raw": "0",
                "value": 0,
            }
    mock_aguaiot = MagicMock()
    return build_device_from_fixture(mock_aguaiot, "stove_test", register_map)


@pytest.mark.asyncio
async def test_fetch_chrono_registers_chunking():
    """Verify that fetch_chrono_registers queries in chunks of 20 registers."""
    instance = LocalMqttAguaIOT(
        hass=MagicMock(),
        api_url="https://remote.mcz.it",
        customer_code="746318",
        email="test@example.com",
        password="test",
        unique_id="TEST-UUID",
        buffer_read_timeout=1,
    )
    writer_mock = MagicMock()
    writer_mock.is_closing.return_value = False
    instance._client_writer = writer_mock
    instance._device_mac = "349454591D20"

    device = make_device_with_chrono()
    instance.devices = [device]

    published_queries = []

    async def fake_publish(topic, payload):
        published_queries.append(payload)
        # Simulate stove response for the requested items
        items = payload["pl"]["Items"]
        for item in items:
            instance._register_values[item] = 42
        instance._response_event.set()

    instance._send_mqtt_publish = fake_publish

    res = await instance.fetch_chrono_registers(device)

    # 30 chrono offsets split into chunks of 20 -> 2 query packets
    assert len(published_queries) == 2
    assert len(published_queries[0]["pl"]["Items"]) == 20
    assert len(published_queries[1]["pl"]["Items"]) == 10
    assert res[32825] == 42


@pytest.mark.asyncio
async def test_atomic_schedule_write_uses_single_eeprom_slot():
    """Verify that updating a complete schedule program counts as a single EEPROM write."""
    instance = LocalMqttAguaIOT(
        hass=MagicMock(),
        api_url="https://remote.mcz.it",
        customer_code="746318",
        email="test@example.com",
        password="test",
        unique_id="TEST-UUID",
        allow_eeprom_writes=True,
        max_eeprom_writes_per_hour=15,
        max_eeprom_writes_per_day=50,
    )
    writer_mock = MagicMock()
    writer_mock.is_closing.return_value = False
    instance._client_writer = writer_mock
    instance._device_mac = "349454591D20"

    device = make_device_with_chrono()
    device._Device__aguaiot = instance

    sent_payloads = []

    async def fake_publish(topic, payload):
        sent_payloads.append(payload)

    instance._send_mqtt_publish = fake_publish
    instance._fetch_device_information = AsyncMock(return_value={})

    # Writing 4 schedule registers together
    await device.set_register_values(
        {
            "chrono_p1_enable_set": 1,
            "chrono_p1_start_set": 15,
            "chrono_p1_stop_set": 25,
            "chrono_p1_day_monday_set": 1,
        },
        limit_value_raw=True,
    )

    # Exactly 1 MQTT write request sent
    assert len(sent_payloads) == 1
    req = sent_payloads[0]["pl"]
    assert req["Cmd"] == "RequestWriting"
    assert len(req["Items"]) == 4
    # Rate limiter recorded only 1 timestamp
    assert len(instance._eeprom_write_timestamps) == 1


def test_time_conversion_resolution():
    """Verify conversion between 10-minute units and standard datetime.time."""
    def units_to_time(val):
        if val is None:
            return None
        total_minutes = int(val) * 10
        if total_minutes >= 1440:
            return time(23, 59)
        return time(total_minutes // 60, total_minutes % 60)

    def time_to_units(t):
        return min(144, max(0, round((t.hour * 60 + t.minute) / 10)))

    # 02:00 -> 120 min / 10 = 12
    assert units_to_time(12) == time(2, 0)
    assert time_to_units(time(2, 0)) == 12

    # 03:00 -> 180 min / 10 = 18
    assert units_to_time(18) == time(3, 0)
    assert time_to_units(time(3, 0)) == 18

    # 06:30 -> 390 min / 10 = 39
    assert units_to_time(39) == time(6, 30)
    assert time_to_units(time(6, 30)) == 39

    # 24:00 (144) -> 23:59
    assert units_to_time(144) == time(23, 59)
    assert time_to_units(time(23, 59)) == 144
