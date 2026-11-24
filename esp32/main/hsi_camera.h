// Hyperspectral camera (Pi HQ Camera / IMX477) capture over MIPI CSI.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "imx477.h"

typedef struct {
    uint32_t exposure_us;          // requested exposure
    float analog_gain;             // 1.0 = no gain (what the Pi scanner uses)
    imx477_test_pattern_t pattern; // IMX477_TEST_PATTERN_OFF for real images
} hsi_capture_params_t;

typedef struct {
    const uint8_t *data;           // packed CSI-2 RAW12: 2 pixels in 3 bytes
    size_t len;
    uint16_t width, height;
    uint32_t exposure_us;          // exposure actually applied
    float analog_gain;
    const char *bayer;             // e.g. "BGGR"
    int64_t capture_time_us;       // esp_timer time at end of frame
} hsi_frame_t;

esp_err_t hsi_camera_init(void);
bool hsi_camera_ready(void);
uint16_t hsi_camera_chip_id(void);
// Captures one frame with the given settings. The returned buffer stays valid
// until the next call. Not thread safe: call from one task at a time.
esp_err_t hsi_camera_capture(const hsi_capture_params_t *params, hsi_frame_t *frame);
