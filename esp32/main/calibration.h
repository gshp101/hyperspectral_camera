#pragma once
// Pi calibration, loaded from /sdcard/hsi/calibration.json (made by tools/convert_calibration.py).
// Without the file, the Kconfig defaults are used.
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    bool loaded;                 // true when calibration.json was read
    char source[64];             // pickle the JSON was converted from
    uint16_t win_x0, win_y0, win_x1, win_y1;   // raw window (even numbers)
    int16_t band_top, band_bottom;             // scan band rows on the webcam image
    int16_t target_x, target_y, target_r;      // viewfinder target circle (webcam pixels)
    int16_t offset_x, offset_y;                // webcam offset added to the target
} calibration_t;

esp_err_t calibration_load(void);              // call after storage_init; never fails hard
const calibration_t *calibration_get(void);
// Sets the webcam offset so that the target lands on (col,row), like the Pi's setWebcamOffset,
// and writes it back to calibration.json.
esp_err_t calibration_set_offset_at(int col, int row);
