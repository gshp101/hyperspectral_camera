#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"     // provided by esp_wifi_remote on the P4, which has no radio of its own
#include "sdkconfig.h"
#include "net.h"

static const char *TAG = "net";

#define CONNECTED_BIT BIT0
#define FAILED_BIT    BIT1
#define MAX_RETRIES   5

static EventGroupHandle_t s_events;
static int s_retries;
static bool s_station;
static char s_ip[16] = "0.0.0.0";

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retries++ < MAX_RETRIES) {
            esp_wifi_connect();
        } else {
            xEventGroupSetBits(s_events, FAILED_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&ev->ip_info.ip));
        s_retries = 0;
        xEventGroupSetBits(s_events, CONNECTED_BIT);
    }
}

static esp_err_t start_ap(void)
{
    esp_netif_create_default_wifi_ap();
    wifi_config_t ap = {
        .ap = {
            .ssid = CONFIG_HSI_AP_SSID,
            .ssid_len = strlen(CONFIG_HSI_AP_SSID),
            .password = CONFIG_HSI_AP_PASSWORD,
            .max_connection = 4,
            .authmode = strlen(CONFIG_HSI_AP_PASSWORD) >= 8 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN,
        },
    };
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_AP), TAG, "AP mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &ap), TAG, "AP config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "AP start");
    strcpy(s_ip, "192.168.4.1");
    ESP_LOGI(TAG, "access point \"%s\" started, browse to http://%s/", CONFIG_HSI_AP_SSID, s_ip);
    return ESP_OK;
}

esp_err_t net_init(void)
{
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    esp_err_t err = esp_event_loop_create_default();
    ESP_RETURN_ON_FALSE(err == ESP_OK || err == ESP_ERR_INVALID_STATE, err, TAG, "event loop");
    s_events = xEventGroupCreate();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&cfg), TAG, "wifi init (is the C6 running ESP-Hosted slave firmware?)");
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    if (strlen(CONFIG_HSI_WIFI_SSID) > 0) {
        esp_netif_t *sta = esp_netif_create_default_wifi_sta();
        esp_netif_set_hostname(sta, CONFIG_HSI_HOSTNAME);
        wifi_config_t wc = {0};
        strlcpy((char *)wc.sta.ssid, CONFIG_HSI_WIFI_SSID, sizeof(wc.sta.ssid));
        strlcpy((char *)wc.sta.password, CONFIG_HSI_WIFI_PASSWORD, sizeof(wc.sta.password));
        ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "STA mode");
        ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wc), TAG, "STA config");
        ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "STA start");
        EventBits_t bits = xEventGroupWaitBits(s_events, CONNECTED_BIT | FAILED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(20000));
        if (bits & CONNECTED_BIT) {
            s_station = true;
            ESP_LOGI(TAG, "joined \"%s\", browse to http://%s/", CONFIG_HSI_WIFI_SSID, s_ip);
            esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_HSI_NTP_SERVER);
            esp_netif_sntp_init(&sntp);
            setenv("TZ", CONFIG_HSI_TIMEZONE, 1);
            tzset();
            return ESP_OK;
        }
        ESP_LOGW(TAG, "could not join \"%s\", falling back to access point", CONFIG_HSI_WIFI_SSID);
        esp_wifi_stop();
        esp_netif_destroy_default_wifi(sta);
    }
    return start_ap();
}

bool net_is_station(void)
{
    return s_station;
}

const char *net_ip(void)
{
    return s_ip;
}
