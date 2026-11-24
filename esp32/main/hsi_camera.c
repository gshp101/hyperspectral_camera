#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "esp_cache.h"
#include "esp_heap_caps.h"
#include "esp_ldo_regulator.h"
#include "driver/i2c_master.h"
#include "driver/isp.h"
#include "esp_cam_ctlr.h"
#include "esp_cam_ctlr_csi.h"
#include "sdkconfig.h"
#include "hsi_camera.h"

static const char *TAG = "hsi_camera";

// Frames to throw away after changing exposure or gain, so the new settings
// have taken effect in the frame we keep.
#define SETTLE_FRAMES 2

static struct {
    bool ready;
    uint16_t chip_id;
    imx477_handle_t sensor;
    imx477_mode_info_t mode;
    esp_cam_ctlr_handle_t csi;
    isp_proc_handle_t isp;
    uint8_t *buf;
    size_t buf_len;
    SemaphoreHandle_t done;
    volatile int64_t done_time;
    hsi_capture_params_t current;
    uint32_t applied_exposure_us;
} s;

static bool IRAM_ATTR on_trans_finished(esp_cam_ctlr_handle_t handle, esp_cam_ctlr_trans_t *trans, void *user_data)
{
    if (trans->buffer != s.buf) {
        return false;  // a frame that landed in the driver's own backup buffer
    }
    BaseType_t woken = pdFALSE;
    s.done_time = esp_timer_get_time();
    xSemaphoreGiveFromISR(s.done, &woken);
    return woken == pdTRUE;
}

esp_err_t hsi_camera_init(void)
{
    // MIPI CSI PHY supply from the P4's internal LDO.
    esp_ldo_channel_handle_t ldo = NULL;
    esp_ldo_channel_config_t ldo_cfg = {
        .chan_id = CONFIG_HSI_MIPI_LDO_CHAN,
        .voltage_mv = CONFIG_HSI_MIPI_LDO_MV,
    };
    ESP_RETURN_ON_ERROR(esp_ldo_acquire_channel(&ldo_cfg, &ldo), TAG, "MIPI LDO");

    i2c_master_bus_handle_t bus = NULL;
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = CONFIG_HSI_CAM_I2C_PORT,
        .sda_io_num = CONFIG_HSI_CAM_I2C_SDA,
        .scl_io_num = CONFIG_HSI_CAM_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &bus), TAG, "camera I2C bus");

    imx477_config_t sensor_cfg = {
        .bus = bus,
        .scl_speed_hz = 400000,
        .enable_gpio = CONFIG_HSI_CAM_ENABLE_GPIO,
    };
    ESP_RETURN_ON_ERROR(imx477_new(&sensor_cfg, &s.sensor), TAG, "IMX477 not found; check cable, power enable and I2C pins");
    imx477_read_chip_id(s.sensor, &s.chip_id);

    // Same mode and orientation as the Pi scanner (2028x1080, rotated 180 degrees),
    // so the calibration window from the Pi still applies.
    ESP_RETURN_ON_ERROR(imx477_set_mode(s.sensor, IMX477_MODE_2028x1080), TAG, "mode");
    imx477_get_mode_info(IMX477_MODE_2028x1080, &s.mode);
    ESP_RETURN_ON_ERROR(imx477_set_orientation(s.sensor, CONFIG_HSI_CAM_ROTATE_180, CONFIG_HSI_CAM_ROTATE_180), TAG, "flip");

    esp_cam_ctlr_csi_config_t csi_cfg = {
        .ctlr_id = 0,
        .h_res = s.mode.width,
        .v_res = s.mode.height,
        .data_lane_num = IMX477_DATA_LANES,
        .lane_bit_rate_mbps = IMX477_LANE_MBPS,
        .input_data_color_type = CAM_CTLR_COLOR_RAW12,
        .output_data_color_type = CAM_CTLR_COLOR_RAW12,
        .queue_items = 1,
        .byte_swap_en = false,
        .bk_buffer_dis = false,   // frames we don't ask for go to the driver's backup buffer
    };
    ESP_RETURN_ON_ERROR(esp_cam_new_csi_ctlr(&csi_cfg, &s.csi), TAG, "CSI controller");

    // RAW data straight to memory: the ISP is bypassed but must still be set up.
    esp_isp_processor_cfg_t isp_cfg = {
        .clk_hz = 80 * 1000 * 1000,
        .input_data_source = ISP_INPUT_DATA_SOURCE_CSI,
        .input_data_color_type = ISP_COLOR_RAW12,
        .output_data_color_type = ISP_COLOR_RAW12,
        .has_line_start_packet = false,
        .has_line_end_packet = false,
        .h_res = s.mode.width,
        .v_res = s.mode.height,
        .flags.bypass_isp = true,
    };
    ESP_RETURN_ON_ERROR(esp_isp_new_processor(&isp_cfg, &s.isp), TAG, "ISP");
    ESP_RETURN_ON_ERROR(esp_isp_enable(s.isp), TAG, "ISP enable");

    s.done = xSemaphoreCreateBinary();
    esp_cam_ctlr_evt_cbs_t cbs = {
        // No on_get_new_trans: the driver then takes buffers from esp_cam_ctlr_receive()'s
        // queue, and uses its backup buffer when nothing is queued.
        .on_trans_finished = on_trans_finished,
    };
    ESP_RETURN_ON_ERROR(esp_cam_ctlr_register_event_callbacks(s.csi, &cbs, NULL), TAG, "callbacks");
    ESP_RETURN_ON_ERROR(esp_cam_ctlr_enable(s.csi), TAG, "CSI enable");

    ESP_RETURN_ON_ERROR(esp_cam_ctlr_get_frame_buffer_len(s.csi, &s.buf_len), TAG, "fb len");
    s.buf = esp_cam_ctlr_alloc_buffer(s.csi, s.buf_len, MALLOC_CAP_SPIRAM);
    ESP_RETURN_ON_FALSE(s.buf, ESP_ERR_NO_MEM, TAG, "no PSRAM for %u byte frame", (unsigned)s.buf_len);

    s.current = (hsi_capture_params_t){.exposure_us = 66666, .analog_gain = 1.0f, .pattern = IMX477_TEST_PATTERN_OFF};
    imx477_set_exposure_us(s.sensor, s.current.exposure_us, &s.applied_exposure_us);
    imx477_set_analog_gain(s.sensor, s.current.analog_gain);
    imx477_set_test_pattern(s.sensor, s.current.pattern);

    ESP_RETURN_ON_ERROR(esp_cam_ctlr_start(s.csi), TAG, "CSI start");
    ESP_RETURN_ON_ERROR(imx477_stream(s.sensor, true), TAG, "stream on");

    s.ready = true;
    ESP_LOGI(TAG, "camera ready: %ux%u RAW12, %u byte frames, bayer %s",
             s.mode.width, s.mode.height, (unsigned)s.buf_len, imx477_bayer_order(s.sensor));
    return ESP_OK;
}

bool hsi_camera_ready(void)
{
    return s.ready;
}

uint16_t hsi_camera_chip_id(void)
{
    return s.chip_id;
}

static esp_err_t grab_one(uint32_t timeout_ms)
{
    esp_cam_ctlr_trans_t trans = {.buffer = s.buf, .buflen = s.buf_len};
    xSemaphoreTake(s.done, 0);
    ESP_RETURN_ON_ERROR(esp_cam_ctlr_receive(s.csi, &trans, timeout_ms), TAG, "receive");
    if (xSemaphoreTake(s.done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

esp_err_t hsi_camera_capture(const hsi_capture_params_t *params, hsi_frame_t *frame)
{
    ESP_RETURN_ON_FALSE(s.ready, ESP_ERR_INVALID_STATE, TAG, "camera not ready");
    int settle = 0;
    if (params->exposure_us != s.current.exposure_us) {
        ESP_RETURN_ON_ERROR(imx477_set_exposure_us(s.sensor, params->exposure_us, &s.applied_exposure_us), TAG, "exposure");
        settle = SETTLE_FRAMES;
    }
    if (params->analog_gain != s.current.analog_gain) {
        ESP_RETURN_ON_ERROR(imx477_set_analog_gain(s.sensor, params->analog_gain), TAG, "gain");
        settle = SETTLE_FRAMES;
    }
    if (params->pattern != s.current.pattern) {
        ESP_RETURN_ON_ERROR(imx477_set_test_pattern(s.sensor, params->pattern), TAG, "pattern");
        settle = SETTLE_FRAMES;
    }
    s.current = *params;

    // Allow two frame times plus margin per grab.
    uint32_t timeout_ms = 2 * (s.applied_exposure_us / 1000) + 500;
    for (int i = 0; i <= settle; i++) {
        ESP_RETURN_ON_ERROR(grab_one(timeout_ms), TAG, "frame %d", i);
    }
    esp_cache_msync(s.buf, s.buf_len, ESP_CACHE_MSYNC_FLAG_DIR_M2C);

    *frame = (hsi_frame_t){
        .data = s.buf,
        .len = s.buf_len,
        .width = s.mode.width,
        .height = s.mode.height,
        .exposure_us = s.applied_exposure_us,
        .analog_gain = params->analog_gain,
        .bayer = imx477_bayer_order(s.sensor),
        .capture_time_us = s.done_time,
    };
    return ESP_OK;
}
