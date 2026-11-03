"""Test the update coordinator."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.helpers import load_component_module

try:
    from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
    from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers.update_coordinator import UpdateFailed
except ImportError:  # pragma: no cover
    pytest.skip("homeassistant is required", allow_module_level=True)


class StubLocalBleAguaIOT:
    """Placeholder for the Bluetooth transport, which is not tested here."""

    def __init__(self, *args, **kwargs):
        self.device_options = kwargs.get("device_options") or {}
        self._cached_devices = kwargs.get("cached_devices") or []
        self._only_device_ids = kwargs.get("only_device_ids")
        self.air_temp_fix = kwargs.get("air_temp_fix", False)
        self.devices = []


coordinator_module = load_component_module(
    "coordinator",
    stubbed_modules={
        "local_ble": {
            "DEFAULT_CHAR_UUID": "0000ffe1-0000-1000-8000-00805f9b34fb",
            "DEFAULT_SERVICE_UUID": "0000ffe5-0000-1000-8000-00805f9b34fb",
            "LocalBleAguaIOT": StubLocalBleAguaIOT,
        },
    },
)
const = load_component_module("const")
LocalMqttAguaIOT = load_component_module("local_mqtt").LocalMqttAguaIOT

AguaIOTDataUpdateCoordinator = coordinator_module.AguaIOTDataUpdateCoordinator
AguaIOTUpdateError = coordinator_module.AguaIOTUpdateError


def make_config_entry(connection_mode=None):
    """Return a config entry stub."""
    config_entry = MagicMock()
    config_entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    config_entry.data = {
        const.CONF_API_URL: "https://example.com",
        const.CONF_CUSTOMER_CODE: "000000",
        CONF_EMAIL: "test@example.com",
        CONF_PASSWORD: "test",
        const.CONF_UUID: "test-uuid",
    }
    options = {}
    if connection_mode:
        options[const.CONF_CONNECTION_MODE] = connection_mode
    config_entry.options = options
    config_entry.subentries = {}
    return config_entry


def make_coordinator(connection_mode=None):
    """Return a coordinator with a mocked AguaIOT transport."""
    coordinator = AguaIOTDataUpdateCoordinator(
        hass=MagicMock(), config_entry=make_config_entry(connection_mode)
    )
    if not connection_mode or connection_mode == const.CONNECTION_MODE_CLOUD:
        coordinator.agua = MagicMock()
        coordinator.agua.connect = AsyncMock()
        coordinator.agua.update = AsyncMock()
    return coordinator


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_error_raises_update_failed(self):
        coordinator = make_coordinator()
        coordinator.agua.update = AsyncMock(
            side_effect=AguaIOTUpdateError("terminated")
        )

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_successful_update_does_not_raise(self):
        coordinator = make_coordinator()

        assert await coordinator._async_update_data() is None
        assert coordinator.last_update_success is True


class TestFirstRefresh:
    @pytest.mark.asyncio
    async def test_update_error_raises_config_entry_not_ready(self):
        coordinator = make_coordinator()
        coordinator.agua.update = AsyncMock(
            side_effect=AguaIOTUpdateError("terminated")
        )

        with pytest.raises(ConfigEntryNotReady):
            await coordinator.async_config_entry_first_refresh()

        assert coordinator.last_update_success is False

    @pytest.mark.asyncio
    async def test_setup_error_raises_config_entry_not_ready(self):
        coordinator = make_coordinator()
        coordinator.agua.connect = AsyncMock(
            side_effect=AguaIOTUpdateError("terminated")
        )

        with pytest.raises(ConfigEntryNotReady):
            await coordinator.async_config_entry_first_refresh()

        assert coordinator.last_update_success is False

    @pytest.mark.asyncio
    async def test_setup_succeeds_after_a_failed_read(self):
        coordinator = make_coordinator()
        coordinator.agua.update = AsyncMock(
            side_effect=AguaIOTUpdateError("terminated")
        )

        with pytest.raises(ConfigEntryNotReady):
            await coordinator.async_config_entry_first_refresh()

        # The next attempt of Home Assistant to set up the config entry
        # succeeds as soon as the device can be read again.
        coordinator.agua.update = AsyncMock(return_value=None)
        await coordinator.async_config_entry_first_refresh()

        assert coordinator.last_update_success is True


class TestConnectionModes:
    def test_local_mqtt_mode_initialization(self):
        coordinator = make_coordinator(const.CONNECTION_MODE_LOCAL_MQTT)
        assert isinstance(coordinator.agua, LocalMqttAguaIOT)
        assert coordinator.connection_mode == const.CONNECTION_MODE_LOCAL_MQTT

    def test_local_mqtt_prefers_the_stove_row_over_legacy_options(self):
        entry = make_config_entry(const.CONNECTION_MODE_LOCAL_MQTT)
        entry.options["mqtt_devices"] = {
            "one": {"hostname": "old.example", "allow_eeprom_writes": True}
        }
        current = ConfigSubentry(
            data={"hostname": "127.0.0.1", "port": 1883, "allow_eeprom_writes": False},
            subentry_type="stove",
            title="Cadel",
            unique_id="one",
        )
        entry.subentries = {current.subentry_id: current}
        coordinator = AguaIOTDataUpdateCoordinator(hass=MagicMock(), config_entry=entry)
        assert coordinator.agua._mqtt_devices["one"]["hostname"] == "127.0.0.1"
        assert coordinator.agua._mqtt_devices["one"]["allow_eeprom_writes"] is False

    def test_a_stove_connection_wins_over_the_account_option(self):
        entry = make_config_entry(const.CONNECTION_MODE_LOCAL_MQTT)
        current = ConfigSubentry(
            data={"connection_mode": const.CONNECTION_MODE_CLOUD},
            subentry_type="stove",
            title="Cloud",
            unique_id="one",
        )
        entry.subentries = {current.subentry_id: current}
        coordinator = AguaIOTDataUpdateCoordinator(hass=MagicMock(), config_entry=entry)
        assert not isinstance(coordinator.agua, LocalMqttAguaIOT)
        assert coordinator.connection_mode == const.CONNECTION_MODE_CLOUD

    def test_mixed_stoves_get_one_client_each(self):
        entry = make_config_entry(const.CONNECTION_MODE_CLOUD)
        mqtt = ConfigSubentry(
            data={
                "connection_mode": const.CONNECTION_MODE_LOCAL_MQTT,
                "hostname": "127.0.0.1",
            },
            subentry_type="stove",
            title="Mqtt",
            unique_id="mqtt",
        )
        ble = ConfigSubentry(
            data={
                "connection_mode": const.CONNECTION_MODE_BLUETOOTH,
                "air_temp_fix": True,
            },
            subentry_type="stove",
            title="Ble",
            unique_id="ble",
        )
        cloud = ConfigSubentry(
            data={
                "connection_mode": const.CONNECTION_MODE_CLOUD,
                "reading_error_fix": True,
            },
            subentry_type="stove",
            title="Cloud",
            unique_id="cloud",
        )
        entry.subentries = {
            mqtt.subentry_id: mqtt,
            ble.subentry_id: ble,
            cloud.subentry_id: cloud,
        }
        entry.data[const.CONF_BLE_BOOTSTRAP_DEVICES] = [
            {"id_device": "mqtt", "name": "Mqtt"},
            {"id_device": "ble", "name": "Ble"},
            {"id_device": "cloud", "name": "Cloud"},
        ]
        coordinator = AguaIOTDataUpdateCoordinator(hass=MagicMock(), config_entry=entry)
        router = coordinator.agua
        assert isinstance(router, coordinator_module.StoveRouter)
        assert coordinator.connection_mode is None
        clients = dict(router._clients)
        assert set(clients) == {
            const.CONNECTION_MODE_LOCAL_MQTT,
            const.CONNECTION_MODE_BLUETOOTH,
            const.CONNECTION_MODE_CLOUD,
        }
        assert set(clients[const.CONNECTION_MODE_LOCAL_MQTT]._mqtt_devices) == {"mqtt"}
        assert clients[const.CONNECTION_MODE_LOCAL_MQTT]._only_device_ids == {"mqtt"}
        assert clients[const.CONNECTION_MODE_LOCAL_MQTT].air_temp_fix is False
        assert [
            item["id_device"]
            for item in clients[const.CONNECTION_MODE_LOCAL_MQTT]._cached_devices
        ] == ["mqtt"]
        ble_client = clients[const.CONNECTION_MODE_BLUETOOTH]
        assert set(ble_client.device_options) == {"ble"}
        assert ble_client.device_options["ble"]["air_temp_fix"] is True
        assert ble_client._only_device_ids == {"ble"}
        assert [item["id_device"] for item in ble_client._cached_devices] == ["ble"]
        assert set(clients[const.CONNECTION_MODE_CLOUD].device_options) == {"cloud"}


class TestRouterFailures:
    @pytest.mark.asyncio
    async def test_one_failed_connection_leaves_the_other_stove(self):
        class Client:
            def __init__(self, fail, device_id):
                self.fail = fail
                self.devices = []
                self.device_id = device_id
                self.closed = False

            async def connect(self):
                if self.fail:
                    raise RuntimeError("down")
                self.devices = [MagicMock(id_device=self.device_id)]

            async def close(self):
                self.closed = True

            async def update(self):
                if self.fail:
                    raise RuntimeError("down")

        bad = Client(True, "ble")
        good = Client(False, "cloud")
        router = coordinator_module.StoveRouter(
            [
                (const.CONNECTION_MODE_BLUETOOTH, bad),
                (const.CONNECTION_MODE_CLOUD, good),
            ],
            {"ble"},
        )
        await router.connect()
        assert bad.closed is True
        assert [device.id_device for device in router.devices] == ["cloud"]
        bad.devices = [MagicMock(id_device="ble")]
        await router.update()
        bad.fail = True
        good.fail = True
        good.devices = [MagicMock(id_device="cloud")]
        with pytest.raises(RuntimeError):
            await router.update()

    @pytest.mark.asyncio
    async def test_router_cache_keeps_a_stove_the_export_missed(self):
        coordinator = make_coordinator()
        router = coordinator_module.StoveRouter([], set())
        router.export_bootstrap_cache = lambda: [
            {"id_device": "mqtt", "register_map": {"a": 1}}
        ]
        coordinator.agua = router
        coordinator.config_entry.data[const.CONF_BLE_BOOTSTRAP_DEVICES] = [
            {"id_device": "ble", "register_map": {"b": 1}},
            {"id_device": "mqtt", "register_map": {"old": 1}},
        ]
        await coordinator._async_persist_ble_bootstrap_if_needed()
        saved = coordinator.hass.config_entries.async_update_entry.call_args.kwargs[
            "data"
        ][const.CONF_BLE_BOOTSTRAP_DEVICES]
        by_id = {item["id_device"]: item for item in saved}
        assert by_id["ble"]["register_map"] == {"b": 1}
        assert by_id["mqtt"]["register_map"] == {"a": 1}


class TestCloudBootstrapForLocalConnections:
    @pytest.mark.asyncio
    async def test_cloud_refresh_caches_available_register_maps(self):
        coordinator = make_coordinator()
        cached = {
            "product_serial": "AABBCCDDEEFF",
            "register_map": {"temp_air_get": {"offset": 1}},
        }
        coordinator.agua.devices = [
            MagicMock(export_cache=MagicMock(return_value=cached))
        ]
        await coordinator._async_persist_ble_bootstrap_if_needed()
        update = coordinator.hass.config_entries.async_update_entry
        assert update.call_args.kwargs["data"][const.CONF_BLE_BOOTSTRAP_DEVICES] == [
            cached
        ]

    @pytest.mark.asyncio
    async def test_cloud_refresh_does_not_save_incomplete_maps(self):
        coordinator = make_coordinator()
        coordinator.agua.devices = [
            MagicMock(export_cache=MagicMock(return_value={"register_map": {}}))
        ]
        await coordinator._async_persist_ble_bootstrap_if_needed()
        coordinator.hass.config_entries.async_update_entry.assert_not_called()
