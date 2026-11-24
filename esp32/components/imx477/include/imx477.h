// Driver for the Sony IMX477 (Raspberry Pi HQ Camera) on the ESP32-P4.
// Talks to the sensor over I2C only; image data arrives on MIPI CSI and is
// received by the application (see main/hsi_camera.c).
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

#ifdef __cplusplus
extern "C" {
#endif

#define IMX477_I2C_ADDR         0x1A
#define IMX477_CHIP_ID          0x0477
#define IMX477_PIXEL_RATE_HZ    840000000u   // internal pixel rate, fixed for all modes
#define IMX477_LINK_FREQ_HZ     450000000u   // CSI-2 clock; lane bit rate is twice this
#define IMX477_LANE_MBPS        (IMX477_LINK_FREQ_HZ * 2 / 1000000)
#define IMX477_DATA_LANES       2
#define IMX477_BITS_PER_PIXEL   12

typedef enum {
    IMX477_MODE_2028x1080 = 0,  // 2x2 binned, cropped; the mode the Pi scanner uses
    IMX477_MODE_2028x1520,      // 2x2 binned, full field of view
    IMX477_MODE_4056x3040,      // full resolution (wider than the P4 datapath; not for streaming)
} imx477_mode_t;

typedef struct {
    uint16_t width;
    uint16_t height;
    uint16_t line_length_pix;   // H timing in internal pixel clocks
    uint16_t frame_length_min;  // V timing (lines) for the default frame rate
} imx477_mode_info_t;

typedef enum {
    IMX477_TEST_PATTERN_OFF = 0,
    IMX477_TEST_PATTERN_SOLID = 1,
    IMX477_TEST_PATTERN_COLOR_BARS = 2,
    IMX477_TEST_PATTERN_GREY_BARS = 3,
    IMX477_TEST_PATTERN_PN9 = 4,
} imx477_test_pattern_t;

typedef struct {
    i2c_master_bus_handle_t bus;   // I2C bus the camera connector is on
    uint32_t scl_speed_hz;         // 400 kHz is fine; 0 selects 400 kHz
    int enable_gpio;               // CAM_GPIO / power enable pin on the camera connector, -1 if not wired
} imx477_config_t;

typedef struct imx477_dev *imx477_handle_t;

esp_err_t imx477_new(const imx477_config_t *config, imx477_handle_t *ret);
void imx477_del(imx477_handle_t dev);

esp_err_t imx477_read_chip_id(imx477_handle_t dev, uint16_t *chip_id);

// Writes the common settings (first time only) and the mode table. Sensor stays in standby.
esp_err_t imx477_set_mode(imx477_handle_t dev, imx477_mode_t mode);
esp_err_t imx477_get_mode_info(imx477_mode_t mode, imx477_mode_info_t *info);

// Exposure in microseconds. Frame length is stretched when the exposure needs it.
// Returns the exposure actually applied (rounded to whole lines) in *applied_us if not NULL.
esp_err_t imx477_set_exposure_us(imx477_handle_t dev, uint32_t exposure_us, uint32_t *applied_us);
// Analogue gain, 1.0 to 22.26 (the Pi scanner uses 1.0).
esp_err_t imx477_set_analog_gain(imx477_handle_t dev, float gain);
esp_err_t imx477_set_orientation(imx477_handle_t dev, bool hflip, bool vflip);
esp_err_t imx477_set_test_pattern(imx477_handle_t dev, imx477_test_pattern_t pattern);
esp_err_t imx477_stream(imx477_handle_t dev, bool on);

// Bayer order of the output given the current flips: "RGGB", "GRBG", "GBRG" or "BGGR".
const char *imx477_bayer_order(imx477_handle_t dev);

// Raw register access for bring-up and debugging.
esp_err_t imx477_write_reg8(imx477_handle_t dev, uint16_t reg, uint8_t val);
esp_err_t imx477_read_reg8(imx477_handle_t dev, uint16_t reg, uint8_t *val);

#ifdef __cplusplus
}
#endif
