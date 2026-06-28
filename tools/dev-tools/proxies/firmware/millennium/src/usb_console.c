/**
 * @file usb_console.c
 * @brief USB CDC console implementation
 *
 * Streams timestamped protocol traffic over USB CDC for host analysis.
 */

#include "usb_console.h"

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/logging/log.h>

#include <stdio.h>
#include <stdarg.h>
#include <string.h>

LOG_MODULE_REGISTER(usb_console, LOG_LEVEL_INF);

/* USB CDC device */
static const struct device *cdc_dev;
static bool usb_ready = false;

/* Output buffer for formatting */
#define OUTPUT_BUF_SIZE 512
static char output_buf[OUTPUT_BUF_SIZE];
static struct k_mutex output_mutex;

/**
 * Get timestamp string in HH:MM:SS.mmm format.
 *
 * Uses uptime since we don't have RTC. Hours wrap at 24.
 */
static void get_timestamp(char *buf, size_t len)
{
    uint32_t uptime_ms = k_uptime_get_32();
    uint32_t ms = uptime_ms % 1000;
    uint32_t sec = (uptime_ms / 1000) % 60;
    uint32_t min = (uptime_ms / 60000) % 60;
    uint32_t hr = (uptime_ms / 3600000) % 24;
    
    snprintf(buf, len, "%02u:%02u:%02u.%03u", hr, min, sec, ms);
}

/**
 * Write string to USB CDC.
 */
static void cdc_write_string(const char *str)
{
    if (!usb_ready || !cdc_dev) {
        return;
    }
    
    size_t len = strlen(str);
    
    for (size_t i = 0; i < len; i++) {
        uart_poll_out(cdc_dev, str[i]);
    }
}

int usb_console_init(void)
{
    k_mutex_init(&output_mutex);
    
    /* Get CDC ACM device */
    cdc_dev = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
    if (!device_is_ready(cdc_dev)) {
        LOG_ERR("CDC ACM device not ready");
        return -ENODEV;
    }
    
    /* Wait for USB to be configured */
    k_sleep(K_MSEC(1000));
    
    usb_ready = true;
    LOG_INF("USB CDC console initialized");
    
    /* Print startup banner */
    usb_console_log_status("Millennium BLE Proxy initialized");
    usb_console_log_status("Waiting for connections...");
    
    return 0;
}

void usb_console_log_traffic(traffic_dir_t dir, const uint8_t *data, size_t len)
{
    if (!usb_ready || len == 0) {
        return;
    }
    
    k_mutex_lock(&output_mutex, K_FOREVER);
    
    char timestamp[16];
    get_timestamp(timestamp, sizeof(timestamp));
    
    const char *dir_str = (dir == DIR_APP_TO_BOARD) ? "APP->BOARD" : "BOARD->APP";
    
    /* Format: [HH:MM:SS.mmm] DIR: xx xx xx ... */
    int pos = snprintf(output_buf, OUTPUT_BUF_SIZE, "[%s] %s:", timestamp, dir_str);
    /* snprintf returns the length it WOULD have written; clamp so a truncated
     * header can't push pos past the buffer when it is used as an index below. */
    if (pos < 0) {
        pos = 0;
    } else if (pos > OUTPUT_BUF_SIZE - 1) {
        pos = OUTPUT_BUF_SIZE - 1;
    }
    
    /* Append hex bytes. Check each return value so OUTPUT_BUF_SIZE - pos can
     * never underflow into a huge size (CodeQL cpp/overflowing-snprintf). */
    for (size_t i = 0; i < len; i++) {
        int n = snprintf(output_buf + pos, OUTPUT_BUF_SIZE - pos, " %02x", data[i]);
        if (n < 0 || n >= OUTPUT_BUF_SIZE - pos) {
            break;  /* buffer full or encoding error */
        }
        pos += n;
    }
    
    /* Add newline */
    if (pos < OUTPUT_BUF_SIZE - 2) {
        output_buf[pos++] = '\r';
        output_buf[pos++] = '\n';
        output_buf[pos] = '\0';
    }
    
    cdc_write_string(output_buf);
    
    k_mutex_unlock(&output_mutex);
}

void usb_console_log_decoded(traffic_dir_t dir, const char *msg)
{
    if (!usb_ready) {
        return;
    }
    
    k_mutex_lock(&output_mutex, K_FOREVER);
    
    char timestamp[16];
    get_timestamp(timestamp, sizeof(timestamp));
    
    const char *dir_str = (dir == DIR_APP_TO_BOARD) ? "APP->BOARD" : "BOARD->APP";
    
    snprintf(output_buf, OUTPUT_BUF_SIZE, "[%s] %s: %s\r\n", timestamp, dir_str, msg);
    cdc_write_string(output_buf);
    
    k_mutex_unlock(&output_mutex);
}

void usb_console_log_status(const char *msg)
{
    if (!usb_ready) {
        return;
    }
    
    k_mutex_lock(&output_mutex, K_FOREVER);
    
    char timestamp[16];
    get_timestamp(timestamp, sizeof(timestamp));
    
    snprintf(output_buf, OUTPUT_BUF_SIZE, "[%s] STATUS: %s\r\n", timestamp, msg);
    cdc_write_string(output_buf);
    
    k_mutex_unlock(&output_mutex);
}

void usb_console_printf(const char *fmt, ...)
{
    if (!usb_ready) {
        return;
    }
    
    k_mutex_lock(&output_mutex, K_FOREVER);
    
    va_list args;
    va_start(args, fmt);
    vsnprintf(output_buf, OUTPUT_BUF_SIZE, fmt, args);
    va_end(args);
    
    cdc_write_string(output_buf);
    
    k_mutex_unlock(&output_mutex);
}

