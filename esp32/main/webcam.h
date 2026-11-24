// USB webcam (UVC, MJPEG) on the ESP32-P4's high-speed USB host.
// Used as the viewfinder when idle, and for one RGB line per step during a scan.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t webcam_init(void);
bool webcam_connected(void);
void webcam_get_info(uint16_t *width, uint16_t *height, float *fps, uint32_t *frames);

// Copies the newest JPEG frame newer than *seq into buf (waits up to timeout_ms).
// Updates *seq and *len. Returns ESP_ERR_TIMEOUT if no new frame arrived,
// ESP_ERR_INVALID_SIZE if buf is too small.
esp_err_t webcam_get_jpeg(uint8_t *buf, size_t buf_size, size_t *len, uint32_t *seq, uint32_t timeout_ms);

// Waits for a frame newer than *seq, decodes it with the hardware JPEG decoder and
// returns the RGB888 image (valid until the next call). Width and height are the
// camera's; the buffer's row stride is *stride pixels.
esp_err_t webcam_decode(uint32_t *seq, uint32_t timeout_ms, const uint8_t **rgb,
                        uint16_t *width, uint16_t *height, uint16_t *stride);

// Like the Pi scan loop: the centre column of a new frame, top to bottom, as RGB888.
// line must hold 3 * height bytes. Writes the number of pixels to *pixels.
esp_err_t webcam_grab_center_column(uint8_t *line, size_t line_size, uint16_t *pixels, uint32_t timeout_ms);
