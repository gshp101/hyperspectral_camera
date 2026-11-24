#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include "esp_log.h"
#include "esp_check.h"
#include "esp_vfs_fat.h"
#include "driver/sdmmc_host.h"
#include "sd_pwr_ctrl_by_on_chip_ldo.h"
#include "sdmmc_cmd.h"
#include "sdkconfig.h"
#include "storage.h"

static const char *TAG = "storage";
static sdmmc_card_t *s_card;

esp_err_t storage_init(void)
{
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.slot = SDMMC_HOST_SLOT_0;          // P4 slot 0: fixed IOMUX pins, fastest path
    host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;

#if CONFIG_HSI_SD_LDO_CHAN >= 0
    // Board powers the card's IO from the P4's internal LDO.
    sd_pwr_ctrl_ldo_config_t ldo_cfg = {.ldo_chan_id = CONFIG_HSI_SD_LDO_CHAN};
    sd_pwr_ctrl_handle_t pwr = NULL;
    ESP_RETURN_ON_ERROR(sd_pwr_ctrl_new_on_chip_ldo(&ldo_cfg, &pwr), TAG, "SD LDO");
    host.pwr_ctrl_handle = pwr;
#endif

    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 4;

    esp_vfs_fat_sdmmc_mount_config_t mount_cfg = {
        .format_if_mount_failed = false,
        .max_files = 8,
        .allocation_unit_size = 64 * 1024,
    };
    ESP_RETURN_ON_ERROR(esp_vfs_fat_sdmmc_mount(STORAGE_MOUNT, &host, &slot, &mount_cfg, &s_card),
                        TAG, "mount failed; is a FAT32/exFAT card inserted?");
    sdmmc_card_print_info(stdout, s_card);
    mkdir(STORAGE_DIR, 0775);
    return ESP_OK;
}

bool storage_ready(void)
{
    return s_card != NULL;
}

esp_err_t storage_next_name(char *name, size_t len, int *number)
{
    ESP_RETURN_ON_FALSE(s_card, ESP_ERR_INVALID_STATE, TAG, "no card");
    int n = 1;
    FILE *f = fopen(STORAGE_DIR "/next.txt", "r");
    if (f) {
        if (fscanf(f, "%d", &n) != 1 || n < 1) {
            n = 1;
        }
        fclose(f);
    }
    f = fopen(STORAGE_DIR "/next.txt", "w");
    ESP_RETURN_ON_FALSE(f, ESP_FAIL, TAG, "cannot write next.txt");
    fprintf(f, "%d\n", n + 1);
    fclose(f);
    snprintf(name, len, "IMG_%05d", n);
    *number = n;
    return ESP_OK;
}

esp_err_t storage_index_add(int number, const char *file, const char *comment)
{
    struct stat st;
    bool fresh = stat(STORAGE_DIR "/index.csv", &st) != 0;
    FILE *f = fopen(STORAGE_DIR "/index.csv", "a");
    ESP_RETURN_ON_FALSE(f, ESP_FAIL, TAG, "cannot open index.csv");
    if (fresh) {
        fprintf(f, "number,file,time,comment\n");
    }
    char ts[32];
    time_t now = time(NULL);
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", localtime(&now));
    fputc('"', f);
    fprintf(f, "%d\",\"%s\",\"%s\",\"", number, file, ts);
    for (const char *c = comment ? comment : ""; *c; c++) {
        if (*c == '"') {
            fputc('"', f);   // CSV escape
        }
        if (*c != '\n' && *c != '\r') {
            fputc(*c, f);
        }
    }
    fprintf(f, "\"\n");
    fclose(f);
    return ESP_OK;
}

esp_err_t storage_space(uint64_t *total_bytes, uint64_t *free_bytes)
{
    return esp_vfs_fat_info(STORAGE_MOUNT, total_bytes, free_bytes);
}
