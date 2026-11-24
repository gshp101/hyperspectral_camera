#include <math.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_check.h"
#include "rom/ets_sys.h"
#include "stepper.h"

static const char *TAG = "stepper";

#define FLAG_A        0x1
#define FLAG_B        0x2
#define FLAG_DISABLE  0x4

static struct {
    stepper_config_t cfg;
    SemaphoreHandle_t lock;
    volatile uint32_t flags;      // latched switch hits (set from the GPIO interrupt too)
    bool clockwise;
    bool fine;
    bool sleeping;
    bool calibrated;
    float angle;
    float deg_per_step;
    int right_switch, left_switch; // which switch (FLAG_A/FLAG_B) ends each direction
    char last_error[96];
} s;

static void IRAM_ATTR switch_isr(void *arg)
{
    s.flags |= (uint32_t)(uintptr_t)arg;
    s.calibrated = false;
}

static bool switch_closed(int gpio)
{
    return gpio_get_level(gpio) == (s.cfg.switch_active_high ? 1 : 0);
}

static void check_switches(void)
{
    if (switch_closed(s.cfg.switch_a_gpio)) {
        s.flags |= FLAG_A;
        s.calibrated = false;
    }
    if (switch_closed(s.cfg.switch_b_gpio)) {
        s.flags |= FLAG_B;
        s.calibrated = false;
    }
}

static void clear_switch_flags(void)
{
    s.flags &= ~(FLAG_A | FLAG_B);
}

static void fail(const char *msg)
{
    ESP_LOGE(TAG, "%s", msg);
    strlcpy(s.last_error, msg, sizeof(s.last_error));
    s.flags |= FLAG_DISABLE;
    s.calibrated = false;
    if (s.cfg.sleep_gpio >= 0) {
        gpio_set_level(s.cfg.sleep_gpio, 0);
    }
    s.sleeping = true;
}

static void set_direction(bool clockwise)
{
    s.clockwise = clockwise;
    gpio_set_level(s.cfg.dir_gpio, clockwise ? 0 : 1);
    ets_delay_us(5);   // A4988 needs 200 ns DIR setup before STEP
}

static void out(int gpio, int level)
{
    if (gpio >= 0) {
        gpio_set_level(gpio, level);
    }
}

esp_err_t stepper_init(const stepper_config_t *config)
{
    s.cfg = *config;
    s.lock = xSemaphoreCreateRecursiveMutex();
    uint64_t outs = (1ULL << config->step_gpio) | (1ULL << config->dir_gpio);
    if (config->sleep_gpio >= 0) {
        outs |= 1ULL << config->sleep_gpio;
    }
    if (config->microstep_gpio >= 0) {
        outs |= 1ULL << config->microstep_gpio;
    }
    gpio_config_t o = {.pin_bit_mask = outs, .mode = GPIO_MODE_OUTPUT};
    ESP_RETURN_ON_ERROR(gpio_config(&o), TAG, "output pins");
    gpio_set_level(config->step_gpio, 0);

    gpio_config_t in = {
        .pin_bit_mask = (1ULL << config->switch_a_gpio) | (1ULL << config->switch_b_gpio),
        .mode = GPIO_MODE_INPUT,
        .pull_down_en = config->switch_pull_down ? GPIO_PULLDOWN_ENABLE : GPIO_PULLDOWN_DISABLE,
        .pull_up_en = config->switch_pull_down ? GPIO_PULLUP_DISABLE : GPIO_PULLUP_ENABLE,
        .intr_type = config->switch_active_high ? GPIO_INTR_POSEDGE : GPIO_INTR_NEGEDGE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&in), TAG, "switch pins");
    esp_err_t err = gpio_install_isr_service(0);
    ESP_RETURN_ON_FALSE(err == ESP_OK || err == ESP_ERR_INVALID_STATE, err, TAG, "isr service");
    gpio_isr_handler_add(config->switch_a_gpio, switch_isr, (void *)FLAG_A);
    gpio_isr_handler_add(config->switch_b_gpio, switch_isr, (void *)FLAG_B);

    set_direction(true);
    stepper_set_fine(false);
    stepper_sleep();
    check_switches();
    ESP_LOGI(TAG, "ready (switches: a=%d b=%d)", switch_closed(config->switch_a_gpio), switch_closed(config->switch_b_gpio));
    return ESP_OK;
}

void stepper_set_fine(bool fine)
{
    s.fine = fine;
    s.deg_per_step = fine ? s.cfg.full_step_deg / 16.0f : s.cfg.full_step_deg;
    out(s.cfg.microstep_gpio, fine ? 1 : 0);
    check_switches();
}

void stepper_wake(void)
{
    s.sleeping = false;
    out(s.cfg.sleep_gpio, 1);
    vTaskDelay(pdMS_TO_TICKS(2));   // A4988 charge pump start-up (1 ms)
    check_switches();
}

void stepper_sleep(void)
{
    s.sleeping = true;
    s.calibrated = false;            // the rotor can move freely while unpowered
    out(s.cfg.sleep_gpio, 0);
    check_switches();
}

stepper_result_t stepper_step(int direction, uint32_t period_ms)
{
    xSemaphoreTakeRecursive(s.lock, portMAX_DELAY);
    stepper_result_t res = STEPPER_OK;
    if (s.flags & FLAG_DISABLE) {
        res = STEPPER_DISABLED;
    } else if (s.flags & (FLAG_A | FLAG_B)) {
        res = STEPPER_SWITCH_HIT;
    } else {
        if (s.sleeping) {
            stepper_wake();
        }
        if ((direction > 0) != s.clockwise) {
            set_direction(direction > 0);
        }
        TickType_t start = xTaskGetTickCount();
        gpio_set_level(s.cfg.step_gpio, 1);
        ets_delay_us(5);              // A4988 minimum STEP high is 1 us
        gpio_set_level(s.cfg.step_gpio, 0);
        s.angle += s.clockwise ? s.deg_per_step : -s.deg_per_step;
        TickType_t period = pdMS_TO_TICKS(period_ms);
        if (period > 0) {
            vTaskDelayUntil(&start, period);
        } else {
            ets_delay_us(500);
        }
        check_switches();
    }
    xSemaphoreGiveRecursive(s.lock);
    return res;
}

stepper_result_t stepper_rotate_to(float angle_deg, uint32_t period_ms)
{
    xSemaphoreTakeRecursive(s.lock, portMAX_DELAY);
    int dir = angle_deg > s.angle ? 1 : -1;
    float remaining = fabsf(angle_deg - s.angle);
    stepper_result_t res = STEPPER_OK;
    while (remaining > s.deg_per_step / 2) {
        res = stepper_step(dir, period_ms);
        if (res != STEPPER_OK) {
            ESP_LOGW(TAG, "rotation stopped at %.3f deg (switch or fault)", s.angle);
            break;
        }
        remaining -= s.deg_per_step;
    }
    xSemaphoreGiveRecursive(s.lock);
    return res;
}

// After hitting a switch: remember which end it is, then back off until it opens.
// Port of Motor._setSwitch().
static bool back_off_switch(void)
{
    if ((s.flags & (FLAG_A | FLAG_B)) == (FLAG_A | FLAG_B)) {
        fail("both switches closed: wiring or switch fault");
        return false;
    }
    int hit = (s.flags & FLAG_A) ? FLAG_A : FLAG_B;
    if (s.clockwise) {
        s.right_switch = hit;
    } else {
        s.left_switch = hit;
    }
    int back = s.clockwise ? -1 : 1;
    for (int i = 0; i < 100; i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
        clear_switch_flags();
        stepper_step(back, 10);
        if (!(s.flags & (FLAG_A | FLAG_B))) {
            return true;
        }
    }
    fail("could not move off a limit switch");
    return false;
}

static bool run_until_switch(int direction, int max_steps, uint32_t period_ms, int *count)
{
    *count = 0;
    while (*count < max_steps) {
        if (stepper_step(direction, period_ms) != STEPPER_OK) {
            return true;
        }
        (*count)++;
        if (s.flags & (FLAG_A | FLAG_B)) {
            return true;
        }
    }
    return false;
}

esp_err_t stepper_calibrate(bool force)
{
    xSemaphoreTakeRecursive(s.lock, portMAX_DELAY);
    esp_err_t ret = ESP_OK;
    if (s.calibrated && !force) {
        goto done;
    }
    s.last_error[0] = 0;
    s.flags &= ~FLAG_DISABLE;
    stepper_sleep();
    vTaskDelay(pdMS_TO_TICKS(1000));
    clear_switch_flags();
    check_switches();
    if (s.flags & (FLAG_A | FLAG_B)) {
        strlcpy(s.last_error, "starting on a limit switch: move the camera away from it", sizeof(s.last_error));
        ESP_LOGE(TAG, "%s", s.last_error);
        ret = ESP_ERR_INVALID_STATE;
        goto done;
    }
    stepper_wake();
    stepper_set_fine(true);
    s.right_switch = s.left_switch = 0;
    // Same pace as the Pi: at most 16 rev/s, at least 10 ms per step.
    const uint32_t period_ms = 10;
    int max_steps = (int)(s.cfg.range_deg * 1.1f / s.deg_per_step) + 1;
    int count;

    set_direction(true);
    if (!run_until_switch(1, max_steps, period_ms, &count)) {
        fail("right limit switch not found");
        ret = ESP_FAIL;
        goto done;
    }
    if (!back_off_switch()) {
        ret = ESP_FAIL;
        goto done;
    }
    set_direction(false);
    vTaskDelay(pdMS_TO_TICKS(500));
    if (!run_until_switch(-1, max_steps, period_ms, &count)) {
        fail("left limit switch not found");
        ret = ESP_FAIL;
        goto done;
    }
    if (!back_off_switch()) {
        ret = ESP_FAIL;
        goto done;
    }
    if (s.left_switch == 0 || s.right_switch == 0 || s.left_switch == s.right_switch) {
        fail("both ends hit the same switch");
        ret = ESP_FAIL;
        goto done;
    }
    set_direction(true);
    vTaskDelay(pdMS_TO_TICKS(500));
    for (int i = 0; i < count / 2; i++) {
        if (stepper_step(1, period_ms) != STEPPER_OK) {
            fail("switch closed while centring");
            ret = ESP_FAIL;
            goto done;
        }
    }
    s.angle = 0;
    s.calibrated = true;
    ESP_LOGI(TAG, "calibrated: %d steps (%.1f deg) between switches", count, count * s.deg_per_step);

done:
    xSemaphoreGiveRecursive(s.lock);
    return ret;
}

float stepper_angle(void)
{
    return s.angle;
}

float stepper_deg_per_step(void)
{
    return s.deg_per_step;
}

bool stepper_is_calibrated(void)
{
    return s.calibrated;
}

void stepper_get_status(stepper_status_t *st)
{
    *st = (stepper_status_t){
        .calibrated = s.calibrated,
        .sleeping = s.sleeping,
        .disabled = (s.flags & FLAG_DISABLE) != 0,
        .fine = s.fine,
        .switch_a = switch_closed(s.cfg.switch_a_gpio),
        .switch_b = switch_closed(s.cfg.switch_b_gpio),
        .angle_deg = s.angle,
        .deg_per_step = s.deg_per_step,
    };
    strlcpy(st->last_error, s.last_error, sizeof(st->last_error));
}
