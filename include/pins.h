#ifndef PINS_H
#define PINS_H

#include <Arduino.h>

#define R1_PIN  42  // D15
#define G1_PIN  D3  // D3
#define B1_PIN  10  // D16
#define CLK_PIN D4  // D4
#define LAT_PIN D6  // D6
#define OE_PIN  D5  // D5

#define A_PIN    D0 // D0
#define B_PIN    38 // D11
#define C_PIN    39 // D12
#define L_EN_PIN 40 // D13
#define M_EN_PIN 41 // D14
#define R_EN_PIN D2 // D2

#define I2C_SDA_PIN 12 
#define I2C_SCL_PIN 7

// UI Elements
#define BEEPER_PIN          13
#ifdef BOARD_REV2
#define BUTTON_LADDER_PIN   D9   // GPIO8, ADC1_CH7: both buttons as a ladder (idle ~3.3 V, mode 0 V, power / LED off ~1.65 V)
#define RADAR_PWR_EN_PIN    D10  // GPIO9: HIGH = both radars powered (Q2 -> Q1); R7 keeps them ON while the pin is not driven
#else
#define MODE_BUTTON_PIN     D9 // D9
#define SWITCH_OFF_LED_PIN  D10 // D10
#endif

#define RADAR_PRESENCE_PIN   D1

// LD2454 Radar (Hardware Serial 1)
#define RADAR_RX_PIN 11
#define RADAR_TX_PIN D7
#define RADAR_BAUD   256000

#endif