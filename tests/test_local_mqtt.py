"""Unit tests for the LocalMqttAguaIOT transport."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers import AguaIOTError, load_component_module

local_mqtt_module = load_component_module("local_mqtt")
LocalMqttAguaIOT = local_mqtt_module.LocalMqttAguaIOT
DEFAULT_LOCAL_MQTT_PORT = local_mqtt_module.DEFAULT_LOCAL_MQTT_PORT


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
                "temp_air_get": {"offset": 1, "mask": 65535, "formula": "# / 10", "reg_type": "GET"},
                "temp_smoke_get": {"offset": 6, "mask": 65535, "formula": "#", "reg_type": "GET"},
                "status_managed_set": {"offset": 33, "mask": 65535, "formula": "#", "reg_type": "SET"},
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
    port=1883,
    cached_devices=None,
    allow_eeprom_writes=True,
    max_eeprom_writes_per_hour=15,
    max_eeprom_writes_per_day=50,
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
        port=port,
        allow_eeprom_writes=allow_eeprom_writes,
        max_eeprom_writes_per_hour=max_eeprom_writes_per_hour,
        max_eeprom_writes_per_day=max_eeprom_writes_per_day,
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
        assert client.mqtt_port == 1883
        assert client.allow_eeprom_writes is True
        assert client.max_eeprom_writes_per_hour == 15
        assert client.max_eeprom_writes_per_day == 50
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

    def test_mqtt_publish_encoding(self):
        client = make_test_instance()
        topic = "navel/TESTMAC"
        payload = b'{"hello":"world"}'
        packet = client._encode_mqtt_publish(topic, payload)

        # Header 0x30 = PUBLISH, QoS 0
        assert packet[0] == 0x30
        # Topic length
        topic_len = int.from_bytes(packet[2:4], "big")
        assert topic_len == len(topic)
        extracted_topic = packet[4 : 4 + topic_len].decode("utf-8")
        assert extracted_topic == topic
        assert payload in packet


class TestEepromProtection:
    """Test EEPROM write permission and rate limits."""

    def test_ram_writes_always_allowed(self):
        client = make_test_instance(
            allow_eeprom_writes=False,
            max_eeprom_writes_per_hour=1,
            max_eeprom_writes_per_day=1,
        )
        # Offset 33 is in RAM (< 32768), so it should never be blocked or counted
        for _ in range(10):
            client._check_and_record_eeprom_write([33, 51])
        assert len(client._eeprom_write_timestamps) == 0

    def test_eeprom_writes_disabled_blocks_write(self):
        client = make_test_instance(allow_eeprom_writes=False)
        with pytest.raises(AguaIOTError, match="EEPROM writes are disabled"):
            client._check_and_record_eeprom_write([32893])

    def test_eeprom_hourly_rate_limit_enforced(self):
        client = make_test_instance(
            allow_eeprom_writes=True,
            max_eeprom_writes_per_hour=3,
            max_eeprom_writes_per_day=10,
        )
        client._check_and_record_eeprom_write([32893])
        client._check_and_record_eeprom_write([32893])
        client._check_and_record_eeprom_write([32893])

        with pytest.raises(AguaIOTError, match="EEPROM write rate limit exceeded"):
            client._check_and_record_eeprom_write([32893])

    def test_eeprom_daily_rate_limit_enforced(self):
        client = make_test_instance(
            allow_eeprom_writes=True,
            max_eeprom_writes_per_hour=10,
            max_eeprom_writes_per_day=2,
        )
        client._check_and_record_eeprom_write([32893])
        client._check_and_record_eeprom_write([32893])

        with pytest.raises(AguaIOTError, match="EEPROM daily write limit exceeded"):
            client._check_and_record_eeprom_write([32893])


class TestLocalMqttProtocolFlow:
    """Test embedded MQTT network protocol handlers."""

    @pytest.mark.asyncio
    async def test_server_bind_and_close(self):
        # Pick an arbitrary high port for testing
        cached = make_dummy_cached_devices()
        client = make_test_instance(port=18883, cached_devices=cached)
        client._load_cached_devices(cached)
        await client.start_server()
        assert client._server is not None
        assert client._server.is_serving()

        validation = await client.validate_local_connection()
        assert validation["server_port"] == 18883
        assert validation["connected_stove"] is False

        await client.close()
        assert client._server is None

    @pytest.mark.asyncio
    async def test_full_stove_handshake(self):
        cached = make_dummy_cached_devices()
        client = make_test_instance(port=18884, cached_devices=cached)
        client._load_cached_devices(cached)
        await client.start_server()

        # Connect simulated stove client
        reader, writer = await asyncio.open_connection("127.0.0.1", 18884)

        # 1. Send MQTT CONNECT with ClientId 'A1B2C3D4E5F6'
        client_id = "A1B2C3D4E5F6"
        cid_bytes = client_id.encode("utf-8")
        connect_var_header = b"\x00\x04MQTT\x04\x02\x00\x3c"
        connect_payload = len(cid_bytes).to_bytes(2, "big") + cid_bytes
        body = connect_var_header + connect_payload
        connect_packet = b"\x10" + bytes([len(body)]) + body

        writer.write(connect_packet)
        await writer.drain()

        # Read CONNACK (0x20, 0x02, 0x00, 0x00)
        connack = await reader.read(4)
        assert connack == b"\x20\x02\x00\x00"
        assert client._device_mac == "A1B2C3D4E5F6"

        # 2. Send MQTT SUBSCRIBE to navel/A1B2C3D4E5F6
        topic = "navel/A1B2C3D4E5F6"
        sub_body = b"\x00\x01\x00" + bytes([len(topic)]) + topic.encode("utf-8") + b"\x00"
        sub_packet = b"\x82" + bytes([len(sub_body)]) + sub_body
        writer.write(sub_packet)
        await writer.drain()

        # Read SUBACK (0x90, 0x03, msg_id, qos)
        suback = await reader.read(5)
        assert suback[0] == 0x90
        assert suback[1] == 0x03

        # 3. Send Stove Identity announcement (mt: 'C')
        identity_pl = {
            "mi": "msg-001",
            "mt": "C",
            "pl": {
                "Cmd": "Identity",
                "Id": "A1B2C3D4E5F6",
                "Security": "87654321",
                "Devices": {"Type": "PELLET_BOARD"},
            },
        }
        id_bytes = json.dumps(identity_pl).encode("utf-8")
        pub_packet = client._encode_mqtt_publish("navel/A1B2C3D4E5F6", id_bytes)

        writer.write(pub_packet)
        await writer.drain()

        # Server should reply with ACK and Auth Query
        await asyncio.sleep(0.3)
        assert client._security_code == "87654321"

        # 4. Simulate Stove Ping (mt: 'C')
        ping_pl = {"mi": "msg-002", "mt": "C", "pl": {"Cmd": "Ping"}}
        ping_bytes = json.dumps(ping_pl).encode("utf-8")
        pub_ping = client._encode_mqtt_publish("navel/A1B2C3D4E5F6", ping_bytes)
        writer.write(pub_ping)
        await writer.drain()

        # 5. Simulate Stove sending RequestReading response
        reading_pl = {
            "mi": "msg-003",
            "mt": "R",
            "pl": {
                "Cmd": "RequestReading",
                "Items": [1, 2, 6, 32893],
                "Values": [225, 230, 115, 23],
            },
        }
        read_bytes = json.dumps(reading_pl).encode("utf-8")
        pub_reading = client._encode_mqtt_publish("navel/A1B2C3D4E5F6", read_bytes)
        writer.write(pub_reading)
        await writer.drain()

        await asyncio.sleep(0.1)
        assert client._register_values[1] == 225
        assert client._register_values[2] == 230
        assert client._register_values[6] == 115
        assert client._register_values[32893] == 23

        writer.close()
        await writer.wait_closed()
        await client.close()
