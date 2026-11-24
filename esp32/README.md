# ESP32-P4 hyperspectral scanner

Port of the Raspberry Pi scanner in `code/CameraPi/` to an ESP32-P4. The full plan is in
the project's port plan document. This folder is an ESP-IDF project.

**Status: milestone 2.** Wi-Fi, microSD card, a web page, capture of single raw frames
from the Raspberry Pi HQ Camera (Sony IMX477), and the scan motor with limit-switch
calibration. It builds for the ESP32-P4 but has not been run on hardware yet. The webcam
and the scan loop come in later milestones.

## Hardware

| Part | Notes |
| --- | --- |
| Waveshare ESP32-P4-WIFI6-DEV-KIT | ESP32-P4, 32 MB PSRAM, ESP32-C6 for Wi-Fi, MIPI CSI, TF slot, USB-A host |
| Raspberry Pi HQ Camera (IMX477) | Same camera and optics as the Pi build |
| Camera cable | Match the board's CSI connector to the camera's 15-pin 1 mm FPC |
| microSD card | 32 GB or larger, A1/U3, FAT32 or exFAT |
| A4988 stepper driver | Kept from the Pi build (milestone 3) |
| 5 V 3 A USB-C supply | Board, camera and webcam |

## Build and flash

Needs ESP-IDF v5.5 or newer ([install guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/get-started/)).

```sh
cd esp32
idf.py set-target esp32p4
idf.py menuconfig        # Hyperspectral scanner -> Wi-Fi: set your network
idf.py build flash monitor
```

The first build downloads `esp_wifi_remote` and `esp_hosted` from the Espressif component registry.

The ESP32-C6 on the board must run the ESP-Hosted slave firmware. Boards normally ship
with it flashed. If Wi-Fi fails to start, flash it from the
[esp-hosted-mcu](https://github.com/espressif/esp-hosted-mcu) repository.

## Board settings to check

All under `idf.py menuconfig` -> Hyperspectral scanner:

- **Camera I2C SDA/SCL** default to GPIO7/GPIO8. Confirm against the board schematic.
- **Camera power enable GPIO.** The HQ Camera stays off until CAM_GPIO on its connector is
  high. Set this if the board routes that pin to a GPIO.
- **MIPI PHY LDO** (channel 3, 2500 mV) and **SD card LDO** (channel 4) match Espressif's
  and Waveshare's P4 boards.

## Motor wiring

The motor code is a port of `code/CameraPi/motor.py`. Pins are set under
menuconfig -> Hyperspectral scanner -> Scan motor. Check the defaults against the
board's header before wiring.

| A4988 / switch | Pi BCM pin | ESP32-P4 default |
| --- | --- | --- |
| STEP | 20 | GPIO20 |
| DIR | 26 | GPIO21 |
| SLEEP | 16 | GPIO22 |
| MS1-MS3 (tied, high = 1/16 step) | 19 | GPIO23 |
| Limit switch "white" | 13 | GPIO45 |
| Limit switch "green" | 6 | GPIO46 |

Run the A4988's logic supply (VDD) from the board's 3.3 V, so its inputs match the P4's
3.3 V GPIO. The motor supply (VMOT) stays as it was on the Pi build. The switches are read
as high when closed, with the P4's internal pull-downs. Untick "Switches read high when
closed" if yours pull to ground instead.

**Calibrate** steps clockwise at 1/16 step until one switch closes, backs off, then
steps counter-clockwise to the other switch, and centres between them. That point
becomes 0 degrees. It takes about 30 s and runs in the background while the page shows
progress. Starting with a switch already closed is refused, as on the Pi.

## Using it

Browse to the address printed on the serial console (or join the `hsi-scanner` access
point, password `hyperspectral`, and browse to http://192.168.4.1/).

- **Capture** saves `hsi/IMG_nnnnn.r12` (the raw frame, 2028x1080 RAW12 packed, 3.3 MB)
  and `hsi/IMG_nnnnn.json` (exposure, gain, Bayer order, time, comment). `hsi/index.csv`
  lists every capture, like the Pi's image database.
- **Test pattern** set to colour bars checks the whole camera path without optics.
- Files can be downloaded from the page, or read straight off the card.

HTTP API: `GET /api/status`, `GET /api/capture?exposure=66666&gain=1.0&pattern=0&comment=...`,
`GET /api/files`, `GET /files/<name>`, `POST /api/time?epoch_ms=...`,
`GET /api/motor/status`, `POST /api/motor/calibrate`, `POST /api/motor/move?angle=-45`,
`POST /api/motor/sleep`.

## Reading frames on a computer

```sh
python3 tools/read_raw.py IMG_00001.json            # stats and a PNG preview
python3 tools/read_raw.py IMG_00001.json --npy      # unpacked uint16 array for numpy
python3 tools/read_raw.py IMG_00001.json --window 466 198 738 786   # Pi calibration window
```

## First bring-up checklist

1. Serial log shows `found IMX477, chip id 0x0477`. If not: cable orientation, the enable
   GPIO, and the I2C pins.
2. Capture with the colour-bar test pattern. The PNG preview should show clean vertical
   bars. Scrambled bars mean the byte order needs fixing (try `--swap-words`).
3. Capture a real scene at 66666 us and compare with a Pi capture of the same scene.
4. Motor: with the camera away from both switches, press each switch by hand and check
   `switch_a` and `switch_b` change on the page. Then run Calibrate and check it reports
   about 160 degrees between the switches.

## Layout

| Path | What it is |
| --- | --- |
| `main/` | Application: camera capture, SD storage, Wi-Fi, web server and page |
| `components/imx477/` | IMX477 sensor driver (I2C register control) |
| `components/stepper/` | A4988 stepper and limit switches, port of `motor.py` |
| `components/imx477/imx477_regs.h` | Sensor register tables, generated by `tools/gen_imx477_regs.py` |
| `tools/read_raw.py` | Unpacks captured frames into numpy arrays and PNG previews |
| `tools/gen_imx477_regs.py` | Regenerates the register tables from the Raspberry Pi Linux driver |

The register tables are values read out of the Raspberry Pi Linux `imx477` driver (GPL-2.0)
by `tools/gen_imx477_regs.py`. They are sensor settings, and no driver code is reused.
