#include <Arduino.h>
#include "time.h"
#include "constants.h"
#include "ntp_clock.h"

QueueHandle_t clockQueue = NULL;

void NtpClock::begin() {
    configTzTime(TIME_ZONE, NTP_SERVER);
}

void NtpClock::update(bool showColon) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
        ClockMessage msg;
        int currentHour = timeinfo.tm_hour;
        int currentMinute = timeinfo.tm_min;

        if (currentHour < 10) {
            msg.text[0] = '0' + currentHour;
            msg.text[1] = showColon ? ':' : ' ';
            msg.text[2] = '0' + (currentMinute / 10);
            msg.text[3] = '0' + (currentMinute % 10);
            msg.text[4] = 0;
            msg.startX = 2; 
        } else {
            msg.text[0] = '0' + (currentHour / 10);
            msg.text[1] = '0' + (currentHour % 10);
            msg.text[2] = showColon ? ':' : ' ';
            msg.text[3] = '0' + (currentMinute / 10);
            msg.text[4] = '0' + (currentMinute % 10);
            msg.text[5] = 0;
            msg.startX = 0; 
        }

        if (clockQueue != NULL) {
            xQueueOverwrite(clockQueue, &msg);
        }
    }
}

void ntpClockTask(void *pvParameters) {
    NtpClock ntpClock;
    ntpClock.begin();
    bool showColon = true;

    for (;;) {
        ntpClock.update(showColon);
        showColon = !showColon;
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void initNtpClockTask() {
    clockQueue = xQueueCreate(CLOCK_QUEUE_LENGTH, sizeof(ClockMessage));

    xTaskCreatePinnedToCore(
        ntpClockTask,
        "NtpClockTask",
        CLOCK_TASK_STACK_SIZE,
        NULL,
        CLOCK_TASK_PRIORITY,
        NULL,
        1
    );
}
