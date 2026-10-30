"""Local MQTT transport for Micronova Navel 2.0 Wi-Fi modules."""

from __future__ import annotations

import asyncio
from collections import deque
import copy
import json
import logging
import time
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .aguaiot import (
    AguaIOTConnectionError,
    AguaIOTError,
    Device,
    aguaiot,
    update_devices,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_LOCAL_MQTT_PORT = 1883
DEFAULT_ALLOW_EEPROM_WRITES = True
DEFAULT_MAX_EEPROM_WRITES_PER_HOUR = 15
DEFAULT_MAX_EEPROM_WRITES_PER_DAY = 50
EEPROM_OFFSET_THRESHOLD = 32768
ACTIVE_EEPROM_PREFIXES = ("chrono_p",)


class LocalMqttAguaIOT:
    """Micronova transport using an embedded local MQTT broker for Navel modules."""

    def __init__(
        self,
        hass: Any,
        api_url: str,
        customer_code: str,
        email: str,
        password: str,
        unique_id: str,
        *,
        login_api_url: str | None = None,
        brand_id: str | None = None,
        brand: str | None = None,
        application_version: str = "1.9.7",
        async_client=None,
        air_temp_fix: bool = False,
        reading_error_fix: bool = False,
        language: str | None = "ENG",
        http_timeout: int | None = 30,
        buffer_read_timeout: int | None = 30,
        port: int = DEFAULT_LOCAL_MQTT_PORT,
        allow_eeprom_writes: bool = DEFAULT_ALLOW_EEPROM_WRITES,
        max_eeprom_writes_per_hour: int = DEFAULT_MAX_EEPROM_WRITES_PER_HOUR,
        max_eeprom_writes_per_day: int = DEFAULT_MAX_EEPROM_WRITES_PER_DAY,
        cached_devices: list[dict[str, Any]] | None = None,
    ) -> None:
        self.hass = hass
        self.api_url = api_url.rstrip("/")
        self.customer_code = customer_code
        self.email = email
        self.password = password
        self.unique_id = unique_id
        self.login_api_url = login_api_url
        self.brand_id = brand_id
        self.brand = brand
        self.application_version = application_version
        self.async_client = async_client
        self.air_temp_fix = air_temp_fix
        self.reading_error_fix = reading_error_fix
        self.language = language
        self.http_timeout = http_timeout or 30
        self.buffer_read_timeout = buffer_read_timeout or 30
        self.mqtt_port = port
        self.allow_eeprom_writes = allow_eeprom_writes
        self.max_eeprom_writes_per_hour = max_eeprom_writes_per_hour
        self.max_eeprom_writes_per_day = max_eeprom_writes_per_day
        self.devices: list[Device] = []

        self._session_uuid = str(uuid.uuid4()).upper()
        self._cached_devices = cached_devices or []
        self._cache_dirty = False

        self._server: asyncio.Server | None = None
        self._client_writer: asyncio.StreamWriter | None = None
        self._client_reader: asyncio.StreamReader | None = None
        self._device_mac: str | None = None
        self._security_code: str | None = None
        self._register_values: dict[int, int] = {}
        self._response_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._eeprom_write_timestamps: deque[float] = deque()

    @property
    def cache_dirty(self) -> bool:
        """Return True when bootstrap cache should be persisted."""
        return self._cache_dirty

    def export_bootstrap_cache(self) -> list[dict[str, Any]]:
        """Export device bootstrap data for config entry persistence."""
        return [device.export_cache() for device in self.devices]

    def mark_cache_persisted(self) -> None:
        """Clear the dirty flag once bootstrap data is stored."""
        self._cache_dirty = False

    def _clean_identifier(self, raw_id: str | None) -> str | None:
        """Strip colons and dashes from MAC or serial identifiers."""
        if not raw_id:
            return None
        return raw_id.replace(":", "").replace("-", "").strip().upper()

    def _seed_register_cache_from_map(self, register_map: dict[str, Any] | None) -> None:
        """Pre-populate initial register values from the persisted bootstrap register map."""
        if not isinstance(register_map, dict):
            return
        for reg in register_map.values():
            if not isinstance(reg, dict) or "offset" not in reg:
                continue
            offset = int(reg["offset"])
            if offset in self._register_values:
                continue
            raw_val = reg.get("value_raw")
            if raw_val is not None:
                try:
                    self._register_values[offset] = int(raw_val)
                except (ValueError, TypeError):
                    pass

    def _load_cached_devices(self, cached_devices: list[dict[str, Any]]) -> None:
        """Restore devices from persisted bootstrap cache."""
        self.devices = []
        for entry in cached_devices:
            reg_map = entry.get("register_map") or entry.get("registers")
            self._seed_register_cache_from_map(reg_map)
            dev = Device(
                entry["id"],
                entry["id_device"],
                entry["id_product"],
                entry["product_serial"],
                entry["name"],
                entry.get("is_online", True),
                entry.get("name_product", "Pellet Stove"),
                entry["id_registers_map"],
                self,
                device_info=entry.get("device_info"),
                register_map=reg_map,
            )
            self.devices.append(dev)

            if not self._device_mac:
                serial_clean = self._clean_identifier(entry.get("product_serial"))
                id_device_clean = self._clean_identifier(entry.get("id_device"))
                self._device_mac = serial_clean or id_device_clean

            if not self._security_code:
                dev_info = entry.get("device_info") or {}
                self._security_code = dev_info.get("reg_code") or dev_info.get("security_code")

    async def _bootstrap_from_cloud(self) -> None:
        """Fetch device metadata and register mappings once via the cloud API."""
        cloud = aguaiot(
            api_url=self.api_url,
            customer_code=self.customer_code,
            email=self.email,
            password=self.password,
            unique_id=self.unique_id,
            login_api_url=self.login_api_url,
            brand_id=self.brand_id,
            brand=self.brand,
            application_version=self.application_version,
            async_client=self.async_client,
            air_temp_fix=self.air_temp_fix,
            reading_error_fix=self.reading_error_fix,
            language=self.language,
            http_timeout=self.http_timeout,
            buffer_read_timeout=self.buffer_read_timeout,
        )
        await cloud.connect()

        self.devices = []
        for dev in cloud.devices:
            reg_map = dev.export_register_map()
            self._seed_register_cache_from_map(reg_map)
            self.devices.append(
                Device(
                    dev.id,
                    dev.id_device,
                    dev.id_product,
                    dev.product_serial,
                    dev.name,
                    dev.is_online,
                    dev.name_product,
                    dev.id_registers_map,
                    self,
                    device_info=dev.device_info_data,
                    register_map=reg_map,
                )
            )
        self._cache_dirty = True

        for dev in self.devices:
            if not self._device_mac:
                serial_clean = self._clean_identifier(dev.product_serial)
                id_device_clean = self._clean_identifier(dev.id_device)
                self._device_mac = serial_clean or id_device_clean
            if not self._security_code:
                dev_info = dev.device_info_data or {}
                self._security_code = dev_info.get("reg_code") or dev_info.get("security_code")

    async def _fetch_device_registers_mapping(self, device: Device) -> dict[str, Any]:
        """Return the cached registers map."""
        register_map = device.export_register_map()
        if register_map:
            return register_map
        raise AguaIOTError(f"No cached registers map found for '{device.name}'")

    async def connect(self) -> None:
        """Initialize devices and start the local MQTT server."""
        if self._cached_devices:
            self._load_cached_devices(self._cached_devices)
        else:
            await self._bootstrap_from_cloud()

        await self.start_server()

    async def start_server(self) -> None:
        """Start local MQTT broker listening for incoming stove connections."""
        if not self._server:
            try:
                self._server = await asyncio.start_server(
                    self._handle_client, "0.0.0.0", self.mqtt_port
                )
                _LOGGER.info(
                    "AguaIOT local MQTT server listening on 0.0.0.0:%s for Navel stoves",
                    self.mqtt_port,
                )
            except Exception as err:
                _LOGGER.error("Failed to start local MQTT server on port %s: %s", self.mqtt_port, err)
                raise AguaIOTConnectionError(
                    f"Could not bind local MQTT server on port {self.mqtt_port}: {err}"
                ) from err

    async def fetch_device_information(self) -> None:
        """Fetch information for all devices."""
        for dev in self.devices:
            await dev.update_mapping()

    async def update(self) -> None:
        """Poll the stove for latest readings and update device states."""
        await update_devices(self.devices)

    async def validate_local_connection(self) -> dict[str, Any]:
        """Validate that the local MQTT server is running."""
        if not self.devices:
            raise AguaIOTError("No Micronova devices are available for local MQTT.")

        if not self._server or not self._server.is_serving():
            raise AguaIOTConnectionError(f"Local MQTT server is not running on port {self.mqtt_port}.")

        return {
            "server_port": self.mqtt_port,
            "connected_stove": bool(self._client_writer and not self._client_writer.is_closing()),
            "device_mac": self._device_mac,
        }

    def _check_and_record_eeprom_write(self, offsets: list[int]) -> None:
        """Enforce EEPROM write permissions and hourly/daily rate limits."""
        eeprom_offsets = [off for off in offsets if off >= EEPROM_OFFSET_THRESHOLD]
        if not eeprom_offsets:
            return

        if not self.allow_eeprom_writes:
            raise AguaIOTError(
                f"Blocked write to EEPROM registers {eeprom_offsets}: EEPROM writes are disabled in options."
            )

        now = time.monotonic()
        # Prune timestamps older than 24 hours (86400 seconds)
        while self._eeprom_write_timestamps and (now - self._eeprom_write_timestamps[0]) > 86400:
            self._eeprom_write_timestamps.popleft()

        writes_last_day = len(self._eeprom_write_timestamps)
        writes_last_hour = sum(1 for ts in self._eeprom_write_timestamps if (now - ts) <= 3600)

        if writes_last_hour >= self.max_eeprom_writes_per_hour:
            raise AguaIOTError(
                f"EEPROM write rate limit exceeded: {writes_last_hour} writes in the last hour "
                f"(limit: {self.max_eeprom_writes_per_hour}/hour). Write to {eeprom_offsets} blocked to prevent flash wear."
            )

        if writes_last_day >= self.max_eeprom_writes_per_day:
            raise AguaIOTError(
                f"EEPROM daily write limit exceeded: {writes_last_day} writes in the last 24 hours "
                f"(limit: {self.max_eeprom_writes_per_day}/day). Write to {eeprom_offsets} blocked to prevent flash wear."
            )

        self._eeprom_write_timestamps.append(now)

    def _collect_readable_offsets(self, device: Device) -> list[int]:
        """Collect RAM and active setpoint/status offsets to read from the stove."""
        register_map = device.export_register_map() or {}
        offsets: set[int] = set()

        for key, reg in register_map.items():
            if not isinstance(reg, dict) or "offset" not in reg:
                continue
            offset = int(reg["offset"])
            reg_type = reg.get("reg_type")
            key_lower = str(key).lower()

            if offset < EEPROM_OFFSET_THRESHOLD:
                offsets.add(offset)
            elif reg_type in ("GET", "ENABLE", "SET") and not key_lower.startswith(ACTIVE_EEPROM_PREFIXES):
                offsets.add(offset)

        if not offsets:
            return [1, 2, 6, 33, 51, 52, 232]

        return sorted(offsets)

    async def _fetch_device_information(self, device: Device) -> dict[int, int]:
        """Query registers from the stove over local MQTT."""
        if not self._client_writer or self._client_writer.is_closing():
            if self._register_values:
                return copy.deepcopy(self._register_values)
            _LOGGER.debug("Stove not connected yet, waiting for initial connection")
            return {}

        target_offsets = self._collect_readable_offsets(device)

        self._response_event.clear()
        masks = [65535] * len(target_offsets)
        query = {
            "mi": str(uuid.uuid4()),
            "mt": "Q",
            "r": {"rj": "Navel"},
            "s": {"ss": "Apk", "sj": self._session_uuid},
            "pl": {
                "Cmd": "RequestReading",
                "Items": target_offsets,
                "Masks": masks,
            },
        }

        target_mac = self._device_mac or self._clean_identifier(device.product_serial) or "UNKNOWN"
        try:
            await self._send_mqtt_publish(f"navel/{target_mac}", query)
            await asyncio.wait_for(
                self._response_event.wait(),
                timeout=min(float(self.buffer_read_timeout), 5.0),
            )
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for RequestReading response, using cached values")
        except Exception as err:
            _LOGGER.warning("Error requesting registers from stove: %s", err)

        return copy.deepcopy(self._register_values)

    async def _request_writing(self, device: Device, payload_or_items: dict[str, Any]) -> None:
        """Send register write command to stove with EEPROM wear protection."""
        if not self._client_writer or self._client_writer.is_closing():
            raise AguaIOTConnectionError("Stove is not connected via local MQTT.")

        if "Cmd" not in payload_or_items:
            item_offsets = []
            masks = []
            bit_data = []
            values = []

            for key, value in payload_or_items.items():
                register = device.get_register(key)
                mask = int(register["mask"])
                item_offsets.append(int(register["offset"]))
                masks.append(mask)
                bit_data.append(16 if mask > 0xFF else 8)
                values.append(int(value))

            payload = {
                "Cmd": "RequestWriting",
                "Protocol": "RWMSmaster",
                "BitData": bit_data,
                "Endianess": ["L"] * len(item_offsets),
                "Items": item_offsets,
                "Masks": masks,
                "Values": values,
            }
        else:
            payload = payload_or_items
            item_offsets = [int(x) for x in payload.get("Items", [])]

        # Enforce EEPROM safety guard before transmitting
        self._check_and_record_eeprom_write(item_offsets)

        target_mac = self._device_mac or self._clean_identifier(device.product_serial) or "UNKNOWN"
        async with self._write_lock:
            cmd = {
                "mi": str(uuid.uuid4()),
                "mt": "Q",
                "r": {"rj": "Navel"},
                "s": {"ss": "Apk", "sj": self._session_uuid},
                "pl": payload,
            }
            _LOGGER.info("Sending RequestWriting to stove: %s", payload)
            await self._send_mqtt_publish(f"navel/{target_mac}", cmd)
            # Optimistically update cached register values so UI reflects changes immediately
            for idx, off in enumerate(item_offsets):
                if idx < len(payload.get("Values", [])):
                    self._register_values[off] = int(payload["Values"][idx])
            await asyncio.sleep(0.5)
            await self._fetch_device_information(device)

    def _encode_mqtt_publish(self, topic: str, payload_bytes: bytes) -> bytes:
        """Encode MQTT 3.1.1 PUBLISH packet (QoS 0)."""
        topic_bytes = topic.encode("utf-8")
        var_header = len(topic_bytes).to_bytes(2, "big") + topic_bytes
        rem_len = len(var_header) + len(payload_bytes)

        rem_bytes = bytearray()
        x = rem_len
        while True:
            b = x % 128
            x //= 128
            if x > 0:
                b |= 128
            rem_bytes.append(b)
            if x == 0:
                break

        return b"\x30" + bytes(rem_bytes) + var_header + payload_bytes

    async def _send_mqtt_publish(self, topic: str, data: dict[str, Any]) -> None:
        """Send a JSON payload over MQTT to the connected stove."""
        if not self._client_writer or self._client_writer.is_closing():
            return
        payload_bytes = json.dumps(data).encode("utf-8")
        packet = self._encode_mqtt_publish(topic, payload_bytes)
        self._client_writer.write(packet)
        await self._client_writer.drain()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle incoming TCP connection on MQTT port."""
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("Connection accepted on MQTT port %s from %s", self.mqtt_port, peer)
        is_stove = False

        try:
            while True:
                header = await reader.read(1)
                if not header:
                    break

                first_byte = header[0]
                pkt_type = first_byte >> 4
                flags = first_byte & 0x0F

                multiplier = 1
                rem_len = 0
                while True:
                    b = await reader.read(1)
                    if not b:
                        break
                    rem_len += (b[0] & 0x7F) * multiplier
                    if (b[0] & 0x80) == 0:
                        break
                    multiplier *= 128

                payload = bytearray()
                while len(payload) < rem_len:
                    chunk = await reader.read(rem_len - len(payload))
                    if not chunk:
                        break
                    payload.extend(chunk)

                # 1: CONNECT
                if pkt_type == 1:
                    is_stove = True
                    self._client_reader = reader
                    self._client_writer = writer

                    try:
                        proto_len = int.from_bytes(payload[:2], "big")
                        pos = 2 + proto_len + 1 + 1 + 2
                        client_id_len = int.from_bytes(payload[pos : pos + 2], "big")
                        client_id = payload[pos + 2 : pos + 2 + client_id_len].decode("utf-8", errors="ignore")
                        if client_id and not self._device_mac:
                            self._device_mac = self._clean_identifier(client_id)
                    except Exception:
                        pass

                    _LOGGER.info("Received MQTT CONNECT from stove (%s bytes) at %s", rem_len, peer)
                    writer.write(b"\x20\x02\x00\x00")
                    await writer.drain()

                # 8: SUBSCRIBE
                elif pkt_type == 8:
                    msg_id = payload[:2]
                    topic_len = int.from_bytes(payload[2:4], "big")
                    topic = payload[4 : 4 + topic_len].decode("utf-8", errors="ignore")
                    _LOGGER.debug("Stove subscribed to MQTT topic '%s'", topic)

                    if topic.startswith("navel/"):
                        self._device_mac = self._clean_identifier(topic.split("/", 1)[1])

                    writer.write(b"\x90\x03" + bytes(msg_id) + b"\x00")
                    await writer.drain()

                # 3: PUBLISH
                elif pkt_type == 3:
                    qos = (flags >> 1) & 0x03
                    topic_len = int.from_bytes(payload[:2], "big")
                    topic = payload[2 : 2 + topic_len].decode("utf-8", errors="ignore")
                    pos = 2 + topic_len
                    if qos > 0:
                        msg_id = payload[pos : pos + 2]
                        pos += 2
                        writer.write(b"\x40\x02" + bytes(msg_id))
                        await writer.drain()

                    data = bytes(payload[pos:])
                    try:
                        text = data.decode("utf-8", errors="ignore")
                        obj = json.loads(text)
                        pl = obj.get("pl", {})
                        cmd_val = pl.get("cmd") or pl.get("Cmd")

                        # Stove Identity announcement
                        if cmd_val == "Identity":
                            if obj.get("mt") == "C":
                                if pl.get("Id"):
                                    self._device_mac = self._clean_identifier(pl.get("Id"))
                                if pl.get("Security"):
                                    self._security_code = str(pl.get("Security"))

                                _LOGGER.info(
                                    "Stove Identity registered: MAC=%s, Board=%s",
                                    self._device_mac,
                                    pl.get("Devices", {}).get("Type"),
                                )

                                # Acknowledge identity packet
                                identity_ack = {
                                    "mi": obj.get("mi", str(uuid.uuid4())),
                                    "mt": "A",
                                    "r": {"rj": "Navel", "rs": "XM"},
                                    "s": {"ss": "Apk", "sj": self._session_uuid},
                                    "pl": {
                                        "cmd": "Identity",
                                        "Success": True,
                                        "Result": 0,
                                    },
                                }
                                await self._send_mqtt_publish(f"navel/{self._device_mac}", identity_ack)

                                # Send Client Identity authentication to stove
                                await asyncio.sleep(0.2)
                                sec_code = self._security_code or ""
                                auth_cmd = {
                                    "mi": str(uuid.uuid4()),
                                    "mt": "Q",
                                    "r": {"rj": "Navel"},
                                    "s": {"ss": "Apk", "sj": self._session_uuid},
                                    "pl": {
                                        "Cmd": "Identity",
                                        "Id": self._device_mac,
                                        "Security": sec_code,
                                    },
                                }
                                _LOGGER.debug("Sending Client Identity Auth to stove")
                                await self._send_mqtt_publish(f"navel/{self._device_mac}", auth_cmd)

                            elif obj.get("mt") == "R":
                                _LOGGER.info("Stove confirmed Client Identity authentication")
                                await asyncio.sleep(0.2)
                                if self.devices:
                                    await self._fetch_device_information(self.devices[0])

                        # Stove Ping keepalive
                        if cmd_val == "Ping" and obj.get("mt") == "C":
                            ping_ack = {
                                "mi": obj.get("mi", str(uuid.uuid4())),
                                "mt": "A",
                                "r": {"rj": "Navel", "rs": "XM"},
                                "s": {"ss": "Apk", "sj": self._session_uuid},
                                "pl": {"cmd": "Ping", "Success": True},
                            }
                            await self._send_mqtt_publish(f"navel/{self._device_mac}", ping_ack)

                        # Parse register values from RequestReading response
                        items = pl.get("Items") or pl.get("items")
                        values = pl.get("Values") or pl.get("values")
                        if items and values:
                            for idx, item in enumerate(items):
                                if idx < len(values):
                                    self._register_values[int(item)] = int(values[idx])
                            _LOGGER.debug(
                                "Updated %s stove registers from local MQTT reading",
                                len(items),
                            )
                            self._response_event.set()

                    except Exception as err:
                        _LOGGER.warning("Error parsing stove MQTT publish on '%s': %s", topic, err)

                # 12: PINGREQ
                elif pkt_type == 12:
                    writer.write(b"\xd0\x00")
                    await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception as err:
            _LOGGER.debug("Local MQTT client connection terminated: %s", err)
        finally:
            if is_stove:
                _LOGGER.info("Stove disconnected from local MQTT server")
                if self._client_writer == writer:
                    self._client_writer = None
                    self._client_reader = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def close(self) -> None:
        """Close the local MQTT server and client connection."""
        if self._client_writer:
            try:
                self._client_writer.close()
                await asyncio.wait_for(self._client_writer.wait_closed(), timeout=1.0)
            except Exception:
                pass
            self._client_writer = None
            self._client_reader = None

        if self._server:
            try:
                self._server.close()
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            except Exception:
                pass
            self._server = None

    async def stop_server(self) -> None:
        """Alias for close."""
        await self.close()
