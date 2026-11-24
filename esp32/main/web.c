#include <ctype.h>
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "hsi_camera.h"
#include "stepper.h"
#include "webcam.h"
#include "scan.h"
#include "esp_heap_caps.h"
#include "storage.h"
#include "net.h"
#include "web.h"

static const char *TAG = "web";
static SemaphoreHandle_t s_capture_lock;

extern const char index_html_start[] asm("_binary_index_html_start");
extern const char index_html_end[] asm("_binary_index_html_end");

static void url_decode(char *s)
{
    char *o = s;
    for (; *s; s++) {
        if (*s == '+') {
            *o++ = ' ';
        } else if (*s == '%' && isxdigit((unsigned char)s[1]) && isxdigit((unsigned char)s[2])) {
            char hex[3] = {s[1], s[2], 0};
            *o++ = (char)strtol(hex, NULL, 16);
            s += 2;
        } else {
            *o++ = *s;
        }
    }
    *o = 0;
}

static bool query_param(httpd_req_t *req, const char *key, char *val, size_t len)
{
    char q[256];
    if (httpd_req_get_url_query_str(req, q, sizeof(q)) != ESP_OK) {
        return false;
    }
    if (httpd_query_key_value(q, key, val, len) != ESP_OK) {
        return false;
    }
    url_decode(val);
    return true;
}

static void json_escape(const char *in, char *out, size_t len)
{
    size_t o = 0;
    for (; *in && o + 7 < len; in++) {
        unsigned char c = (unsigned char)*in;
        if (c == '"' || c == '\\') {
            out[o++] = '\\';
            out[o++] = c;
        } else if (c < 0x20) {
            o += snprintf(out + o, len - o, "\\u%04x", c);
        } else {
            out[o++] = c;
        }
    }
    out[o] = 0;
}

static esp_err_t send_json(httpd_req_t *req, const char *status, const char *body)
{
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, body);
}

static esp_err_t index_get(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, index_html_start, index_html_end - index_html_start);
}

static esp_err_t status_get(httpd_req_t *req)
{
    uint64_t total = 0, free_b = 0;
    if (storage_ready()) {
        storage_space(&total, &free_b);
    }
    char ts[32];
    time_t now = time(NULL);
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", localtime(&now));
    char body[320];
    snprintf(body, sizeof(body),
             "{\"camera\":%s,\"chip_id\":\"0x%04x\",\"sd\":%s,\"sd_total_mb\":%llu,\"sd_free_mb\":%llu,"
             "\"wifi\":\"%s\",\"ip\":\"%s\",\"time\":\"%s\",\"uptime_s\":%lld}",
             hsi_camera_ready() ? "true" : "false", hsi_camera_chip_id(), storage_ready() ? "true" : "false",
             total >> 20, free_b >> 20, net_is_station() ? "station" : "access point", net_ip(), ts,
             esp_timer_get_time() / 1000000);
    return send_json(req, "200 OK", body);
}

// Lets the browser set the clock when there is no internet for SNTP (access point mode).
static esp_err_t time_post(httpd_req_t *req)
{
    char v[24];
    if (!query_param(req, "epoch_ms", v, sizeof(v))) {
        return send_json(req, "400 Bad Request", "{\"error\":\"epoch_ms missing\"}");
    }
    long long ms = atoll(v);
    struct timeval tv = {.tv_sec = ms / 1000, .tv_usec = (ms % 1000) * 1000};
    settimeofday(&tv, NULL);
    return send_json(req, "200 OK", "{\"ok\":true}");
}

static esp_err_t write_file(const char *path, const void *data, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (!f) {
        return ESP_FAIL;
    }
    size_t n = fwrite(data, 1, len, f);
    fclose(f);
    return n == len ? ESP_OK : ESP_FAIL;
}

// GET /api/capture?exposure=66666&gain=1.0&pattern=0&comment=...
// Captures one raw frame and saves IMG_nnnnn.r12 (packed RAW12) and IMG_nnnnn.json.
static esp_err_t capture_get(httpd_req_t *req)
{
    if (!hsi_camera_ready() || !storage_ready()) {
        return send_json(req, "503 Service Unavailable", "{\"error\":\"camera or SD card not ready\"}");
    }
    if (scan_running()) {
        return send_json(req, "409 Conflict", "{\"error\":\"a scan is running\"}");
    }
    char v[128];
    hsi_capture_params_t p = {.exposure_us = 66666, .analog_gain = 1.0f, .pattern = IMX477_TEST_PATTERN_OFF};
    if (query_param(req, "exposure", v, sizeof(v))) {
        long e = atol(v);
        if (e >= 100 && e <= 1000000) {
            p.exposure_us = e;
        }
    }
    if (query_param(req, "gain", v, sizeof(v))) {
        float g = strtof(v, NULL);
        if (g >= 1.0f && g <= 22.0f) {
            p.analog_gain = g;
        }
    }
    if (query_param(req, "pattern", v, sizeof(v))) {
        int t = atoi(v);
        if (t >= 0 && t <= 4) {
            p.pattern = t;
        }
    }
    char comment[128] = "";
    query_param(req, "comment", comment, sizeof(comment));

    if (xSemaphoreTake(s_capture_lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        return send_json(req, "409 Conflict", "{\"error\":\"capture already running\"}");
    }
    int64_t t0 = esp_timer_get_time();
    hsi_frame_t fr;
    esp_err_t err = hsi_camera_capture(&p, &fr);
    int64_t t1 = esp_timer_get_time();
    char name[16], path[64], body[512], esc[256];
    int number = 0;
    if (err == ESP_OK) {
        err = storage_next_name(name, sizeof(name), &number);
    }
    if (err == ESP_OK) {
        snprintf(path, sizeof(path), STORAGE_DIR "/%s.r12", name);
        err = write_file(path, fr.data, fr.len);
    }
    int64_t t2 = esp_timer_get_time();
    if (err == ESP_OK) {
        char ts[32];
        time_t now = time(NULL);
        strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", localtime(&now));
        json_escape(comment, esc, sizeof(esc));
        int n = snprintf(body, sizeof(body),
                         "{\"file\":\"%s.r12\",\"format\":\"csi2_raw12_packed\",\"width\":%u,\"height\":%u,"
                         "\"bayer\":\"%s\",\"exposure_us\":%lu,\"analog_gain\":%.3f,\"test_pattern\":%d,"
                         "\"sensor\":\"imx477\",\"mode\":\"2028x1080\",\"time\":\"%s\",\"comment\":\"%s\"}",
                         name, fr.width, fr.height, fr.bayer, (unsigned long)fr.exposure_us, fr.analog_gain,
                         (int)p.pattern, ts, esc);
        snprintf(path, sizeof(path), STORAGE_DIR "/%s.json", name);
        err = write_file(path, body, n);
        snprintf(path, sizeof(path), "%s.r12", name);
        storage_index_add(number, path, comment);
    }
    xSemaphoreGive(s_capture_lock);
    if (err != ESP_OK) {
        snprintf(body, sizeof(body), "{\"error\":\"%s\"}", esp_err_to_name(err));
        return send_json(req, "500 Internal Server Error", body);
    }
    ESP_LOGI(TAG, "%s: capture %lld ms, SD write %lld ms", name, (t1 - t0) / 1000, (t2 - t1) / 1000);
    snprintf(body, sizeof(body), "{\"name\":\"%s\",\"capture_ms\":%lld,\"write_ms\":%lld,\"bytes\":%u}",
             name, (t1 - t0) / 1000, (t2 - t1) / 1000, (unsigned)fr.len);
    return send_json(req, "200 OK", body);
}

static esp_err_t files_get(httpd_req_t *req)
{
    httpd_resp_set_type(req, "application/json");
    DIR *d = storage_ready() ? opendir(STORAGE_DIR) : NULL;
    httpd_resp_sendstr_chunk(req, "[");
    if (d) {
        struct dirent *e;
        bool first = true;
        char line[320], path[300];
        while ((e = readdir(d)) != NULL) {
            if (e->d_name[0] == '.') {
                continue;
            }
            struct stat st;
            snprintf(path, sizeof(path), STORAGE_DIR "/%s", e->d_name);
            if (stat(path, &st) != 0 || !S_ISREG(st.st_mode)) {
                continue;
            }
            snprintf(line, sizeof(line), "%s{\"name\":\"%s\",\"bytes\":%ld}", first ? "" : ",", e->d_name, (long)st.st_size);
            httpd_resp_sendstr_chunk(req, line);
            first = false;
        }
        closedir(d);
    }
    httpd_resp_sendstr_chunk(req, "]");
    return httpd_resp_sendstr_chunk(req, NULL);
}

// GET /files/<name> streams a file from the card.
static esp_err_t file_get(httpd_req_t *req)
{
    const char *name = req->uri + strlen("/files/");
    char clean[64];
    size_t i = 0;
    for (; name[i] && name[i] != '?' && i < sizeof(clean) - 1; i++) {
        char c = name[i];
        if (!(isalnum((unsigned char)c) || c == '_' || c == '-' || c == '.')) {
            return send_json(req, "400 Bad Request", "{\"error\":\"bad file name\"}");
        }
        clean[i] = c;
    }
    clean[i] = 0;
    if (i == 0 || strstr(clean, "..")) {
        return send_json(req, "400 Bad Request", "{\"error\":\"bad file name\"}");
    }
    char path[96];
    snprintf(path, sizeof(path), STORAGE_DIR "/%s", clean);
    FILE *f = fopen(path, "rb");
    if (!f) {
        return send_json(req, "404 Not Found", "{\"error\":\"no such file\"}");
    }
    httpd_resp_set_type(req, strstr(clean, ".json") ? "application/json" : "application/octet-stream");
    char disp[96];
    snprintf(disp, sizeof(disp), "attachment; filename=\"%s\"", clean);
    httpd_resp_set_hdr(req, "Content-Disposition", disp);
    const size_t chunk = 16 * 1024;
    char *buf = malloc(chunk);
    esp_err_t err = buf ? ESP_OK : ESP_ERR_NO_MEM;
    size_t n;
    while (err == ESP_OK && (n = fread(buf, 1, chunk, f)) > 0) {
        err = httpd_resp_send_chunk(req, buf, n);
    }
    free(buf);
    fclose(f);
    if (err == ESP_OK) {
        err = httpd_resp_send_chunk(req, NULL, 0);
    }
    return err;
}

// ---- Scan motor ----
// Motor jobs (calibrate takes ~30 s) run in their own task so the web page stays live.

typedef enum { JOB_NONE, JOB_CALIBRATE, JOB_MOVE } motor_job_t;
static volatile motor_job_t s_motor_job;
static volatile float s_motor_target;
static char s_motor_result[96] = "";

static void motor_task(void *arg)
{
    motor_job_t job = (motor_job_t)(uintptr_t)arg;
    if (job == JOB_CALIBRATE) {
        esp_err_t err = stepper_calibrate(true);
        snprintf(s_motor_result, sizeof(s_motor_result), "calibrate: %s", err == ESP_OK ? "ok" : "failed");
    } else if (job == JOB_MOVE) {
        stepper_set_fine(true);
        stepper_result_t r = stepper_rotate_to(s_motor_target, 10);
        snprintf(s_motor_result, sizeof(s_motor_result), "move to %.2f: %s", s_motor_target,
                 r == STEPPER_OK ? "ok" : r == STEPPER_SWITCH_HIT ? "stopped by limit switch" : "motor disabled");
    }
    s_motor_job = JOB_NONE;
    vTaskDelete(NULL);
}

static bool start_motor_job(motor_job_t job)
{
    if (s_motor_job != JOB_NONE || scan_running()) {
        return false;
    }
    s_motor_job = job;
    if (xTaskCreate(motor_task, "motor", 4096, (void *)(uintptr_t)job, 5, NULL) != pdPASS) {
        s_motor_job = JOB_NONE;
        return false;
    }
    return true;
}

static esp_err_t motor_status_get(httpd_req_t *req)
{
    stepper_status_t st;
    stepper_get_status(&st);
    char err[128], res[128], body[512];
    json_escape(st.last_error, err, sizeof(err));
    json_escape(s_motor_result, res, sizeof(res));
    const char *busy = s_motor_job == JOB_CALIBRATE ? "calibrating" : s_motor_job == JOB_MOVE ? "moving" : "idle";
    snprintf(body, sizeof(body),
             "{\"state\":\"%s\",\"calibrated\":%s,\"angle_deg\":%.4f,\"deg_per_step\":%.4f,\"sleeping\":%s,"
             "\"disabled\":%s,\"switch_a\":%s,\"switch_b\":%s,\"last_error\":\"%s\",\"last_result\":\"%s\"}",
             busy, st.calibrated ? "true" : "false", st.angle_deg, st.deg_per_step, st.sleeping ? "true" : "false",
             st.disabled ? "true" : "false", st.switch_a ? "true" : "false", st.switch_b ? "true" : "false", err, res);
    return send_json(req, "200 OK", body);
}

static esp_err_t motor_calibrate_post(httpd_req_t *req)
{
    if (!start_motor_job(JOB_CALIBRATE)) {
        return send_json(req, "409 Conflict", "{\"error\":\"motor busy\"}");
    }
    return send_json(req, "202 Accepted", "{\"started\":\"calibrate\"}");
}

// POST /api/motor/move?angle=-45  (absolute degrees, 0 = centre)
static esp_err_t motor_move_post(httpd_req_t *req)
{
    char v[24];
    if (!query_param(req, "angle", v, sizeof(v))) {
        return send_json(req, "400 Bad Request", "{\"error\":\"angle missing\"}");
    }
    float a = strtof(v, NULL);
    if (a < -90.0f || a > 90.0f) {
        return send_json(req, "400 Bad Request", "{\"error\":\"angle must be within +/-90\"}");
    }
    s_motor_target = a;
    if (!start_motor_job(JOB_MOVE)) {
        return send_json(req, "409 Conflict", "{\"error\":\"motor busy\"}");
    }
    return send_json(req, "202 Accepted", "{\"started\":\"move\"}");
}

static esp_err_t motor_sleep_post(httpd_req_t *req)
{
    if (s_motor_job != JOB_NONE || scan_running()) {
        return send_json(req, "409 Conflict", "{\"error\":\"motor busy\"}");
    }
    stepper_sleep();
    return send_json(req, "200 OK", "{\"ok\":true}");
}

// ---- USB webcam ----

static esp_err_t webcam_status_get(httpd_req_t *req)
{
    uint16_t w, h;
    float fps;
    uint32_t frames;
    webcam_get_info(&w, &h, &fps, &frames);
    char body[256];
    snprintf(body, sizeof(body),
             "{\"connected\":%s,\"width\":%u,\"height\":%u,\"fps\":%.1f,\"frames\":%lu,"
             "\"stream_port\":%d,\"band_top\":%d,\"band_bottom\":%d}",
             webcam_connected() ? "true" : "false", w, h, fps, (unsigned long)frames,
             CONFIG_HSI_HTTP_PORT + 1, CONFIG_HSI_FOV_BAND_TOP, CONFIG_HSI_FOV_BAND_BOTTOM);
    return send_json(req, "200 OK", body);
}

// Bring-up check of the scan path: decode one frame in hardware and take its centre column.
static esp_err_t webcam_test_get(httpd_req_t *req)
{
    if (!webcam_connected()) {
        return send_json(req, "503 Service Unavailable", "{\"error\":\"no webcam\"}");
    }
    static uint8_t line[3 * 2160];
    uint16_t n = 0;
    int64_t t0 = esp_timer_get_time();
    esp_err_t err = webcam_grab_center_column(line, sizeof(line), &n, 1000);
    int64_t t1 = esp_timer_get_time();
    if (err != ESP_OK) {
        char body[96];
        snprintf(body, sizeof(body), "{\"error\":\"%s\"}", esp_err_to_name(err));
        return send_json(req, "500 Internal Server Error", body);
    }
    unsigned long sum[3] = {0};
    for (int i = 0; i < n; i++) {
        for (int c = 0; c < 3; c++) {
            sum[c] += line[i * 3 + c];
        }
    }
    char body[160];
    snprintf(body, sizeof(body), "{\"pixels\":%u,\"grab_decode_ms\":%lld,\"mean_rgb\":[%lu,%lu,%lu]}",
             n, (t1 - t0) / 1000, n ? sum[0] / n : 0, n ? sum[1] / n : 0, n ? sum[2] / n : 0);
    return send_json(req, "200 OK", body);
}

// MJPEG viewfinder. Runs on its own server (port + 1) because a stream never
// returns, and esp_http_server handles one request at a time.
#define STREAM_BOUNDARY "hsiframe"
static esp_err_t webcam_stream_get(httpd_req_t *req)
{
    const size_t cap = 512 * 1024;
    uint8_t *buf = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
    if (!buf) {
        return httpd_resp_send_500(req);
    }
    httpd_resp_set_type(req, "multipart/x-mixed-replace; boundary=" STREAM_BOUNDARY);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    uint32_t seq = 0;
    esp_err_t err = ESP_OK;
    while (err == ESP_OK) {
        size_t len = 0;
        if (webcam_get_jpeg(buf, cap, &len, &seq, 2000) != ESP_OK) {
            if (!webcam_connected()) {
                vTaskDelay(pdMS_TO_TICKS(500));
            }
            continue;
        }
        char head[96];
        int n = snprintf(head, sizeof(head), "\r\n--" STREAM_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                         (unsigned)len);
        err = httpd_resp_send_chunk(req, head, n);
        if (err == ESP_OK) {
            err = httpd_resp_send_chunk(req, (const char *)buf, len);
        }
    }
    free(buf);
    return ESP_OK;   // client went away
}

static esp_err_t start_stream_server(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = CONFIG_HSI_HTTP_PORT + 1;
    cfg.ctrl_port = cfg.ctrl_port + 1;
    cfg.max_uri_handlers = 2;
    httpd_handle_t server = NULL;
    esp_err_t err = httpd_start(&server, &cfg);
    if (err != ESP_OK) {
        return err;
    }
    httpd_uri_t u = {.uri = "/webcam", .method = HTTP_GET, .handler = webcam_stream_get};
    return httpd_register_uri_handler(server, &u);
}

// ---- Scan ----

// POST /api/scan/start?angle=90&exposure=66666&gain=1&comment=...&window=466,198,738,786
static esp_err_t scan_start_post(httpd_req_t *req)
{
    if (s_motor_job != JOB_NONE) {
        return send_json(req, "409 Conflict", "{\"error\":\"motor busy\"}");
    }
    scan_params_t p = {
        .angle_deg = CONFIG_HSI_SCAN_ANGLE_DEG,
        .exposure_us = 66666,
        .analog_gain = 1.0f,
        .win_x0 = CONFIG_HSI_WINDOW_X0,
        .win_y0 = CONFIG_HSI_WINDOW_Y0,
        .win_x1 = CONFIG_HSI_WINDOW_X1,
        .win_y1 = CONFIG_HSI_WINDOW_Y1,
    };
    char v[96];
    if (query_param(req, "angle", v, sizeof(v))) {
        p.angle_deg = strtof(v, NULL);
    }
    if (query_param(req, "exposure", v, sizeof(v))) {
        long e = atol(v);
        if (e >= 100 && e <= 1000000) {
            p.exposure_us = e;
        }
    }
    if (query_param(req, "gain", v, sizeof(v))) {
        float g = strtof(v, NULL);
        if (g >= 1.0f && g <= 22.0f) {
            p.analog_gain = g;
        }
    }
    if (query_param(req, "window", v, sizeof(v))) {
        unsigned a, b, c, d;
        if (sscanf(v, "%u,%u,%u,%u", &a, &b, &c, &d) == 4) {
            p.win_x0 = a;
            p.win_y0 = b;
            p.win_x1 = c;
            p.win_y1 = d;
        }
    }
    query_param(req, "comment", p.comment, sizeof(p.comment));
    esp_err_t err = scan_start(&p);
    if (err == ESP_ERR_INVALID_STATE) {
        return send_json(req, "409 Conflict", "{\"error\":\"scan already running\"}");
    }
    if (err == ESP_ERR_INVALID_ARG) {
        return send_json(req, "400 Bad Request", "{\"error\":\"angle must be between 0 and 90\"}");
    }
    if (err != ESP_OK) {
        return send_json(req, "500 Internal Server Error", "{\"error\":\"could not start scan\"}");
    }
    return send_json(req, "202 Accepted", "{\"started\":\"scan\"}");
}

static esp_err_t scan_status_get(httpd_req_t *req)
{
    scan_status_t st;
    scan_get_status(&st);
    char err[128], body[384];
    json_escape(st.error, err, sizeof(err));
    snprintf(body, sizeof(body),
             "{\"state\":\"%s\",\"frames\":%d,\"frames_expected\":%d,\"angle_deg\":%.3f,\"elapsed_s\":%.1f,"
             "\"last_step_ms\":%lu,\"file\":\"%s\",\"error\":\"%s\"}",
             scan_state_name(st.state), st.frames, st.frames_expected, st.angle_deg, st.elapsed_ms / 1000.0,
             (unsigned long)st.last_step_ms, st.file, err);
    return send_json(req, "200 OK", body);
}

static esp_err_t scan_cancel_post(httpd_req_t *req)
{
    scan_cancel();
    return send_json(req, "200 OK", "{\"ok\":true}");
}

esp_err_t web_start(void)
{
    s_capture_lock = xSemaphoreCreateMutex();
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = CONFIG_HSI_HTTP_PORT;
    cfg.uri_match_fn = httpd_uri_match_wildcard;
    cfg.stack_size = 8192;
    cfg.max_uri_handlers = 24;
    httpd_handle_t server = NULL;
    esp_err_t err = httpd_start(&server, &cfg);
    if (err != ESP_OK) {
        return err;
    }
    const httpd_uri_t uris[] = {
        {.uri = "/", .method = HTTP_GET, .handler = index_get},
        {.uri = "/api/status", .method = HTTP_GET, .handler = status_get},
        {.uri = "/api/time", .method = HTTP_POST, .handler = time_post},
        {.uri = "/api/capture", .method = HTTP_GET, .handler = capture_get},
        {.uri = "/api/files", .method = HTTP_GET, .handler = files_get},
        {.uri = "/files/*", .method = HTTP_GET, .handler = file_get},
        {.uri = "/api/motor/status", .method = HTTP_GET, .handler = motor_status_get},
        {.uri = "/api/motor/calibrate", .method = HTTP_POST, .handler = motor_calibrate_post},
        {.uri = "/api/motor/move", .method = HTTP_POST, .handler = motor_move_post},
        {.uri = "/api/motor/sleep", .method = HTTP_POST, .handler = motor_sleep_post},
        {.uri = "/api/webcam/status", .method = HTTP_GET, .handler = webcam_status_get},
        {.uri = "/api/webcam/test", .method = HTTP_GET, .handler = webcam_test_get},
        {.uri = "/api/scan/start", .method = HTTP_POST, .handler = scan_start_post},
        {.uri = "/api/scan/status", .method = HTTP_GET, .handler = scan_status_get},
        {.uri = "/api/scan/cancel", .method = HTTP_POST, .handler = scan_cancel_post},
    };
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        httpd_register_uri_handler(server, &uris[i]);
    }
    if (start_stream_server() != ESP_OK) {
        ESP_LOGE(TAG, "viewfinder stream server failed to start");
    }
    ESP_LOGI(TAG, "web server on port %d, viewfinder on port %d", CONFIG_HSI_HTTP_PORT, CONFIG_HSI_HTTP_PORT + 1);
    return ESP_OK;
}
