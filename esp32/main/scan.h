// The hyperspectral scan: port of doScan() in code/CameraPi/captureServer.py.
//
// At each motor step: grab the centre column of a new webcam frame, capture one raw
// frame from the HQ Camera and keep the calibration window, write both to the SD
// card, then step. The scan runs from +angle/2 to -angle/2 at 1/16 microstepping.
//
// File layout (all little endian), SCAN_nnnnn.hsi:
//   "HSISCAN1"  uint32 json_len  header JSON
//   per step:   "FRAM" uint32 index  float32 angle_deg  uint32 time_ms  uint32 exposure_us
//               uint16 win_w  uint16 win_h  uint16 line_pixels  uint16 reserved
//               uint8 line[line_pixels*3]  (RGB888, top to bottom)
//               uint16 window[win_h*win_w] (12-bit raw values, row major)
//   "HSIEND01"  uint32 json_len  footer JSON
// Overview webcam frames (start, centre, end) are saved as SCAN_nnnnn_ovN.jpg.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    float angle_deg;          // total sweep, at most 90 (as on the Pi)
    uint32_t exposure_us;
    float analog_gain;
    uint16_t win_x0, win_y0, win_x1, win_y1;   // raw window, even numbers; all 0 = full frame
    char comment[96];
} scan_params_t;

typedef enum { SCAN_IDLE, SCAN_PREPARING, SCAN_RUNNING, SCAN_FINISHING, SCAN_DONE, SCAN_FAILED, SCAN_CANCELLED } scan_state_t;

typedef struct {
    scan_state_t state;
    int frames;
    int frames_expected;
    float angle_deg;
    uint32_t elapsed_ms;
    uint32_t last_step_ms;    // time for the last step (webcam + capture + write + step)
    char file[24];
    char error[96];
} scan_status_t;

esp_err_t scan_start(const scan_params_t *params);   // returns at once; scan runs in its own task
void scan_cancel(void);
bool scan_running(void);
void scan_get_status(scan_status_t *status);
const char *scan_state_name(scan_state_t state);
