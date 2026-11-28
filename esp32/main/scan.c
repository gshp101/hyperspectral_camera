#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "hsi_camera.h"
#include "stepper.h"
#include "webcam.h"
#include "calibration.h"
#include "storage.h"
#include "scan.h"

static const char *TAG = "scan";

#define STEP_PERIOD_MS   10
#define LINE_MAX_PIXELS  2160
#define JPEG_MAX         (512 * 1024)

static scan_params_t s_params;
static scan_status_t s_status;
static volatile bool s_cancel;
static TaskHandle_t s_task;

const char *scan_state_name(scan_state_t st)
{
    static const char *names[] = {"idle", "preparing", "running", "finishing", "done", "failed", "cancelled"};
    return st <= SCAN_CANCELLED ? names[st] : "?";
}

bool scan_running(void)
{
    return s_task != NULL;
}

void scan_cancel(void)
{
    s_cancel = true;
}

void scan_get_status(scan_status_t *status)
{
    *status = s_status;
}

static void set_error(const char *msg)
{
    ESP_LOGE(TAG, "%s", msg);
    strlcpy(s_status.error, msg, sizeof(s_status.error));
}

// Unpacks the window from a packed CSI-2 RAW12 frame (2 pixels in 3 bytes) into uint16.
static void unpack_window(const uint8_t *packed, uint16_t frame_w, uint16_t x0, uint16_t y0,
                          uint16_t w, uint16_t h, uint16_t *out)
{
    size_t stride = (size_t)frame_w * 3 / 2;
    for (uint16_t y = 0; y < h; y++) {
        const uint8_t *p = packed + (size_t)(y0 + y) * stride + (size_t)x0 * 3 / 2;
        uint16_t *o = out + (size_t)y * w;
        for (uint16_t x = 0; x < w; x += 2, p += 3) {
            o[x] = (uint16_t)(p[0] << 4) | (p[2] & 0x0f);
            o[x + 1] = (uint16_t)(p[1] << 4) | (p[2] >> 4);
        }
    }
}

static bool write_all(FILE *f, const void *data, size_t len)
{
    return fwrite(data, 1, len, f) == len;
}

static void save_overview(const char *base, int n, uint8_t *jpeg_buf)
{
    size_t len = 0;
    if (webcam_copy_latest_jpeg(jpeg_buf, JPEG_MAX, &len) != ESP_OK) {
        return;
    }
    char path[64];
    snprintf(path, sizeof(path), STORAGE_DIR "/%s_ov%d.jpg", base, n);
    FILE *f = fopen(path, "wb");
    if (f) {
        write_all(f, jpeg_buf, len);
        fclose(f);
    }
}

static void scan_task(void *arg)
{
    const scan_params_t *p = &s_params;
    FILE *f = NULL;
    uint16_t *window = NULL;
    uint8_t *line = NULL, *jpeg = NULL;
    char base[16] = "";
    int number = 0, overviews = 0;
    bool ok = false;
    int64_t t_start = esp_timer_get_time();

    s_status.state = SCAN_PREPARING;
    if (!hsi_camera_ready() || !storage_ready()) {
        set_error("camera or SD card not ready");
        goto end;
    }
    if (!webcam_connected()) {
        set_error("no webcam connected");
        goto end;
    }
    if (!stepper_is_calibrated()) {
        ESP_LOGI(TAG, "motor not calibrated, calibrating first");
        if (stepper_calibrate(false) != ESP_OK) {
            set_error("motor calibration failed");
            goto end;
        }
    }

    // First capture tells us the frame size and applies exposure and gain.
    hsi_capture_params_t cp = {.exposure_us = p->exposure_us, .analog_gain = p->analog_gain, .pattern = IMX477_TEST_PATTERN_OFF};
    hsi_frame_t fr;
    if (hsi_camera_capture(&cp, &fr) != ESP_OK) {
        set_error("camera capture failed");
        goto end;
    }
    uint16_t x0 = p->win_x0, y0 = p->win_y0, x1 = p->win_x1, y1 = p->win_y1;
    if (x1 == 0 && y1 == 0) {
        x0 = y0 = 0;
        x1 = fr.width;
        y1 = fr.height;
    }
    if ((x0 | y0 | x1 | y1) & 1 || x1 <= x0 || y1 <= y0 || x1 > fr.width || y1 > fr.height) {
        set_error("window must be even numbers inside the frame");
        goto end;
    }
    uint16_t ww = x1 - x0, wh = y1 - y0;
    window = heap_caps_malloc((size_t)ww * wh * 2, MALLOC_CAP_SPIRAM);
    line = heap_caps_malloc(LINE_MAX_PIXELS * 3, MALLOC_CAP_SPIRAM);
    jpeg = heap_caps_malloc(JPEG_MAX, MALLOC_CAP_SPIRAM);
    if (!window || !line || !jpeg) {
        set_error("out of memory");
        goto end;
    }

    if (storage_next_name(base, sizeof(base), &number) != ESP_OK) {
        set_error("SD card write failed");
        goto end;
    }
    snprintf(base, sizeof(base), "SCAN_%05d", number);   // same counter as single frames
    snprintf(s_status.file, sizeof(s_status.file), "%s.hsi", base);
    char path[64];
    snprintf(path, sizeof(path), STORAGE_DIR "/%s.hsi", base);
    f = fopen(path, "wb");
    if (!f) {
        set_error("cannot create scan file");
        goto end;
    }
    setvbuf(f, NULL, _IOFBF, 32 * 1024);

    uint16_t wc_w = 0, wc_h = 0;
    float wc_fps = 0;
    uint32_t wc_frames = 0;
    webcam_get_info(&wc_w, &wc_h, &wc_fps, &wc_frames);
    stepper_set_fine(true);
    char comment[sizeof(p->comment)];
    size_t ci = 0;
    for (const char *c = p->comment; *c && ci < sizeof(comment) - 1; c++) {
        comment[ci++] = (*c == '"' || *c == '\\' || (unsigned char)*c < 0x20) ? ' ' : *c;
    }
    comment[ci] = 0;
    char ts[32];
    time_t now = time(NULL);
    strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", localtime(&now));
    const char *cal_src = calibration_get()->loaded ? calibration_get()->source : "";
    char cal[sizeof(calibration_get()->source)];
    size_t k = 0;
    for (const char *c = cal_src; *c && k < sizeof(cal) - 1; c++) {
        cal[k++] = (*c == '"' || *c == '\\' || (unsigned char)*c < 0x20) ? ' ' : *c;
    }
    cal[k] = 0;
    char hdr[768];
    int hl = snprintf(hdr, sizeof(hdr),
                      "{\"hsiscan\":\"esp32-1\",\"angle\":%.3f,\"exposure_us\":%lu,\"analog_gain\":%.3f,"
                      "\"sensor\":\"imx477\",\"raw_width\":%u,\"raw_height\":%u,\"bayer\":\"%s\","
                      "\"window\":{\"ul\":[%u,%u],\"lr\":[%u,%u]},\"webcam\":[%u,%u],"
                      "\"deg_per_step\":%.5f,\"time\":\"%s\",\"calibration\":\"%s\",\"comment\":\"%s\"}",
                      p->angle_deg, (unsigned long)fr.exposure_us, p->analog_gain, fr.width, fr.height, fr.bayer,
                      x0, y0, x1, y1, wc_w, wc_h, stepper_deg_per_step(), ts, cal, comment);
    uint32_t hl32 = hl;
    if (!write_all(f, "HSISCAN1", 8) || !write_all(f, &hl32, 4) || !write_all(f, hdr, hl)) {
        set_error("SD card write failed");
        goto end;
    }

    // Move to the start (+angle/2) at fine steps, then sweep counter-clockwise.
    float half = p->angle_deg / 2;
    if (stepper_rotate_to(half, STEP_PERIOD_MS) != STEPPER_OK) {
        set_error("limit switch hit while moving to the start angle");
        goto end;
    }
    s_status.frames_expected = (int)(p->angle_deg / stepper_deg_per_step()) + 1;
    s_status.state = SCAN_RUNNING;
    float pre_angle = stepper_angle();
    int64_t t_scan = esp_timer_get_time();
    int n = 0;
    while (stepper_angle() > -half) {
        if (s_cancel) {
            s_status.state = SCAN_CANCELLED;
            break;
        }
        int64_t t0 = esp_timer_get_time();
        uint16_t pixels = 0;
        if (webcam_grab_center_column(line, LINE_MAX_PIXELS * 3, &pixels, 1000) != ESP_OK) {
            set_error("webcam frame missing");
            break;
        }
        if (hsi_camera_capture(&cp, &fr) != ESP_OK) {
            set_error("camera capture failed");
            break;
        }
        float angle = stepper_angle();
        uint32_t t_ms = (uint32_t)((esp_timer_get_time() - t_scan) / 1000);
        unpack_window(fr.data, fr.width, x0, y0, ww, wh, window);
        if (n == 0) {
            save_overview(base, overviews++, jpeg);
        }
        struct __attribute__((packed)) {
            char magic[4];
            uint32_t index;
            float angle;
            uint32_t time_ms;
            uint32_t exposure_us;
            uint16_t win_w, win_h, line_pixels, reserved;
        } rec = {{'F', 'R', 'A', 'M'}, (uint32_t)n, angle, t_ms, fr.exposure_us, ww, wh, pixels, 0};
        if (!write_all(f, &rec, sizeof(rec)) || !write_all(f, line, (size_t)pixels * 3) ||
            !write_all(f, window, (size_t)ww * wh * 2)) {
            set_error("SD card write failed (card full?)");
            break;
        }
        stepper_result_t r = stepper_step(-1, STEP_PERIOD_MS);
        n++;
        s_status.frames = n;
        s_status.angle_deg = stepper_angle();
        s_status.last_step_ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        s_status.elapsed_ms = (uint32_t)((esp_timer_get_time() - t_start) / 1000);
        // Overview at the centre crossing and at the end, as on the Pi.
        if (pre_angle * stepper_angle() <= 0 || stepper_angle() <= -half) {
            save_overview(base, overviews++, jpeg);
        }
        pre_angle = stepper_angle();
        if (r != STEPPER_OK) {
            set_error(r == STEPPER_SWITCH_HIT ? "limit switch hit during scan" : "motor disabled during scan");
            break;
        }
    }
    ok = s_status.error[0] == 0 && s_status.state == SCAN_RUNNING;
    s_status.state = SCAN_FINISHING;

    char foot[256];
    int fl = snprintf(foot, sizeof(foot), "{\"success\":%s,\"nframes\":%d,\"collect_ms\":%lu,\"overview_frames\":%d,\"cancelled\":%s}",
                      ok ? "true" : "false", n, (unsigned long)((esp_timer_get_time() - t_scan) / 1000), overviews,
                      s_cancel ? "true" : "false");
    uint32_t fl32 = fl;
    write_all(f, "HSIEND01", 8);
    write_all(f, &fl32, 4);
    write_all(f, foot, fl);
    fclose(f);
    f = NULL;
    storage_index_add(number, s_status.file, p->comment[0] ? p->comment : "SCAN");
    stepper_rotate_to(0, STEP_PERIOD_MS);
    ESP_LOGI(TAG, "%s: %d frames in %lu s (%s)", s_status.file, n, (unsigned long)(s_status.elapsed_ms / 1000),
             ok ? "ok" : "incomplete");

end:
    if (f) {
        fclose(f);
    }
    free(window);
    free(line);
    free(jpeg);
    s_status.elapsed_ms = (uint32_t)((esp_timer_get_time() - t_start) / 1000);
    if (s_status.state != SCAN_CANCELLED) {
        s_status.state = ok ? SCAN_DONE : SCAN_FAILED;
    }
    s_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t scan_start(const scan_params_t *params)
{
    ESP_RETURN_ON_FALSE(!scan_running(), ESP_ERR_INVALID_STATE, TAG, "scan already running");
    ESP_RETURN_ON_FALSE(params->angle_deg > 0 && params->angle_deg <= 90, ESP_ERR_INVALID_ARG, TAG,
                        "can only scan up to 90 degrees");
    s_params = *params;
    memset(&s_status, 0, sizeof(s_status));
    s_cancel = false;
    if (xTaskCreate(scan_task, "scan", 6144, NULL, 6, &s_task) != pdPASS) {
        s_task = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
