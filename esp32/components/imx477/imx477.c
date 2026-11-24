#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_check.h"
#include "imx477.h"
#include "imx477_regs.h"

static const char *TAG = "imx477";

// Standard (SMIA/CCS style) registers used directly by this driver.
#define REG_CHIP_ID          0x0016
#define REG_MODE_SELECT      0x0100
#define REG_ORIENTATION      0x0101
#define REG_SW_RESET         0x0103
#define REG_GROUP_HOLD       0x0104
#define REG_EXPOSURE         0x0202   // 16 bit, lines
#define REG_ANALOG_GAIN      0x0204   // 16 bit, gain = 1024 / (1024 - code)
#define REG_FRAME_LENGTH     0x0340   // 16 bit, lines
#define REG_TEST_PATTERN     0x0600   // 16 bit
#define REG_DPC_ENABLE_1     0x0b05
#define REG_DPC_ENABLE_2     0x0b06
#define REG_MC_MODE          0x3f0b
#define REG_MS_SEL           0x3041
#define REG_XVS_IO_CTRL      0x3040
#define REG_EXTOUT_EN        0x4b81

#define EXPOSURE_OFFSET      22       // exposure must be at least this many lines below frame length
#define EXPOSURE_MIN         4
#define FRAME_LENGTH_MAX     0xffdc
#define ANA_GAIN_MAX_CODE    978

struct imx477_dev {
    i2c_master_dev_handle_t i2c;
    int enable_gpio;
    bool common_written;
    imx477_mode_t mode;
    imx477_mode_info_t info;
    bool hflip, vflip;
};

static const struct {
    imx477_mode_info_t info;
    const imx477_reg_t *regs;
    size_t len;
} s_modes[] = {
    // frame_length_min gives 30 fps: 840 MHz / (line_length * 30)
    [IMX477_MODE_2028x1080] = {{2028, 1080, 0x31c4, 2198}, imx477_mode_2028x1080_regs, IMX477_MODE_2028X1080_REGS_LEN},
    [IMX477_MODE_2028x1520] = {{2028, 1520, 0x31c4, 2198}, imx477_mode_2028x1520_regs, IMX477_MODE_2028X1520_REGS_LEN},
    [IMX477_MODE_4056x3040] = {{4056, 3040, 0x5dc0, 3500}, imx477_mode_4056x3040_regs, IMX477_MODE_4056X3040_REGS_LEN},
};

esp_err_t imx477_write_reg8(imx477_handle_t dev, uint16_t reg, uint8_t val)
{
    uint8_t buf[3] = {reg >> 8, reg & 0xff, val};
    return i2c_master_transmit(dev->i2c, buf, sizeof(buf), 100);
}

static esp_err_t write_reg16(imx477_handle_t dev, uint16_t reg, uint16_t val)
{
    uint8_t buf[4] = {reg >> 8, reg & 0xff, val >> 8, val & 0xff};
    return i2c_master_transmit(dev->i2c, buf, sizeof(buf), 100);
}

esp_err_t imx477_read_reg8(imx477_handle_t dev, uint16_t reg, uint8_t *val)
{
    uint8_t addr[2] = {reg >> 8, reg & 0xff};
    return i2c_master_transmit_receive(dev->i2c, addr, 2, val, 1, 100);
}

static esp_err_t write_table(imx477_handle_t dev, const imx477_reg_t *regs, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        esp_err_t err = imx477_write_reg8(dev, regs[i].addr, regs[i].val);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "write 0x%04x failed: %s", regs[i].addr, esp_err_to_name(err));
            return err;
        }
    }
    return ESP_OK;
}

esp_err_t imx477_new(const imx477_config_t *config, imx477_handle_t *ret)
{
    ESP_RETURN_ON_FALSE(config && config->bus && ret, ESP_ERR_INVALID_ARG, TAG, "bad args");
    struct imx477_dev *dev = calloc(1, sizeof(*dev));
    ESP_RETURN_ON_FALSE(dev, ESP_ERR_NO_MEM, TAG, "no mem");
    dev->enable_gpio = config->enable_gpio;
    dev->mode = IMX477_MODE_2028x1080;
    dev->info = s_modes[dev->mode].info;

    if (dev->enable_gpio >= 0) {
        gpio_config_t io = {.pin_bit_mask = 1ULL << dev->enable_gpio, .mode = GPIO_MODE_OUTPUT};
        gpio_config(&io);
        gpio_set_level(dev->enable_gpio, 0);
        vTaskDelay(pdMS_TO_TICKS(10));
        gpio_set_level(dev->enable_gpio, 1);
        vTaskDelay(pdMS_TO_TICKS(20));  // regulators and oscillator start-up
    }

    i2c_device_config_t dcfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = IMX477_I2C_ADDR,
        .scl_speed_hz = config->scl_speed_hz ? config->scl_speed_hz : 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(config->bus, &dcfg, &dev->i2c);
    if (err != ESP_OK) {
        free(dev);
        return err;
    }

    uint16_t id = 0;
    err = imx477_read_chip_id(dev, &id);
    if (err != ESP_OK || id != IMX477_CHIP_ID) {
        ESP_LOGE(TAG, "sensor not found (err %s, id 0x%04x)", esp_err_to_name(err), id);
        imx477_del(dev);
        return err != ESP_OK ? err : ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "found IMX477, chip id 0x%04x", id);
    *ret = dev;
    return ESP_OK;
}

void imx477_del(imx477_handle_t dev)
{
    if (!dev) {
        return;
    }
    if (dev->i2c) {
        imx477_write_reg8(dev, REG_MODE_SELECT, 0);
        i2c_master_bus_rm_device(dev->i2c);
    }
    if (dev->enable_gpio >= 0) {
        gpio_set_level(dev->enable_gpio, 0);
    }
    free(dev);
}

esp_err_t imx477_read_chip_id(imx477_handle_t dev, uint16_t *chip_id)
{
    uint8_t hi = 0, lo = 0;
    ESP_RETURN_ON_ERROR(imx477_read_reg8(dev, REG_CHIP_ID, &hi), TAG, "read id");
    ESP_RETURN_ON_ERROR(imx477_read_reg8(dev, REG_CHIP_ID + 1, &lo), TAG, "read id");
    *chip_id = (hi << 8) | lo;
    return ESP_OK;
}

esp_err_t imx477_get_mode_info(imx477_mode_t mode, imx477_mode_info_t *info)
{
    ESP_RETURN_ON_FALSE(mode <= IMX477_MODE_4056x3040 && info, ESP_ERR_INVALID_ARG, TAG, "bad mode");
    *info = s_modes[mode].info;
    return ESP_OK;
}

esp_err_t imx477_set_mode(imx477_handle_t dev, imx477_mode_t mode)
{
    ESP_RETURN_ON_FALSE(mode <= IMX477_MODE_4056x3040, ESP_ERR_INVALID_ARG, TAG, "bad mode");
    ESP_RETURN_ON_ERROR(imx477_write_reg8(dev, REG_MODE_SELECT, 0), TAG, "standby");
    if (!dev->common_written) {
        ESP_RETURN_ON_ERROR(imx477_write_reg8(dev, REG_SW_RESET, 1), TAG, "reset");
        vTaskDelay(pdMS_TO_TICKS(5));
        ESP_RETURN_ON_ERROR(write_table(dev, imx477_common_regs, IMX477_COMMON_REGS_LEN), TAG, "common regs");
        ESP_RETURN_ON_ERROR(write_table(dev, imx477_link_450mhz_regs, IMX477_LINK_450MHZ_REGS_LEN), TAG, "link regs");
        dev->common_written = true;
    }
    ESP_RETURN_ON_ERROR(write_table(dev, s_modes[mode].regs, s_modes[mode].len), TAG, "mode regs");
    // On-sensor defect pixel correction off: keep raw data untouched for spectral work.
    imx477_write_reg8(dev, REG_DPC_ENABLE_1, 0);
    imx477_write_reg8(dev, REG_DPC_ENABLE_2, 0);
    // Free-running (no external vsync).
    imx477_write_reg8(dev, REG_MC_MODE, 0);
    imx477_write_reg8(dev, REG_MS_SEL, 1);
    imx477_write_reg8(dev, REG_XVS_IO_CTRL, 0);
    imx477_write_reg8(dev, REG_EXTOUT_EN, 0);
    dev->mode = mode;
    dev->info = s_modes[mode].info;
    ESP_RETURN_ON_ERROR(write_reg16(dev, REG_FRAME_LENGTH, dev->info.frame_length_min), TAG, "frame length");
    ESP_RETURN_ON_ERROR(imx477_set_orientation(dev, dev->hflip, dev->vflip), TAG, "orientation");
    ESP_LOGI(TAG, "mode %ux%u set", dev->info.width, dev->info.height);
    return ESP_OK;
}

esp_err_t imx477_set_exposure_us(imx477_handle_t dev, uint32_t exposure_us, uint32_t *applied_us)
{
    // line time = line_length_pix / pixel_rate
    uint64_t lines = ((uint64_t)exposure_us * (IMX477_PIXEL_RATE_HZ / 1000000u)) / dev->info.line_length_pix;
    if (lines < EXPOSURE_MIN) {
        lines = EXPOSURE_MIN;
    }
    if (lines > FRAME_LENGTH_MAX - EXPOSURE_OFFSET) {
        ESP_LOGW(TAG, "exposure %" PRIu32 " us too long, clamping", exposure_us);
        lines = FRAME_LENGTH_MAX - EXPOSURE_OFFSET;
    }
    uint32_t frame_length = lines + EXPOSURE_OFFSET;
    if (frame_length < dev->info.frame_length_min) {
        frame_length = dev->info.frame_length_min;
    }
    imx477_write_reg8(dev, REG_GROUP_HOLD, 1);
    esp_err_t err = write_reg16(dev, REG_FRAME_LENGTH, frame_length);
    if (err == ESP_OK) {
        err = write_reg16(dev, REG_EXPOSURE, (uint16_t)lines);
    }
    imx477_write_reg8(dev, REG_GROUP_HOLD, 0);
    if (applied_us) {
        *applied_us = (uint32_t)(lines * dev->info.line_length_pix / (IMX477_PIXEL_RATE_HZ / 1000000u));
    }
    return err;
}

esp_err_t imx477_set_analog_gain(imx477_handle_t dev, float gain)
{
    if (gain < 1.0f) {
        gain = 1.0f;
    }
    int code = (int)(1024.0f - 1024.0f / gain + 0.5f);
    if (code > ANA_GAIN_MAX_CODE) {
        code = ANA_GAIN_MAX_CODE;
    }
    return write_reg16(dev, REG_ANALOG_GAIN, (uint16_t)code);
}

esp_err_t imx477_set_orientation(imx477_handle_t dev, bool hflip, bool vflip)
{
    dev->hflip = hflip;
    dev->vflip = vflip;
    return imx477_write_reg8(dev, REG_ORIENTATION, (hflip ? 1 : 0) | (vflip ? 2 : 0));
}

esp_err_t imx477_set_test_pattern(imx477_handle_t dev, imx477_test_pattern_t pattern)
{
    return write_reg16(dev, REG_TEST_PATTERN, (uint16_t)pattern);
}

esp_err_t imx477_stream(imx477_handle_t dev, bool on)
{
    return imx477_write_reg8(dev, REG_MODE_SELECT, on ? 1 : 0);
}

const char *imx477_bayer_order(imx477_handle_t dev)
{
    // Native order is RGGB; each flip moves the 2x2 pattern by one pixel.
    static const char *orders[4] = {"RGGB", "GRBG", "GBRG", "BGGR"};
    return orders[(dev->hflip ? 1 : 0) | (dev->vflip ? 2 : 0)];
}
