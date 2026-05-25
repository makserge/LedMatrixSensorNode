#ifndef NTP_CLOCK_H
#define NTP_CLOCK_H

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

struct ClockMessage {
    uint16_t text[6];
    int startX;
};

class NtpClock {
public:
    void begin();
    void update(bool showColon);
};

extern QueueHandle_t clockQueue;

void initNtpClockTask();

#endif
