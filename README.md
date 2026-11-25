# Chandler Systems for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A [Home Assistant](https://www.home-assistant.io/) custom integration for monitoring [Chandler Systems](https://www.chandlersystemsinc.com/) Signature-series water treatment valves over Bluetooth Low Energy (BLE).

This is a fork of the original integration by [@toekneestuck](https://github.com/toekneestuck), which is no longer maintained. It adds fixes for device registration, message decoding and error handling, plus the water and flow device classes so the gallons sensor works with the Energy dashboard's water panel. See the commit history for details.

## Features

- Fully local. No cloud; it talks to the valve directly over BLE.
- Auto-discovery from BLE advertisements.
- Connects when the valve advertises (at most once a minute), stays connected while the valve pushes data, then disconnects after 10 seconds of silence.
- 40+ sensors: battery, flow, water usage, regeneration status, salt level, diagnostics.
- 7 binary sensors: regeneration, motor, aeration mode and a few others.
- Only creates entities that apply to your valve type (softener, aeration filter, etc.).

## Supported Devices

Chandler Systems Signature-series water treatment devices, including:

- Metered and timeclock softeners
- Backwashing filters
- Aeration filters
- HydroxR, ReactR, and Ultra filters

Across valve series 2-6 (D12, D15, CS125, CS150, CS121).

## Requirements

- Home Assistant 2024.12 or newer
- A Bluetooth adapter accessible to Home Assistant (built-in, USB dongle, or ESPHome Bluetooth proxy)
- Your device's API key (UUID format, provided by Chandler Systems)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three-dot menu in the top right and select **Custom repositories**
3. Add `https://github.com/Timeteo/hass_chandler_systems` and pick **Integration** as the category
4. Search for **Chandler Systems** and install it
5. Restart Home Assistant

### Manual

1. Copy `custom_components/hass_chandler_systems` to `config/custom_components/chandler_systems` (the folder name must match the domain)
2. Restart Home Assistant

### Switching from the original integration

HACS won't uninstall a repository while its integration is configured, so:

1. Delete the Chandler Systems integration under **Settings > Devices & Services**
2. Uninstall the original in HACS and remove it from your custom repositories
3. Add this repository and install it as above, then restart
4. Set the integration up again with your API key

You lose the old entities' history, and entity IDs will gain a device prefix because a device is now created.

## Configuration

1. Ensure Bluetooth is available in Home Assistant
2. Your device should be auto-discovered. Look for a notification in **Settings > Devices & Services**
3. If not auto-discovered, click **Add Integration**, search for **Chandler Systems**, and enter the Bluetooth address by hand
4. Enter your API key when prompted
5. The integration will connect, authenticate, and begin receiving data

## Sensors

### Dashboard

| Sensor | Unit | Description |
|--------|------|-------------|
| Battery Level | mV | Device battery voltage |
| Total Gallons Remaining | gal | Estimated gallons until regeneration |
| Peak Flow Daily | GPM | Highest flow rate recorded today |
| Water Hardness | gpg | Configured water hardness |
| Water Used Today | gal | Total water consumed today |
| Average Water Used | gal | Daily average water usage |
| Salt Tank Capacity | lbs | Total salt tank capacity |
| Salt Tank Remaining | lbs | Estimated salt remaining |

### Flow & Valve

| Sensor | Unit | Description |
|--------|------|-------------|
| Present Flow | GPM | Current water flow rate |
| Valve Status | | Current valve position |
| Regeneration State | | Current regeneration phase (idle, backwash, brine draw, etc.) |
| Valve Error | | Error state if any (lost home, encoder error, motor timeout, etc.) |

### History & Diagnostics

| Sensor | Unit | Description |
|--------|------|-------------|
| Days in Operation | days | Total days the device has been running |
| Days Since Regeneration | days | Days since last regeneration cycle |
| Gallons Since Regeneration | gal | Water used since last regeneration |
| Regeneration Counter | | Total regeneration cycles |
| Total Gallons | gal | Lifetime water usage |
| Serial Number | | Device serial number |
| Firmware Version | | Current firmware version |

### Binary Sensors

| Sensor | Description |
|--------|-------------|
| Regeneration Active | Whether a regeneration cycle is in progress |
| Regeneration Motor in Progress | Whether the valve motor is currently running |
| Regeneration in Aeration | Whether the device is in aeration mode |
| Regeneration Soak Mode | Whether the device is in soak mode |
| Prefill Enabled | Whether prefill is enabled |
| Auto Reserve Mode | Whether auto reserve capacity is enabled |
| Display Off | Whether the device display is turned off |

## Troubleshooting

- **Device not discovered**: Ensure your Bluetooth adapter is working and the device is powered on and within range. ESPHome Bluetooth proxies can extend range.
- **Authentication failed**: Verify your API key is correct. It should be a UUID (32 hex characters, with or without dashes).
- **Frequent disconnections**: The integration disconnects after 10 seconds without data. This is normal. It reconnects on the next advertisement.
- **Missing sensors**: Some sensors only appear for specific device types. Softener-specific sensors (salt, hardness, brine) won't appear for filter-only devices.

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/Timeteo/hass_chandler_systems/issues).

## License

See [LICENSE](LICENSE) for details.
