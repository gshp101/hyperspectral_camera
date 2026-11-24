// microSD card storage and the image index (replaces the Pi's SQLite database).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define STORAGE_MOUNT   "/sdcard"
#define STORAGE_DIR     STORAGE_MOUNT "/hsi"

esp_err_t storage_init(void);
bool storage_ready(void);
// Reserves the next image number (persists in STORAGE_DIR/next.txt) and writes
// "<base>" such as "IMG_00012" into name.
esp_err_t storage_next_name(char *name, size_t len, int *number);
// Appends a row to STORAGE_DIR/index.csv: number,file,time,comment
esp_err_t storage_index_add(int number, const char *file, const char *comment);
esp_err_t storage_space(uint64_t *total_bytes, uint64_t *free_bytes);
