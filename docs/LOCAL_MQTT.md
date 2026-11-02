# Local MQTT

Use an MQTT 3.1.1 broker or protocol-compatible endpoint reachable by both Home Assistant and the stove. Agua IOT runs an MQTT client and does not install or host a broker.

1. Connect Agua IOT through the cloud once to cache device metadata and register maps.
2. Point the stove vendor hostname to your broker using your router or local DNS service.
3. In Agua IOT options, enable **Configure MQTT per stove** to enter the Wi-Fi MAC and connection settings. Leave the host empty to use the existing Home Assistant MQTT integration, or enter a host, port and credentials for a separate connection.
4. Select **Local MQTT** as connection mode. The mode is accepted after fresh readings arrive from every stove in the account.

Each stove has separate topics, register cache and application session. Use MAC client IDs and configure the broker ACLs for your firmware. Stove firmware credentials may be shared and MQTT traffic is unencrypted; use a trusted LAN.

Topics are `navel/<MAC>` and `master/responses/navel/<MAC>`, with 12 uppercase hexadecimal MAC digits. Topic wildcards are not required by Agua IOT. Retained responses are ignored and commands are never retained or automatically retried.

Application write confirmations are an optional endpoint capability. Leave disabled for ordinary MQTT brokers. When enabled, the client waits for an `mt: G` response with matching `mi`, `pl.Cmd: MqttWriteResult`, boolean `Accepted` and optional `Reason`. An accepted receipt confirms forwarding, not physical execution. Missing receipts mean uncertain delivery; writes are not replayed.

Normal polling reads RAM and cached setpoints. Write only registers that belong to this stove model.

Offsets at or above 32768 are EEPROM. Those writes stay allowed. Each stove gets 3 commands per hour and 5 commands per day. At 5 per day a cell of about 32,000 writes lasts about 17.5 years. That is the middle of the usual 10,000 to 100,000 cycle range. Cloud, Bluetooth and local MQTT share that count. One command counts once, even when it changes several registers. The count is saved before the command is sent and kept across restarts. Cloud and Bluetooth give the count back when the write fails. MQTT gives it back only when the command was not sent. A sent command stays counted, including when delivery is uncertain, because the stove does not confirm that it stored the value. If the saved count cannot be read or updated, the write is blocked. RAM reads and RAM writes are free. Turn off "Allow EEPROM writes" in the stove's MQTT settings to block EEPROM writes from this integration. Other programs are not covered. Deleting the saved count is not a supported reset.

The options interface supports English, German, French, Dutch and Portuguese. Generic MQTT connection settings contain no dependency on another custom integration.

Compatibility depends on the Navel protocol and a valid register map for the actual stove. A vendor hostname such as `remote.mcz.it` alone does not establish compatibility. Unica Nobis models have not been verified.

Register reads wait until the stove answers the Identity query. Navel Gateway repeats the current session Identity announcement for a client that connects afterwards. The learned registration code is saved in the Home Assistant private device cache for later starts and redacted from diagnostics.
