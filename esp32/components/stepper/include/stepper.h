// Scan motor: a stepper on an A4988 driver with a limit switch at each end of travel.
// Port of code/CameraPi/motor.py. Angles are in degrees, clockwise positive,
// 0 = centre between the two switches (after calibration).
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int step_gpio;          // A4988 STEP
    int dir_gpio;           // A4988 DIR (low = clockwise, as on the Pi)
    int sleep_gpio;         // A4988 SLEEP (high = awake), -1 if tied awake
    int microstep_gpio;     // A4988 MS1-3 tied together: high = 1/16 step, low = full step
    int switch_a_gpio;      // limit switch ("white" on the Pi)
    int switch_b_gpio;      // limit switch ("green" on the Pi)
    bool switch_active_high;
    bool switch_pull_down;  // internal pull-down (active high) or pull-up (active low)
    float range_deg;        // nominal travel between the switches (160 on the Pi)
    float full_step_deg;    // 1.8 for a 200 step/rev motor
} stepper_config_t;

typedef enum {
    STEPPER_OK = 0,
    STEPPER_SWITCH_HIT,     // a limit switch closed during the move
    STEPPER_DISABLED,       // a hardware fault disabled the motor; calibrate again
} stepper_result_t;

typedef struct {
    bool calibrated;
    bool sleeping;
    bool disabled;
    bool fine;              // 1/16 microstepping
    bool switch_a, switch_b;
    float angle_deg;
    float deg_per_step;
    char last_error[96];
} stepper_status_t;

esp_err_t stepper_init(const stepper_config_t *config);

// Finds both switches and centres between them, like Motor.calibrate().
// Takes about 30 s at 1/16 stepping. Returns ESP_OK when calibrated.
esp_err_t stepper_calibrate(bool force);

void stepper_set_fine(bool fine);       // true = 1/16 step (Pi "slow"), false = full step ("fast")
void stepper_wake(void);
void stepper_sleep(void);               // driver off; also drops calibration, as on the Pi

// One step in the given direction (+1 clockwise, -1 counter-clockwise), taking period_ms.
stepper_result_t stepper_step(int direction, uint32_t period_ms);
// Moves to an absolute angle at one step per period_ms.
stepper_result_t stepper_rotate_to(float angle_deg, uint32_t period_ms);

float stepper_angle(void);
float stepper_deg_per_step(void);
bool stepper_is_calibrated(void);
void stepper_get_status(stepper_status_t *status);

#ifdef __cplusplus
}
#endif
