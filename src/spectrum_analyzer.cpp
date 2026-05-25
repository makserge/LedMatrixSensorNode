#include "spectrum_analyzer.h"
#include "constants.h"
#include "Audio.h"
#include "arduinoFFT.h"
#include "display_manager.h"

SpectrumAnalyzer spectrumAnalyzer;
Audio audio;
ArduinoFFT<double> FFT;

double vReal[FFT_SAMPLES];
double vImag[FFT_SAMPLES];
int sampleCounter = 0;
portMUX_TYPE audioMux = portMUX_INITIALIZER_UNLOCKED;

uint8_t sharedBars[24] = {0};
uint8_t sharedPeaks[24] = {0};
unsigned long peakHoldTimers[24] = {0};
unsigned long peakDecayTimers[24] = {0};

double rollingMaxMagnitude = NORMALIZE_MIN_LIMIT;

void audio_process_i2s_to_internal_dac(int16_t sample_ch0, int16_t sample_ch1) {
    if (sampleCounter < FFT_SAMPLES) {
        vReal[sampleCounter] = (double)(sample_ch0 + sample_ch1) / 2.0;
        vImag[sampleCounter] = 0.0;
        sampleCounter++;
    }
}

void SpectrumAnalyzer::begin() {
    audio.connecttohost(AUDIO_STREAM_URL);
}

void SpectrumAnalyzer::updateAudio() {
    audio.loop();

    DisplayView currentView = DisplayManager::getCurrentView();

    if (audio.isRunning()) {
        if (currentView != VIEW_CUSTOM_MSG && 
            currentView != VIEW_SPECTRUM && 
            currentView != VIEW_TEMP_DATA && 
            currentView != VIEW_CO2) {
            DisplayManager::setView(VIEW_SPECTRUM);
        }
    } else {
        if (currentView == VIEW_SPECTRUM) {
            DisplayManager::setView(VIEW_TIME);
        }
    }

    if (sampleCounter >= FFT_SAMPLES) {
        FFT.dcRemoval(vReal, FFT_SAMPLES);
        FFT.windowing(vReal, FFT_SAMPLES, FFT_WIN_TYP_HAMMING, FFT_FORWARD);
        FFT.compute(vReal, vImag, FFT_SAMPLES, FFT_FORWARD);
        FFT.complexToMagnitude(vReal, vImag, FFT_SAMPLES);

        unsigned long now = millis();
        double currentFrameMax = 0.0;

        for (int i = 0; i < 24; i++) {
            int bin = i + 2;
            if (vReal[bin] > currentFrameMax) {
                currentFrameMax = vReal[bin];
            }
        }

        if (currentFrameMax > rollingMaxMagnitude) {
            rollingMaxMagnitude = currentFrameMax;
        } else {
            rollingMaxMagnitude *= NORMALIZE_DECAY_RATE;
            if (rollingMaxMagnitude < NORMALIZE_MIN_LIMIT) {
                rollingMaxMagnitude = NORMALIZE_MIN_LIMIT;
            }
        }

        portENTER_CRITICAL(&audioMux);
        for (int i = 0; i < 24; i++) {
            int bin = i + 2;
            double magnitude = vReal[bin];
            
            int barHeight = (int)((magnitude / rollingMaxMagnitude) * 16.0);
            
            if (barHeight > 16) barHeight = 16;
            if (barHeight < 0) barHeight = 0;
            
            if (barHeight >= sharedBars[i]) {
                sharedBars[i] = barHeight;
            } else if (sharedBars[i] > 0) {
                sharedBars[i]--;
            }

            if (barHeight >= sharedPeaks[i]) {
                sharedPeaks[i] = barHeight;
                peakHoldTimers[i] = now + SPECTRUM_PEAK_HOLD_TIME_MS; 
                peakDecayTimers[i] = now;
            } else if (now > peakHoldTimers[i]) {
                if (now - peakDecayTimers[i] >= SPECTRUM_PEAK_DECAY_TIME_MS) {
                    if (sharedPeaks[i] > 0) {
                        sharedPeaks[i]--;
                    }
                    peakDecayTimers[i] = now;
                }
            }
        }
        portEXIT_CRITICAL(&audioMux);

        sampleCounter = 0;
    }
}

void SpectrumAnalyzer::getBarsAndPeaks(uint8_t* outBars, uint8_t* outPeaks) {
    portENTER_CRITICAL(&audioMux);
    for (int i = 0; i < 24; i++) {
        outBars[i] = sharedBars[i];
        outPeaks[i] = sharedPeaks[i];
    }
    portEXIT_CRITICAL(&audioMux);
}

void spectrumAnalyzerTask(void* pvParameters) {
    spectrumAnalyzer.begin();
    for (;;) {
        spectrumAnalyzer.updateAudio();
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

void initSpectrumAnalyzerTask() {
    xTaskCreatePinnedToCore(
        spectrumAnalyzerTask,
        "AudioTask",
        AUDIO_TASK_STACK_SIZE,
        NULL,
        AUDIO_TASK_PRIORITY,
        NULL,
        0
    );
}
