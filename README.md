# CYD-Klipper on the ESP32-2432S028 (2USB)

Notes, automation and a test harness for running
[**CYD-Klipper**](https://github.com/suchmememanyskill/CYD-Klipper) on the
2.8" **CYD ESP32-2432S028** board with two USB connectors, as a touch panel for
a Klipper printer.

> The firmware itself is **not** in this repository — it is upstream, by
> [suchmememanyskill](https://github.com/suchmememanyskill/CYD-Klipper).
> What lives here is everything around it: the walkthrough for this exact
> board revision, a scripted esptool fallback, and a mock printer to test
> against without owning a 3D printer.

## The guide

[`CYD-Klipper-CYD-2USB.md`](CYD-Klipper-CYD-2USB.md) is a step-by-step
walkthrough covering:

- flashing through the browser WebSerial installer (Chrome/Edge only), and the
  BOOT + RST recovery sequence when it will not start,
- the **esptool fallback** for when WebSerial cannot see the port or the board
  is already running something else,
- the **2USB trap**: these boards often carry an **ST7789** panel instead of
  the ILI9341 of the classic `R` revision, which is why the image looks
  colour-inverted after flashing. That is a setting, not a broken board.
- provisioning Wi-Fi and the Moonraker host, either from the touchscreen or
  over the serial console,
- pin maps, calibration and a troubleshooting table.

## Scripted flashing

```bash
bash firmware/flash.sh                       # COM5, esp32-2432S028R, full erase
bash firmware/flash.sh --close-monitor       # first stop CYD-PC-Monitor, which holds COM5
bash firmware/flash.sh -p COM7 --no-erase    # other port, skip the erase
bash firmware/flash.sh --board esp32-3248S035C
bash firmware/flash.sh --check               # don't write anything: port + chip only
bash firmware/flash.sh --list-boards         # board folders inside the release archive
```

What it does, in order:

1. verifies `python`, `esptool` and a free COM port,
2. optionally closes whatever holds the port (by exe name),
3. downloads the release's `firmware.zip` if it is not cached,
4. unpacks the folder for the requested board,
5. optionally erases the whole flash (first install, or after foreign firmware),
6. writes `merged_firmware.bin` at `0x0`, then reads the flash back and compares
   hashes,
7. tails the serial console to confirm the firmware actually booted.

> `merged_firmware.bin` spans `0x0` up to roughly `0x14xxxx`, which includes the
> `nvs` region — so Wi-Fi credentials and printer settings are wiped **even
> without** `--erase`.

`firmware/serial_log.py` reads the boot log without disturbing DTR/RTS.

## Mock printer test harness

`test_printer/` brings up a stand-in Klipper host so the panel can be exercised
end to end with no printer present:

```bash
bash test_printer/up.sh        # Docker up -> containers -> patch virtual printer -> wait for "ready"
cd test_printer && docker compose down
```

It contains:

| File | Role |
| --- | --- |
| `mock_moonraker.py` | Moonraker-compatible API stub |
| `mock_smoke_test.py` | asserts the panel's requests arrive and are answered |
| `wired_test.py` | drives the real serial link rather than HTTP |
| `autostart.py` | keeps the mock and the serial bridge running together |
| `.serial/serial_server.py` | exposes the panel's serial port over a socket |
| `docker-compose.yml`, `up.sh`, `wired.sh` | orchestration |

Runtime state and logs (`.run/`, `*.log`) are gitignored.

## Layout

```
├── CYD-Klipper-CYD-2USB.md      the walkthrough
├── firmware/
│   ├── flash.sh                 esptool flashing + verification
│   └── serial_log.py            boot log without touching DTR
└── test_printer/                mock Moonraker + serial test harness
```

## Requirements

- Bash (Git Bash is fine), Python 3, `esptool`
- Docker Desktop for the mock printer
- A Chromium-based browser for the WebSerial installer

## License

The upstream CYD-Klipper firmware is licensed by its own author and is not
redistributed here. The scripts and documentation in this repository are the
author's own work; no license file is included, so contact the author before
redistributing.
