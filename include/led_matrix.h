#ifndef LED_MATRIX_H
#define LED_MATRIX_H

#include <Arduino.h>
#include "font_4x14.h"
#include "soc/gpio_reg.h"
#include "hal/gpio_ll.h"

struct HUB75_I2S_CFG {
    struct i2s_pins {
        int r1, g1, b1, r2, g2, b2;
        int a, b, c, d, e;
        int lat, oe, clk;
    } pins;
    int width;
    int height;
    int panels;
    int chip1;
    int chip2;
    int chip3;

    HUB75_I2S_CFG() {}
    HUB75_I2S_CFG(int w, int h, int p, i2s_pins ipins, int c1, int c2, int c3) {
        width = w;
        height = h;
        panels = p;
        pins = ipins;
        chip1 = c1;
        chip2 = c2;
        chip3 = c3;
    }
};

class LedMatrix {
public:
    LedMatrix(const HUB75_I2S_CFG& mxconfig);
    ~LedMatrix();

    bool begin();
    void setBrightness(int brightness);

    void drawPixel(int16_t x, int16_t y, uint16_t color);
    void fillScreen(uint16_t color);
    void clearScreen();

    void swapBuffers();

    void drawText4x14(const uint16_t* text, int length, int startX, int startY, uint16_t color);

    static inline uint16_t color565(uint8_t r, uint8_t g, uint8_t b) {
        uint16_t r_bit = (r > 127) ? 0x0004 : 0x0000;
        uint16_t g_bit = (g > 127) ? 0x0002 : 0x0000;
        uint16_t b_bit = (b > 127) ? 0x0001 : 0x0000;
        return (r_bit | g_bit | b_bit);
    }

private:
    HUB75_I2S_CFG _config;
    uint8_t _brightness;
    TaskHandle_t _displayTaskHandle;

    uint16_t* _rBuffer;
    uint16_t* _gBuffer;
    uint16_t* _bBuffer;

    uint16_t* _rDisplay;
    uint16_t* _gDisplay;
    uint16_t* _bDisplay;

    portMUX_TYPE _bufferMux;

    static void IRAM_ATTR shiftOutRGBParallel(int clk, int rPin, int gPin, int bPin, uint16_t rData, uint16_t gData, uint16_t bData);
    static void IRAM_ATTR displayTaskEngine(void* pvParameters);
};

using MatrixPanel_I2S_DMA = LedMatrix;

#endif