#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <Audio.h>
#include <arduinoFFT.h>
#include <LD2450.h>
#include <BH1750.h>
#include <SensirionI2cScd4x.h>
#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>
#include "mapping.h"

// --- Configuration ---
const char* ssid     = "DEIN_WLAN";
const char* password = "DEIN_PASSWORT";
const char* mpd_url  = "http://1.xx"; 

// --- Objects ---
MatrixPanel_I2S_DMA *display = nullptr;
Audio audio;
//LD2450 radar(Serial1);
BH1750 lightMeter;
SensirionI2cScd4x scd4x;

// FFT Settings
#define SAMPLES 64
double vReal[SAMPLES];
double vImag[SAMPLES];
ArduinoFFT<double> FFT = ArduinoFFT<double>(vReal, vImag, SAMPLES, 44100);

// Global Variables
uint16_t co2 = 0;
float temp = 0, hum = 0;
unsigned long lastSensorRead = 0;
bool showRadar = true;

// --- Callbacks ---
// This function "steals" the audio data from the stream for FFT
void audio_process_i2s_config(int16_t* samples, uint16_t sampleCount) {
    for (uint16_t i = 0; i < SAMPLES && i < sampleCount; i++) {
        vReal[i] = (double)samples[i];
        vImag[i] = 0;
    }
}

void setup() {
    Serial.begin(115200);

    // 1. Matrix Initialization
    HUB75_I2S_CFG::i2s_pins _pins = {R1_PIN, G1_PIN, B1_PIN, R2_PIN, G2_PIN, B2_PIN, 
                                     A_PIN, B_PIN, C_PIN, D_PIN, E_PIN, 
                                     LAT_PIN, OE_PIN, CLK_PIN};
    HUB75_I2S_CFG mxConfig(24, 16, 1, _pins);
    display = new MatrixPanel_I2S_DMA(mxConfig);
    display->begin();
    display->setBrightness8(100);

    // 2. WiFi Setup
    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) delay(500);

    // 3. Audio Streaming (Virtual I2S - Output Off)
    audio.setPinout(-1, -1, -1); 
    audio.setVolume(21); // Max internal gain for FFT peaks
    audio.connecttohost(mpd_url);

    // 4. Radar Setup (XIAO Plus Pads)
    Serial1.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX_PIN, RADAR_TX_PIN);
    //radar.begin();

    // 5. I2C Sensors
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    lightMeter.begin();
   // scd4x.begin(Wire);
    scd4x.startPeriodicMeasurement();

    // 6. UI Pins
    pinMode(BEEPER_PIN, OUTPUT);
    pinMode(BUTTON_PIN, INPUT); 
}

void loop() {
    // A. High Priority Tasks
    audio.loop();
    //radar.read();

    // B. Mode Switching (Touch)
    if (touchRead(BUTTON_PIN) < 30) { // Adjust threshold based on your setup
        showRadar = !showRadar;
        delay(300); // Debounce
    }

    // C. Sensor Handling (Every 5 seconds)
    if (millis() - lastSensorRead > 5000) {
        // Auto Brightness
        float lux = lightMeter.readLightLevel();
        int br = map(constrain(lux, 0, 600), 0, 600, 10, 255);
        display->setBrightness8(br);

        // Environment
        scd4x.readMeasurement(co2, temp, hum);
        lastSensorRead = millis();
    }

    // D. Rendering
    display->fillScreen(0);

    // D1. Draw FFT Spectrum (Background)
    FFT.windowing(FFT_WIN_TYP_HAMMING, FFT_FORWARD);
    FFT.compute(FFT_FORWARD);
    FFT.complexToMagnitude();

    for (int i = 0; i < 24; i++) {
        int barH = map((int)vReal[i+1], 0, 10000, 0, 16);
        display->drawFastVLine(i, 15 - barH, barH, display->color565(0, 0, 100));
    }

    // D3. Alerts
    if (co2 > 1200) {
        display->setCursor(0, 0);
        display->setTextColor(display->color565(255, 255, 0));
        display->print("CO2!");
        digitalWrite(BEEPER_PIN, (millis() % 1000 < 500)); // Blink/Beep
    } else {
        digitalWrite(BEEPER_PIN, LOW);
    }
}