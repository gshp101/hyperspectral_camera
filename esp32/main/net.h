// Wi-Fi (through the ESP32-C6 co-processor) and network time.
#pragma once

#include <stdbool.h>
#include "esp_err.h"

// Joins the configured network. If that fails (or no SSID is set), starts an
// access point instead so the scanner can still be reached at 192.168.4.1.
esp_err_t net_init(void);
bool net_is_station(void);
const char *net_ip(void);
