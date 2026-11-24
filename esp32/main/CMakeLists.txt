idf_component_register(SRCS "main.c" "hsi_camera.c" "storage.c" "net.c" "web.c"
                       INCLUDE_DIRS "."
                       EMBED_TXTFILES "index.html"
                       REQUIRES imx477 stepper esp_driver_cam esp_driver_isp esp_driver_i2c esp_driver_sdmmc
                                fatfs sdmmc esp_http_server esp_netif esp_event esp_wifi nvs_flash
                                esp_timer esp_mm)
