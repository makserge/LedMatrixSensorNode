#include "ntp_clock.h"

QueueHandle_t clockQueue = NULL;

// getLocalTime() polls for up to 5 s while the time is unset, which stalls the caller
// (the display task on every CO2 popup); with a 0 ms timeout it reports "unset" whenever
// millis() ticks between its two reads. Same validity test, without either.
bool readLocalTime(struct tm* info) {
    time_t now = time(nullptr);
    localtime_r(&now, info);
    return info->tm_year > (2016 - 1900);
}

void NtpClock::begin() {
    configTzTime(TIME_ZONE, NTP_SERVER);
}

void NtpClock::update(bool showColon) {
    struct tm timeinfo;
    ClockMessage msg;

    if (readLocalTime(&timeinfo)) {
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
        msg.synced = true;
    } else {
        msg.text[0] = 0;
        msg.startX = 0;
        msg.synced = false;
    }

    if (clockQueue != NULL) {
        xQueueOverwrite(clockQueue, &msg);
    }
}

void ntpClockTask(void *pvParameters) {
    NtpClock ntpClock;
    ntpClock.begin();
    bool showColon = true;

    for (;;) {
        waitWhileOtaPaused();
        ntpClock.update(showColon);
        showColon = !showColon;
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void initNtpClockTask() {
    clockQueue = xQueueCreate(CLOCK_QUEUE_LENGTH, sizeof(ClockMessage));

    xTaskCreatePinnedToCore(
        ntpClockTask, "NtpClockTask", CLOCK_TASK_STACK_SIZE, NULL,
        CLOCK_TASK_PRIORITY, NULL, 1
    );
}