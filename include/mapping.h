#ifndef MAPPING_H
#define MAPPING_H

#include <Arduino.h>

/**
 * HUB75 LED MATRIX (24x16)
 * Controlled via 3-to-8 Decoder (SM5166PF) and Shift Registers (SM16306SJ)
 */
#define R1_PIN  1  // D0
#define G1_PIN  2  // D1
#define B1_PIN  3  // D2

#define CDI_PIN 8  // D9
#define CLK_PIN 4  // D3
#define LAT_PIN 5  // D4
#define OE_PIN  6  // D5

// Row Addressing (Binary A, B, C, D selects 1 of 16 rows)
#define A_PIN   43 // D6
#define B_PIN   44 // D7
#define C_PIN   7  // D8

/**
 * PERIPHERALS (XIAO ESP32-S3 Plus Expansion Pads)
 * These are the pads located on the back and sides of the module.
 */

// I2C Bus (BH1750 Light Sensor & SCD40 CO2 Sensor)
#define I2C_SDA_PIN  40 // D13
#define I2C_SCL_PIN  39 // D12

// LD2450 Radar (Hardware Serial 1)
#define RADAR_RX_PIN 41 // D14
#define RADAR_TX_PIN 42 // D15
#define RADAR_BAUD   256000

// UI Elements
#define BEEPER_PIN   9 // D10
#define BUTTON1_PIN   13 // D17
#define BUTTON2_PIN   12 // D18

#define RADAR_PRESENCE_PIN   21 // D19

#endif