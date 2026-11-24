// ESP32-P4 hyperspectral scanner, milestone 1: Wi-Fi, SD card, web page and
// single raw frame capture from the Raspberry Pi HQ Camera (IMX477).
#include "esp_log.h"
#include "nvs_flash.h"
#include "hsi_camera.h"
#include "storage.h"
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
    if (hsi_camera_init() != ESP_OK) {
        ESP_LOGE(TAG, "camera not available");
    }
    ESP_ERROR_CHECK(net_init());
    ESP_ERROR_CHECK(web_start());
    ESP_LOGI(TAG, "ready at http://%s/", net_ip());
}
