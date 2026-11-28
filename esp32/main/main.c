// ESP32-P4 hyperspectral scanner: Wi-Fi, SD card, web page, raw frame capture
// from the Raspberry Pi HQ Camera (IMX477), the scan motor and the USB webcam.
#include "esp_log.h"
#include "nvs_flash.h"
#include "hsi_camera.h"
#include "stepper.h"
#include "webcam.h"
#include "sdkconfig.h"
#include "storage.h"
#include "calibration.h"
#include "net.h"
#include "web.h"

static const char *TAG = "main";

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    // Each part is optional at start-up so the web page can report what is missing.
    if (storage_init() != ESP_OK) {
        ESP_LOGE(TAG, "SD card not available");
    }
    calibration_load();   // falls back to the Kconfig window and band without calibration.json
    if (hsi_camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "camera not available");
    }
    stepper_config_t motor = {
        .step_gpio = CONFIG_HSI_MOTOR_STEP_GPIO,
        .dir_gpio = CONFIG_HSI_MOTOR_DIR_GPIO,
        .sleep_gpio = CONFIG_HSI_MOTOR_SLEEP_GPIO,
        .microstep_gpio = CONFIG_HSI_MOTOR_MICROSTEP_GPIO,
        .switch_a_gpio = CONFIG_HSI_MOTOR_SWITCH_A_GPIO,
        .switch_b_gpio = CONFIG_HSI_MOTOR_SWITCH_B_GPIO,
        .switch_active_high = CONFIG_HSI_MOTOR_SWITCH_ACTIVE_HIGH,
        .switch_pull_down = CONFIG_HSI_MOTOR_SWITCH_ACTIVE_HIGH,
        .range_deg = CONFIG_HSI_MOTOR_RANGE_DEG,
        .full_step_deg = 1.8f,
    };
    if (stepper_init(&motor) != ESP_OK) {
        ESP_LOGE(TAG, "motor not available");
    }
    if (webcam_init() != ESP_OK) {
        ESP_LOGE(TAG, "webcam (USB host) not available");
    }
    ESP_ERROR_CHECK(net_init());
    ESP_ERROR_CHECK(web_start());
    ESP_LOGI(TAG, "ready at http://%s/", net_ip());
}
