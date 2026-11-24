#include "calibration.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"
#include "storage.h"

static const char *TAG = "calibration";
#define CAL_PATH STORAGE_DIR "/calibration.json"
#define CAL_MAX_BYTES (64 * 1024)

static calibration_t s_cal;
static cJSON *s_json;   // the whole file, kept so a save keeps the fields we do not use
static SemaphoreHandle_t s_lock;

static void set_defaults(void)
{
    memset(&s_cal, 0, sizeof(s_cal));
    s_cal.win_x0 = CONFIG_HSI_WINDOW_X0;
    s_cal.win_y0 = CONFIG_HSI_WINDOW_Y0;
    s_cal.win_x1 = CONFIG_HSI_WINDOW_X1;
    s_cal.win_y1 = CONFIG_HSI_WINDOW_Y1;
    s_cal.band_top = CONFIG_HSI_FOV_BAND_TOP;
    s_cal.band_bottom = CONFIG_HSI_FOV_BAND_BOTTOM;
    s_cal.target_x = -1;   // no target until a calibration is loaded
    s_cal.target_y = -1;
}

static bool get_pair(const cJSON *obj, const char *key, int *a, int *b)
{
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!cJSON_IsArray(arr) || cJSON_GetArraySize(arr) < 2) {
        return false;
    }
    const cJSON *x = cJSON_GetArrayItem(arr, 0), *y = cJSON_GetArrayItem(arr, 1);
    if (!cJSON_IsNumber(x) || !cJSON_IsNumber(y)) {
        return false;
    }
    *a = (int)x->valuedouble;
    *b = (int)y->valuedouble;
    return true;
}

static char *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = NULL;
    if (n > 0 && n < CAL_MAX_BYTES) {
        buf = malloc(n + 1);
        if (buf && fread(buf, 1, n, f) == (size_t)n) {
            buf[n] = 0;
            *len = n;
        } else {
            free(buf);
            buf = NULL;
        }
    }
    fclose(f);
    return buf;
}

esp_err_t calibration_load(void)
{
    if (!s_lock) {
        s_lock = xSemaphoreCreateMutex();
    }
    set_defaults();
    size_t len = 0;
    char *text = read_file(CAL_PATH, &len);
    if (!text) {
        ESP_LOGW(TAG, "no %s, using the Kconfig window and band", CAL_PATH);
        return ESP_ERR_NOT_FOUND;
    }
    cJSON *root = cJSON_ParseWithLength(text, len);
    free(text);
    if (!root) {
        ESP_LOGE(TAG, "%s is not valid JSON, using defaults", CAL_PATH);
        return ESP_ERR_INVALID_ARG;
    }
    int a, b, c, d;
    const cJSON *win = cJSON_GetObjectItemCaseSensitive(root, "window");
    if (get_pair(win, "ul", &a, &b) && get_pair(win, "lr", &c, &d) && a >= 0 && b >= 0 && c > a && d > b) {
        s_cal.win_x0 = a & ~1;
        s_cal.win_y0 = b & ~1;
        s_cal.win_x1 = (c + 1) & ~1;
        s_cal.win_y1 = (d + 1) & ~1;
    }
    if (get_pair(root, "scan_band", &a, &b) && b > a) {
        s_cal.band_top = a;
        s_cal.band_bottom = b;
    }
    const cJSON *target = cJSON_GetObjectItemCaseSensitive(root, "target");
    if (get_pair(target, "center", &a, &b)) {
        s_cal.target_x = a;
        s_cal.target_y = b;
        const cJSON *r = cJSON_GetObjectItemCaseSensitive(target, "radius");
        s_cal.target_r = cJSON_IsNumber(r) ? (int)r->valuedouble : 4;
    }
    if (get_pair(root, "webcam_offset", &a, &b)) {
        s_cal.offset_x = a;
        s_cal.offset_y = b;
    }
    const cJSON *src = cJSON_GetObjectItemCaseSensitive(root, "source");
    if (cJSON_IsString(src)) {
        strlcpy(s_cal.source, src->valuestring, sizeof(s_cal.source));
    }
    s_cal.loaded = true;
    s_json = root;
    ESP_LOGI(TAG, "loaded %s: window %u,%u-%u,%u band %d-%d target %d,%d offset %d,%d", CAL_PATH,
             s_cal.win_x0, s_cal.win_y0, s_cal.win_x1, s_cal.win_y1, s_cal.band_top, s_cal.band_bottom,
             s_cal.target_x, s_cal.target_y, s_cal.offset_x, s_cal.offset_y);
    return ESP_OK;
}

const calibration_t *calibration_get(void)
{
    return &s_cal;
}

esp_err_t calibration_set_offset_at(int col, int row)
{
    if (!s_cal.loaded || s_cal.target_x < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_cal.offset_x = col - s_cal.target_x;
    s_cal.offset_y = row - s_cal.target_y;
    cJSON *arr = cJSON_CreateIntArray((const int[]){s_cal.offset_x, s_cal.offset_y}, 2);
    if (cJSON_GetObjectItemCaseSensitive(s_json, "webcam_offset")) {
        cJSON_ReplaceItemInObjectCaseSensitive(s_json, "webcam_offset", arr);
    } else {
        cJSON_AddItemToObject(s_json, "webcam_offset", arr);
    }
    char *text = cJSON_Print(s_json);
    esp_err_t err = ESP_FAIL;
    if (text) {
        // Write a temporary file first so a power cut cannot leave a half-written calibration.
        FILE *f = fopen(CAL_PATH ".tmp", "wb");
        if (f) {
            size_t n = strlen(text);
            bool ok = fwrite(text, 1, n, f) == n;
            ok = (fclose(f) == 0) && ok;
            if (ok) {
                remove(CAL_PATH);
                err = rename(CAL_PATH ".tmp", CAL_PATH) == 0 ? ESP_OK : ESP_FAIL;
            }
        }
        cJSON_free(text);
    }
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "webcam offset set to %d,%d (%s)", s_cal.offset_x, s_cal.offset_y, esp_err_to_name(err));
    return err;
}
