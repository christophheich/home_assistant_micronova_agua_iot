"""Unit tests for the LocalMqttAguaIOT transport."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.helpers import load_component_module

local_mqtt_module = load_component_module("local_mqtt")
LocalMqttAguaIOT = local_mqtt_module.LocalMqttAguaIOT


def make_dummy_cached_devices():
    return [
        {
            "id": 1,
            "id_device": "DEV_01",
            "id_product": 10,
            "product_serial": "A1B2C3D4E5F6",
            "name": "Living Room Stove",
            "is_online": True,
            "name_product": "Pellet Stove",
            "id_registers_map": 5,
            "device_info": {"reg_code": "98765432"},
            "register_map": {
                "temp_air_get": {
                    "offset": 1,
                    "mask": 65535,
                    "formula": "# / 10",
                    "reg_type": "GET",
                },
                "temp_smoke_get": {
                    "offset": 6,
                    "mask": 65535,
                    "formula": "#",
                    "reg_type": "GET",
                },
                "status_managed_set": {
                    "offset": 33,
                    "mask": 65535,
                    "formula": "#",
                    "reg_type": "SET",
                },
                "temp_air_set": {
                    "offset": 32893,
                    "mask": 65535,
                    "formula": "#",
                    "reg_type": "SET",
                    "value_raw": "22",
                },
                "chrono_p1_start_set": {
                    "offset": 32826,
                    "mask": 65535,
                    "formula": "#",
                    "reg_type": "SET",
                },
            },
        }
    ]


def make_test_instance(
    cached_devices=None,
):
    """Create a LocalMqttAguaIOT test instance with dummy credentials."""
    hass = MagicMock()
    return LocalMqttAguaIOT(
        hass=hass,
        api_url="https://example.com",
        customer_code="123456",
        email="test@example.com",
        password="secret_password",
        unique_id="uuid-test-1234",
        cached_devices=cached_devices,
    )


class TestLocalMqttBasics:
    """Test instance creation, cache helpers, and identifier sanitization."""

    def test_clean_identifier(self):
        client = make_test_instance()
        assert client._clean_identifier("AA:BB:CC:11:22:33") == "AABBCC112233"
        assert client._clean_identifier("aa-bb-cc-dd-ee-ff") == "AABBCCDDEEFF"
        assert client._clean_identifier(None) is None
        assert client._clean_identifier("") is None

    def test_init_defaults(self):
        client = make_test_instance()
        assert client._unsubscribe is None
        assert client.devices == []
        assert client.cache_dirty is False

    def test_load_cached_devices(self):
        cached = make_dummy_cached_devices()
        client = make_test_instance(cached_devices=cached)
        client._load_cached_devices(cached)

        assert len(client.devices) == 1
        assert client._device_mac == "A1B2C3D4E5F6"
        assert client._security_code == "98765432"
        # Seeded from value_raw in register_map
        assert client._register_values[32893] == 22

    def test_collect_readable_offsets_includes_setpoints_excludes_chrono(self):
        cached = make_dummy_cached_devices()
        client = make_test_instance(cached_devices=cached)
        client._load_cached_devices(cached)

        offsets = client._collect_readable_offsets(client.devices[0])
        assert 1 in offsets
        assert 6 in offsets
        assert 33 in offsets
        assert 32893 in offsets
        assert 32826 not in offsets


@pytest.fixture
def mqtt_api(monkeypatch):
    """Provide the public HA MQTT API without running a live broker."""
    unsubscribes = [MagicMock(), MagicMock()]
    api = SimpleNamespace(
        async_wait_for_mqtt_client=AsyncMock(return_value=True),
        async_subscribe=AsyncMock(side_effect=unsubscribes),
        async_publish=AsyncMock(),
        is_connected=MagicMock(return_value=True),
        unsubscribes=unsubscribes,
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", api)
    # Python's from-import can also resolve a cached package attribute.
    import homeassistant.components

    monkeypatch.setattr(homeassistant.components, "mqtt", api, raising=False)
    return api


def message(payload, *, retain=False):
    return SimpleNamespace(payload=json.dumps(payload), retain=retain)


class TestLocalMqttProtocolFlow:
    """Exercise the Navel protocol through Home Assistant's shared MQTT client."""

    @pytest.mark.asyncio
    async def test_connect_subscribes_and_authenticates_existing_stove(self, mqtt_api):
        client = make_test_instance(cached_devices=make_dummy_cached_devices())
        await client.connect()
        assert [call.args[1] for call in mqtt_api.async_subscribe.call_args_list] == [
            "navel/A1B2C3D4E5F6",
            "master/responses/navel/A1B2C3D4E5F6",
        ]
        publish = mqtt_api.async_publish.call_args
        assert json.loads(publish.args[2])["pl"]["Cmd"] == "Identity"
        assert publish.kwargs == {"qos": 0, "retain": False}
        await client._message_received(
            message({"mt": "R", "pl": {"Cmd": "Identity", "Result": 0}})
        )

        async def live_reply(_hass, _topic, payload, **_kwargs):
            query = json.loads(payload)
            if query["pl"]["Cmd"] == "RequestReading":
                await client._message_received(
                    message(
                        {
                            "mt": "R",
                            "pl": {
                                "Cmd": "RequestReading",
                                "Items": [1],
                                "Values": [225],
                            },
                        }
                    )
                )

        mqtt_api.async_publish.side_effect = live_reply
        assert await client.validate_local_connection() == {
            "device_mac": "A1B2C3D4E5F6"
        }
        await client.close()
        await client.close()
        for unsubscribe in mqtt_api.unsubscribes:
            unsubscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_mqtt_is_actionable(self, mqtt_api):
        mqtt_api.async_wait_for_mqtt_client.return_value = False
        client = make_test_instance(cached_devices=make_dummy_cached_devices())
        with pytest.raises(
            local_mqtt_module.AguaIOTConnectionError,
            match="Configure Home Assistant MQTT",
        ):
            await client.connect()
        mqtt_api.async_subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_subscription_failure_cleans_up(self, mqtt_api):
        mqtt_api.async_subscribe.side_effect = [
            mqtt_api.unsubscribes[0],
            RuntimeError("subscribe failed"),
        ]
        client = make_test_instance(cached_devices=make_dummy_cached_devices())
        with pytest.raises(
            local_mqtt_module.AguaIOTConnectionError, match="Could not initialize"
        ):
            await client.connect()
        mqtt_api.unsubscribes[0].assert_called_once()
        assert client._unsubscribe is None

    @pytest.mark.asyncio
    async def test_stove_handshake_ping_and_reading(self, mqtt_api):
        client = make_test_instance(cached_devices=make_dummy_cached_devices())
        await client.connect()
        mqtt_api.async_publish.reset_mock()
        await client._message_received(
            message(
                {
                    "mi": "identity-1",
                    "mt": "C",
                    "pl": {
                        "Cmd": "Identity",
                        "Id": "A1B2C3D4E5F6",
                        "Security": "87654321",
                    },
                }
            )
        )
        payloads = [
            json.loads(call.args[2]) for call in mqtt_api.async_publish.call_args_list
        ]
        assert payloads[0]["mt"] == "A"
        assert payloads[0]["mi"] == "identity-1"
        assert payloads[1]["pl"]["Security"] == "87654321"
        assert client.cache_dirty
        cache = client.export_bootstrap_cache()
        restored = make_test_instance(cached_devices=cache)
        restored._load_cached_devices(cache)
        assert restored._security_code == "87654321"
        await client._message_received(
            message({"mi": "ping-1", "mt": "C", "pl": {"Cmd": "Ping"}})
        )
        assert json.loads(mqtt_api.async_publish.call_args.args[2])["mi"] == "ping-1"
        await client._message_received(
            message(
                {
                    "mt": "R",
                    "pl": {
                        "Cmd": "RequestReading",
                        "Items": [1, 6, 32893],
                        "Values": [225, 115, 23],
                    },
                }
            )
        )
        assert client._register_values[1] == 225
        assert client._register_values[32893] == 23
        assert client._response_event.is_set()
        client._identity_ready.clear()
        client._identity_pending = False
        mqtt_api.async_publish.reset_mock()
        published = []

        async def answer_after_identity(_hass, _topic, payload, **_kwargs):
            query = json.loads(payload)
            published.append(query["pl"]["Cmd"])
            if query["pl"]["Cmd"] == "Identity":
                await client._message_received(
                    message({"mt": "R", "pl": {"Cmd": "Identity", "Result": 0}})
                )
            elif query["pl"]["Cmd"] == "RequestReading":
                assert client._identity_ready.is_set()
                await client._message_received(
                    message(
                        {
                            "mt": "R",
                            "pl": {
                                "Cmd": "RequestReading",
                                "Items": [1],
                                "Values": [226],
                            },
                        }
                    )
                )

        mqtt_api.async_publish.side_effect = answer_after_identity
        assert (await client._fetch_device_information(client.devices[0]))[1] == 226
        assert published == ["Identity", "RequestReading"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,retained",
        [
            ({"mt": "Q", "pl": {"Cmd": "Identity"}}, False),
            ({"mt": "A", "pl": {"Cmd": "Ping"}}, False),
            ({"mt": "C", "pl": {"Cmd": "Ping"}}, True),
            ({"mt": "C", "pl": {"Cmd": "Identity", "Id": "FFFFFFFFFFFF"}}, False),
            (
                {
                    "mt": "R",
                    "pl": {
                        "Cmd": "RequestReading",
                        "Items": [1],
                        "Values": ["invalid"],
                    },
                },
                False,
            ),
            (
                {
                    "mt": "R",
                    "pl": {"Cmd": "RequestReading", "Items": [1, 6], "Values": [225]},
                },
                False,
            ),
            ({"mt": "C", "pl": {"Cmd": "Identity", "Id": 123}}, False),
            (
                {"mt": "R", "pl": {"Cmd": "RequestReading", "Items": [], "Values": []}},
                False,
            ),
            (
                {
                    "mt": "R",
                    "pl": {
                        "Cmd": "RequestReading",
                        "Items": [1],
                        "Values": [float("inf")],
                    },
                },
                False,
            ),
            ([], False),
        ],
    )
    async def test_ignores_echo_retained_foreign_and_invalid_messages(
        self, mqtt_api, payload, retained
    ):
        client = make_test_instance(cached_devices=make_dummy_cached_devices())
        await client.connect()
        mqtt_api.async_publish.reset_mock()
        await client._message_received(message(payload, retain=retained))
        mqtt_api.async_publish.assert_not_called()
        assert not client._response_event.is_set()

    @pytest.mark.asyncio
    async def test_disconnected_broker_rejects_publish(self, mqtt_api):
        client = make_test_instance(cached_devices=make_dummy_cached_devices())
        await client.connect()
        mqtt_api.is_connected.return_value = False
        with pytest.raises(
            local_mqtt_module.AguaIOTConnectionError, match="not connected"
        ):
            await client._send_mqtt_publish("navel/A1B2C3D4E5F6", {"mt": "Q"})


@pytest.mark.asyncio
async def test_cached_values_do_not_pass_live_connection_check(mqtt_api):
    client = make_test_instance(cached_devices=make_dummy_cached_devices())
    await client.connect()
    client.buffer_read_timeout = 0.01
    assert client._register_values  # Cache exists, but no live stove has answered.
    with pytest.raises(local_mqtt_module.LocalMqttSetupError, match="No live reading"):
        await client.validate_local_connection()
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_poll_never_reports_cached_readings_as_live(mqtt_api, connected):
    client = make_test_instance(cached_devices=make_dummy_cached_devices())
    await client.connect()
    client.buffer_read_timeout = 0.01
    assert client._register_values
    if not connected:
        await client.close()
    try:
        with pytest.raises(local_mqtt_module.AguaIOTConnectionError):
            await client._fetch_device_information(client.devices[0])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_explicit_mac_uses_shared_ha_mqtt_without_other_integration(mqtt_api):
    cached = make_dummy_cached_devices()
    cached[0]["product_serial"] = "1234567890123"
    cached[0]["id_device"] = "cloud-id"
    client = make_test_instance(cached_devices=cached)
    client.hass.data = {}
    client._mqtt_devices = {"cloud-id": {"device_mac": "AABBCCDDEEFF"}}
    try:
        await client.connect()
        assert client._device_mac == "AABBCCDDEEFF"
        assert not client._owns_mqtt
        assert cached[0]["product_serial"] == "1234567890123"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"temp_air_set": 23},
        {"Cmd": "RequestWriting", "Items": [33, 32768], "Values": [1, 23]},
    ],
)
async def test_eeprom_disabled_blocks_entire_command_before_publish(mqtt_api, payload):
    client = make_test_instance(cached_devices=make_dummy_cached_devices())
    await client.connect()
    mqtt_api.async_publish.reset_mock()
    client._fetch_device_information = AsyncMock(
        return_value=dict(client._register_values)
    )
    await client.update()
    client._mqtt_devices = {"DEV_01": {"allow_eeprom_writes": False}}
    before = dict(client._register_values)
    try:
        with pytest.raises(
            local_mqtt_module.AguaIOTError, match="EEPROM writes are blocked"
        ):
            await client._request_writing(client.devices[0], payload)
        mqtt_api.async_publish.assert_not_awaited()
        assert client._register_values == before
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offset,allowed", [(32767, False), (32768, True), (32768, None)]
)
async def test_ram_and_explicit_eeprom_opt_in(mqtt_api, offset, allowed):
    client = make_test_instance(cached_devices=make_dummy_cached_devices())
    client._mqtt_devices = (
        {} if allowed is None else {"DEV_01": {"allow_eeprom_writes": allowed}}
    )
    client.hass.data = {
        "aguaiot": {"eeprom_budgets": {"DEV_01": MagicMock(reserve=AsyncMock())}}
    }
    await client.connect()
    mqtt_api.async_publish.reset_mock()
    client._fetch_device_information = AsyncMock(return_value={})
    try:
        await client._request_writing(
            client.devices[0],
            {"Cmd": "RequestWriting", "Items": [offset], "Values": [1]},
        )
        mqtt_api.async_publish.assert_awaited_once()
        budget = client.hass.data["aguaiot"]["eeprom_budgets"]["DEV_01"]
        if offset >= 32768:
            budget.reserve.assert_awaited_once()
        else:
            budget.reserve.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_mqtt_publish_keeps_the_eeprom_count(
    mqtt_api, monkeypatch, tmp_path
):
    eeprom = load_component_module("eeprom")

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
    budget = eeprom.EepromBudget(
        SimpleNamespace(async_add_executor_job=execute), "DEV_01"
    )
    client = make_test_instance(cached_devices=make_dummy_cached_devices())
    await client.connect()
    client.hass.data = {"aguaiot": {"eeprom_budgets": {"DEV_01": budget}}}
    mqtt_api.async_publish.reset_mock(side_effect=True)
    mqtt_api.async_publish.side_effect = local_mqtt_module.HomeAssistantError("down")
    try:
        with pytest.raises(local_mqtt_module.AguaIOTConnectionError, match="publish"):
            await client._request_writing(
                client.devices[0],
                {"Cmd": "RequestWriting", "Items": [32893], "Values": [23]},
            )
        assert len(budget.history) == 1
        with open(budget.store.path) as saved:  # noqa: ASYNC230 - tiny local test fixture
            assert json.load(saved)["data"] == budget.history
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_identity_during_subscription_can_be_answered(mqtt_api):
    client = make_test_instance(cached_devices=make_dummy_cached_devices())

    async def subscribe(hass, topic, callback, **kwargs):
        if topic.startswith("master/"):
            await callback(
                message(
                    {
                        "mi": "early",
                        "mt": "C",
                        "pl": {
                            "Cmd": "Identity",
                            "Id": "A1B2C3D4E5F6",
                            "Security": "test-only",
                        },
                    }
                )
            )
        return MagicMock()

    mqtt_api.async_subscribe.side_effect = subscribe
    try:
        await client.connect()
        sent = [
            json.loads(call.args[2]) for call in mqtt_api.async_publish.call_args_list
        ]
        assert any(p["mt"] == "A" and p["mi"] == "early" for p in sent)
    finally:
        await client.close()
