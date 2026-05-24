#pragma once
#ifndef LED_MATRIX_H
#define LED_MATRIX_H

#include <Arduino.h>
#include "font_4x14.h"

struct HUB75_I2S_CFG {
  struct i2s_pins {
    int r1, g1, b1, r2, g2, b2; 
    int a, b, c, d, e;          
    int lat, oe, clk;
  } pins; 

  int width;
  int height;
  int chainLength; 

  int chip1;
  int chip2;
  int chip3;

  HUB75_I2S_CFG(int w, int h, int chain, i2s_pins p, int ch1 = 0, int ch2 = 0, int ch3 = 0) 
    : width(w), height(h), chainLength(chain), pins(p), chip1(ch1), chip2(ch2), chip3(ch3) {}
};

class LedMatrix {
private:
  HUB75_I2S_CFG _config;
  int _brightness;

  volatile uint16_t* _rBuffer;
  volatile uint16_t* _gBuffer;
  volatile uint16_t* _bBuffer;

  TaskHandle_t _displayTaskHandle; 

  static void IRAM_ATTR shiftOutRGBParallel(int clk, int rPin, int gPin, int bPin, uint16_t rData, uint16_t gData, uint16_t bData);
  static void displayTaskEngine(void* pvParameters);

public:
  LedMatrix(const HUB75_I2S_CFG& mxconfig);
  ~LedMatrix();

  bool begin();
  
  void setPanelBrightness(int brightness);
  void setBrightness8(uint8_t brightness);
  void clearScreen();
  void fillScreen(uint16_t color);
  void drawPixel(int16_t x, int16_t y, uint16_t color);
  
  void drawText4x14(const uint16_t* text, int length, int startX, int startY, uint16_t color);

  uint16_t width()  { return _config.width; }
  uint16_t height() { return _config.height; }

  static inline uint16_t color565(uint8_t r, uint8_t g, uint8_t b) {
    uint16_t r_bit = (r > 127) ? 0x0004 : 0x0000;
    uint16_t g_bit = (g > 127) ? 0x0002 : 0x0000;
    uint16_t b_bit = (b > 127) ? 0x0001 : 0x0000;
    return (r_bit | g_bit | b_bit);
  }
};

using MatrixPanel_I2S_DMA = LedMatrix;

#endif
