#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "usb/usb_host.h"
#include "usb/uvc_host.h"
#include "driver/jpeg_decode.h"
#include "sdkconfig.h"
#include "webcam.h"

static const char *TAG = "webcam";

#define LATEST_BUF_SIZE (512 * 1024)   // generous for 640x480 MJPEG (typically 30-100 KB)

static struct {
    SemaphoreHandle_t lock;        // protects latest/latest_len/seq
    SemaphoreHandle_t new_frame;   // given on each new frame (binary, for waiters)
    uint8_t *latest;
    size_t latest_len;
    volatile uint32_t seq;
    volatile bool connected;
    uint16_t width, height;
    float fps;
    // decoding
    jpeg_decoder_handle_t jpeg;
    uint8_t *dec_in;
    size_t dec_in_size;
    uint8_t *dec_out;
    size_t dec_out_size;
    uint16_t dec_w, dec_h, dec_stride;
} s;

static bool frame_cb(const uvc_host_frame_t *frame, void *user_ctx)
{
    if (frame->data_len > 0 && frame->data_len <= LATEST_BUF_SIZE) {
        xSemaphoreTake(s.lock, portMAX_DELAY);
        memcpy(s.latest, frame->data, frame->data_len);
        s.latest_len = frame->data_len;
        s.seq++;
        xSemaphoreGive(s.lock);
        xSemaphoreGive(s.new_frame);
    }
    return true;   // frame buffer can be reused straight away
}

static void stream_cb(const uvc_host_stream_event_data_t *event, void *user_ctx)
{
    switch (event->type) {
    case UVC_HOST_DEVICE_DISCONNECTED:
        ESP_LOGW(TAG, "webcam disconnected");
        s.connected = false;
        uvc_host_stream_close(event->device_disconnected.stream_hdl);
        break;
    case UVC_HOST_TRANSFER_ERROR:
        ESP_LOGE(TAG, "USB transfer error %d", event->transfer_error.error);
        break;
    default:
        break;
    }
}

static void usb_lib_task(void *arg)
{
    while (true) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
            usb_host_device_free_all();
        }
    }
}

// Keeps (re)opening the webcam so it can be plugged in at any time.
static void webcam_task(void *arg)
{
    const uvc_host_stream_config_t cfg = {
        .event_cb = stream_cb,
        .frame_cb = frame_cb,
        .usb = {.vid = UVC_HOST_ANY_VID, .pid = UVC_HOST_ANY_PID, .uvc_stream_index = 0},
        .vs_format = {
            .h_res = CONFIG_HSI_WEBCAM_WIDTH,
            .v_res = CONFIG_HSI_WEBCAM_HEIGHT,
            .fps = CONFIG_HSI_WEBCAM_FPS,
            .format = UVC_VS_FORMAT_MJPEG,
        },
        .advanced = {
            .number_of_frame_buffers = 3,
            .frame_size = 0,                    // from the camera's negotiated maximum
            .frame_heap_caps = MALLOC_CAP_SPIRAM,
            .number_of_urbs = 4,
            .urb_size = 10 * 1024,
        },
    };
    while (true) {
        if (s.connected) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        uvc_host_stream_hdl_t stream = NULL;
        if (uvc_host_stream_open(&cfg, pdMS_TO_TICKS(2000), &stream) != ESP_OK) {
            continue;   // nothing plugged in, or it doesn't offer this MJPEG size
        }
        uvc_host_stream_format_t got = {0};
        uvc_host_stream_format_get(stream, &got);
        s.width = got.h_res;
        s.height = got.v_res;
        s.fps = got.fps;
        if (uvc_host_stream_start(stream) == ESP_OK) {
            s.connected = true;
            ESP_LOGI(TAG, "webcam streaming %ux%u MJPEG at %.1f fps", s.width, s.height, s.fps);
        } else {
            uvc_host_stream_close(stream);
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
    }
}

esp_err_t webcam_init(void)
{
    s.lock = xSemaphoreCreateMutex();
    s.new_frame = xSemaphoreCreateBinary();
    s.latest = heap_caps_malloc(LATEST_BUF_SIZE, MALLOC_CAP_SPIRAM);
    ESP_RETURN_ON_FALSE(s.latest, ESP_ERR_NO_MEM, TAG, "no PSRAM for frame");

    jpeg_decode_engine_cfg_t jcfg = {.timeout_ms = 100};
    ESP_RETURN_ON_ERROR(jpeg_new_decoder_engine(&jcfg, &s.jpeg), TAG, "JPEG decoder");
    jpeg_decode_memory_alloc_cfg_t in_cfg = {.buffer_direction = JPEG_DEC_ALLOC_INPUT_BUFFER};
    s.dec_in = jpeg_alloc_decoder_mem(LATEST_BUF_SIZE, &in_cfg, &s.dec_in_size);
    ESP_RETURN_ON_FALSE(s.dec_in, ESP_ERR_NO_MEM, TAG, "no memory for decoder input");

    usb_host_config_t host_cfg = {.intr_flags = ESP_INTR_FLAG_LOWMED};   // default = high-speed port
    ESP_RETURN_ON_ERROR(usb_host_install(&host_cfg), TAG, "USB host");
    xTaskCreate(usb_lib_task, "usb_lib", 4096, NULL, 10, NULL);

    uvc_host_driver_config_t uvc_cfg = {
        .driver_task_stack_size = 4096,
        .driver_task_priority = 11,
        .xCoreID = tskNO_AFFINITY,
        .create_background_task = true,
    };
    ESP_RETURN_ON_ERROR(uvc_host_install(&uvc_cfg), TAG, "UVC driver");
    xTaskCreate(webcam_task, "webcam", 4096, NULL, 8, NULL);
    return ESP_OK;
}

bool webcam_connected(void)
{
    return s.connected;
}

void webcam_get_info(uint16_t *width, uint16_t *height, float *fps, uint32_t *frames)
{
    *width = s.width;
    *height = s.height;
    *fps = s.fps;
    *frames = s.seq;
}

static esp_err_t wait_newer(uint32_t seq, uint32_t timeout_ms)
{
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    while (s.seq == seq) {
        TickType_t now = xTaskGetTickCount();
        if (now >= deadline || xSemaphoreTake(s.new_frame, deadline - now) != pdTRUE) {
            if (s.seq == seq) {
                return ESP_ERR_TIMEOUT;
            }
        }
    }
    return ESP_OK;
}

esp_err_t webcam_get_jpeg(uint8_t *buf, size_t buf_size, size_t *len, uint32_t *seq, uint32_t timeout_ms)
{
    ESP_RETURN_ON_ERROR(wait_newer(*seq, timeout_ms), TAG, "no new frame");
    esp_err_t err = ESP_OK;
    xSemaphoreTake(s.lock, portMAX_DELAY);
    if (s.latest_len > buf_size) {
        err = ESP_ERR_INVALID_SIZE;
    } else {
        memcpy(buf, s.latest, s.latest_len);
        *len = s.latest_len;
    }
    *seq = s.seq;
    xSemaphoreGive(s.lock);
    return err;
}

esp_err_t webcam_decode(uint32_t *seq, uint32_t timeout_ms, const uint8_t **rgb,
                        uint16_t *width, uint16_t *height, uint16_t *stride)
{
    size_t len = 0;
    ESP_RETURN_ON_ERROR(webcam_get_jpeg(s.dec_in, s.dec_in_size, &len, seq, timeout_ms), TAG, "frame");
    jpeg_decode_picture_info_t info;
    ESP_RETURN_ON_ERROR(jpeg_decoder_get_info(s.dec_in, len, &info), TAG, "JPEG header");
    // The decoder writes whole 16x16 blocks, so size the output for the padded image.
    uint32_t pw = (info.width + 15) & ~15u, ph = (info.height + 15) & ~15u;
    size_t need = pw * ph * 3;
    if (need > s.dec_out_size) {
        if (s.dec_out) {
            free(s.dec_out);
        }
        jpeg_decode_memory_alloc_cfg_t out_cfg = {.buffer_direction = JPEG_DEC_ALLOC_OUTPUT_BUFFER};
        s.dec_out = jpeg_alloc_decoder_mem(need, &out_cfg, &s.dec_out_size);
        ESP_RETURN_ON_FALSE(s.dec_out, ESP_ERR_NO_MEM, TAG, "no memory for decoded frame");
    }
    jpeg_decode_cfg_t dcfg = {
        .output_format = JPEG_DECODE_OUT_FORMAT_RGB888,
        .rgb_order = JPEG_DEC_RGB_ELEMENT_ORDER_RGB,
        .conv_std = JPEG_YUV_RGB_CONV_STD_BT601,
    };
    uint32_t out_len = 0;
    ESP_RETURN_ON_ERROR(jpeg_decoder_process(s.jpeg, &dcfg, s.dec_in, len, s.dec_out, s.dec_out_size, &out_len),
                        TAG, "decode");
    *rgb = s.dec_out;
    *width = info.width;
    *height = info.height;
    *stride = pw;
    return ESP_OK;
}

esp_err_t webcam_grab_center_column(uint8_t *line, size_t line_size, uint16_t *pixels, uint32_t timeout_ms)
{
    static uint32_t seq;
    const uint8_t *rgb;
    uint16_t w, h, stride;
    ESP_RETURN_ON_ERROR(webcam_decode(&seq, timeout_ms, &rgb, &w, &h, &stride), TAG, "decode");
    ESP_RETURN_ON_FALSE(line_size >= (size_t)h * 3, ESP_ERR_INVALID_SIZE, TAG, "line buffer too small");
    const uint8_t *p = rgb + (size_t)(w / 2) * 3;
    for (uint16_t y = 0; y < h; y++, p += (size_t)stride * 3) {
        line[y * 3 + 0] = p[0];
        line[y * 3 + 1] = p[1];
        line[y * 3 + 2] = p[2];
    }
    *pixels = h;
    return ESP_OK;
}
