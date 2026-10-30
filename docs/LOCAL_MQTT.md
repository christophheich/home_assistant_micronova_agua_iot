## Local Wi-Fi MQTT mode

This integration supports direct local control for Micronova stoves equipped with a Navel 2.0 Wi-Fi module, including MCZ, Jolly Mec, Cadel, and other brands that use Agua IOT.

### How it works

When running in local MQTT mode, Home Assistant runs an embedded MQTT server on port 1883. The stove connects to this local server instead of the vendor cloud servers.

- Authentication uses the standard Navel credentials (`navel` / `navel!!`).
- The integration learns the stove MAC address and security PIN dynamically from the initial `Identity` announce packet or from the cached cloud bootstrap data.
- Read operations send `RequestReading` queries to pull live sensor values over your local network.
- Write operations send `RequestWriting` packets when you adjust temperatures, power levels, or switches.
- Regular ping exchanges keep the Wi-Fi module connected without internet access.

### Requirements

1. **Initial cloud setup.** Complete the initial integration setup once with your vendor cloud account. This downloads the model register map and bootstrap metadata.
2. **DNS rewrite.** Point your vendor backend hostname to the local IP address of your Home Assistant instance. For example, if your stove uses `remote.mcz.it`, create a DNS rewrite rule in Pi-hole, AdGuard Home, or OPNsense that maps `remote.mcz.it` to your Home Assistant IP.
3. **Port 1883 available.** Make sure TCP port 1883 on Home Assistant is free, or set a custom port in the integration options. If you already run Mosquitto on port 1883, pick a different port in the options and add a firewall port forward from the stove IP on port 1883 to your custom port.

### Enabling local MQTT mode

1. Complete the standard cloud setup in Home Assistant.
2. Confirm that your DNS rewrite is active and resolves the vendor domain to Home Assistant.
3. Open the integration options in Home Assistant.
4. Set **Connection mode** to `Local MQTT (Navel 2.0 Wi-Fi)`.
5. Save the form. Home Assistant starts listening on the configured MQTT port.
6. When the stove reconnects or restarts, it connects directly to Home Assistant.

### EEPROM wear protection and write limits

Micronova control boards split registers across two memory regions:

- **Registers below 32768 (RAM).** Sensor values such as flue gas temperature, room temperature, fan speeds, flame probe, and running state live in volatile microcontroller RAM. In local MQTT mode, the integration only polls offsets below 32768. Reading RAM causes zero flash wear. On some boards, turning the stove on or off also writes to RAM offset 33.
- **Registers at or above 32768 (EEPROM).** Target setpoints, schedules, and hardware settings live in non-volatile EEPROM flash memory. Writes to these offsets only happen when a command explicitly targets them, such as changing the thermostat temperature setpoint.

> [!CAUTION]
> EEPROM flash cells on Micronova boards have a finite lifespan, typically rated for 10,000 to 100,000 write cycles per address. Manual adjustments a few times a day will last for decades. Automated scripts that change the target temperature every few minutes will wear out the EEPROM chip and permanently damage the stove control board.

To prevent runaway automations from damaging the hardware, the local MQTT transport enforces three configurable safeguards in the integration options:

- **Allow EEPROM writes.** Enabled by default so thermostat setpoints work normally. If you only want to monitor sensors and toggle RAM-based states, you can disable EEPROM writes entirely. Any attempt to write to an offset at or above 32768 will be rejected.
- **Maximum EEPROM writes per hour.** Defaults to 15 writes per hour.
- **Maximum EEPROM writes per 24 hours.** Defaults to 50 writes per day.

When a command exceeds the hourly or daily limit, the integration blocks the write and logs an error before sending any packet to the stove. Writes to volatile RAM registers below 32768 are not rate-limited because they do not affect flash longevity.

### Protocol internals for power users

If you want to inspect the traffic or understand why a simple DNS rewrite is enough to take over the connection, here is what happens on the wire:

1. **No TLS or certificate pinning.** Even though the mobile apps talk to the cloud API over HTTPS on port 443, the Navel 2.0 Wi-Fi module inside the stove does not use TLS on port 443 for its control channel. If port 443 is unreachable or closed, the module ignores it and connects directly via plain, unencrypted TCP on port 1883 using MQTT 3.1.1. No certificate validation or pinning takes place on the stove.
2. **Static MQTT broker credentials.** Every Navel 2.0 module authenticates against the MQTT broker with the hardcoded username `navel` and password `navel!!`. The MQTT `ClientId` is the 12-character uppercase hex MAC address of the Wi-Fi module.
3. **Topic structure.** Once connected, the stove subscribes to `navel/<MAC>` and publishes all messages to that same topic `navel/<MAC>`. Because both directions share one topic, the embedded server in Home Assistant sends commands directly over the stove socket without echoing them back to itself.
4. **Application-layer handshake.** Right after subscribing, the stove publishes an `Identity` announce packet (`mt: "C"`) containing its MAC address (`Id`), board type (`PELLET_BOARD`), firmware revision, and its 8-character registration PIN (`Security`). To activate the session, the server must send two packets back:
   - An acknowledgment (`mt: "A"`) with `cmd: "Identity"`, `Success: true`, and `Result: 0`.
   - A client authentication query (`mt: "Q"`) with `Cmd: "Identity"`, `Id: "<MAC>"`, and `Security: "<PIN>"`.
   Once the stove replies with `mt: "R"`, it accepts register queries and write commands.
5. **Keepalive pings.** Beyond standard MQTT `PINGREQ` / `PINGRESP` frames (`0xC0` / `0xD0`), the stove sends a JSON `Ping` command (`mt: "C"`) every few minutes. If the server does not answer with `mt: "A"` and `Success: true`, the module drops the TCP connection and reconnects.
6. **Register reads and writes.** Reading registers sends `Cmd: "RequestReading"` with an array of register `Items` and 16-bit `Masks` (`65535`). Writing registers sends `Cmd: "RequestWriting"` with `Protocol: "RWMSmaster"`, `BitData` (`8` or `16` depending on mask width), `Endianess: ["L"]`, `Items`, `Masks`, and `Values`.

### Tested hardware and compatibility

This mode has been tested on an MCZ pellet stove using the `Easy Connect Plus` profile (`remote.mcz.it`, customer code `746318`) with a Micronova Navel 2.0 Wi-Fi module (`PELLET_BOARD`).

Because the Navel 2.0 firmware uses the same `navel` MQTT client across brands, this mode should work out of the box with other `Easy Connect`, `Easy Connect Plus`, and `Agua IOT` stoves that connect on port 1883. If you test this mode with another stove model or vendor app, please share your model and vendor endpoint in an issue or pull request.
