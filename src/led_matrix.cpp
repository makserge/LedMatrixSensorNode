#include "led_matrix.h"
#include "constants.h"
#include "pins.h"

// The shift loop writes the GPIO set/clear registers directly, so its data and clock
// pins are compile-time constants taken from pins.h: each one lands in the bank it
// lives in (OUT for GPIO 0-31, OUT1 for 32-48). Masks of the other bank are 0 and
// their register writes compile away.
static constexpr uint32_t gpioBankMask(int pin, bool highBank) {
  return ((pin >= 32) == highBank) ? (1UL << (pin & 31)) : 0;
}
static_assert(R1_PIN >= 0 && R1_PIN <= 48 && G1_PIN >= 0 && G1_PIN <= 48 &&
              B1_PIN >= 0 && B1_PIN <= 48 && CLK_PIN >= 0 && CLK_PIN <= 48,
              "panel data/clock pins must be ESP32-S3 GPIOs 0-48");
static constexpr uint32_t R1_MASK_LO  = gpioBankMask(R1_PIN, false),  R1_MASK_HI  = gpioBankMask(R1_PIN, true);
static constexpr uint32_t G1_MASK_LO  = gpioBankMask(G1_PIN, false),  G1_MASK_HI  = gpioBankMask(G1_PIN, true);
static constexpr uint32_t B1_MASK_LO  = gpioBankMask(B1_PIN, false),  B1_MASK_HI  = gpioBankMask(B1_PIN, true);
static constexpr uint32_t CLK_MASK_LO = gpioBankMask(CLK_PIN, false), CLK_MASK_HI = gpioBankMask(CLK_PIN, true);

LedMatrix::LedMatrix(const HUB75_I2S_CFG& mxconfig) : _config(mxconfig), _bufferMux(portMUX_INITIALIZER_UNLOCKED) {
  _brightness = 2; 
  _displayTaskHandle = NULL; 

  _rBuffer = new uint16_t[_config.width];
  _gBuffer = new uint16_t[_config.width];
  _bBuffer = new uint16_t[_config.width];

  _rDisplay = new uint16_t[_config.width];
  _gDisplay = new uint16_t[_config.width];
  _bDisplay = new uint16_t[_config.width];
  
  memset(_rBuffer, 0, _config.width * sizeof(uint16_t));
  memset(_gBuffer, 0, _config.width * sizeof(uint16_t));
  memset(_bBuffer, 0, _config.width * sizeof(uint16_t));
  memset(_rDisplay, 0, _config.width * sizeof(uint16_t));
  memset(_gDisplay, 0, _config.width * sizeof(uint16_t));
  memset(_bDisplay, 0, _config.width * sizeof(uint16_t));
}

LedMatrix::~LedMatrix() {
  if (_displayTaskHandle != NULL) vTaskDelete(_displayTaskHandle);
  delete[] _rBuffer; delete[] _gBuffer; delete[] _bBuffer;
  delete[] _rDisplay; delete[] _gDisplay; delete[] _bDisplay;
}

bool LedMatrix::begin() {
  if (_config.pins.r1 != R1_PIN || _config.pins.g1 != G1_PIN ||
      _config.pins.b1 != B1_PIN || _config.pins.clk != CLK_PIN) {
    Serial.println("[Matrix] R1/G1/B1/CLK differ from pins.h - the shift loop drives the pins.h ones");
  }
  pinMode(_config.pins.clk, OUTPUT); pinMode(_config.pins.lat, OUTPUT);
  pinMode(_config.pins.r1, OUTPUT); pinMode(_config.pins.g1, OUTPUT); pinMode(_config.pins.b1, OUTPUT);
  pinMode(_config.pins.a, OUTPUT); pinMode(_config.pins.b, OUTPUT); pinMode(_config.pins.c, OUTPUT);
  pinMode(_config.chip1, OUTPUT); pinMode(_config.chip2, OUTPUT); pinMode(_config.chip3, OUTPUT);
  
  digitalWrite(_config.chip1, LOW); digitalWrite(_config.chip2, LOW); digitalWrite(_config.chip3, LOW);
  digitalWrite(_config.pins.clk, LOW); digitalWrite(_config.pins.lat, LOW);

  pinMode(_config.pins.oe, OUTPUT);
  digitalWrite(_config.pins.oe, HIGH);

  clearScreen();
  swapBuffers(); 

  if (_displayTaskHandle == NULL) {
    // Not "DisplayTask": that is the view/render task (display_manager.cpp), and a shared
    // name makes stack-overflow panics and task lists ambiguous.
    xTaskCreatePinnedToCore(this->displayTaskEngine, "MatrixEngine", MATRIX_ENGINE_TASK_STACK_SIZE, this,
                            MATRIX_ENGINE_TASK_PRIORITY, &_displayTaskHandle, 1);
  }
  return true;
}

void LedMatrix::setBrightness(int brightness) { 
  _brightness = constrain(brightness, 0, 255); 
}

void LedMatrix::drawPixel(int16_t x, int16_t y, uint16_t color) {
  if (x < 0 || x >= _config.width || y < 0 || y >= _config.height) return;

  bool r_high = (color & 0x0004);
  bool g_high = (color & 0x0002);
  bool b_high = (color & 0x0001);

  if (r_high) _rBuffer[x] |= (1 << y);  else _rBuffer[x] &= ~(1 << y);
  if (g_high) _gBuffer[x] |= (1 << y);  else _gBuffer[x] &= ~(1 << y);
  if (b_high) _bBuffer[x] |= (1 << y);  else _bBuffer[x] &= ~(1 << y);
}

void LedMatrix::fillScreen(uint16_t color) {
  uint16_t rMask = (color & 0x0004) ? 0xFFFF : 0x0000;
  uint16_t gMask = (color & 0x0002) ? 0xFFFF : 0x0000;
  uint16_t bMask = (color & 0x0001) ? 0xFFFF : 0x0000;
  for (int col = 0; col < _config.width; col++) {
    _rBuffer[col] = rMask; _gBuffer[col] = gMask; _bBuffer[col] = bMask;
  }
}
void LedMatrix::clearScreen() { fillScreen(0x0000); }

void LedMatrix::swapBuffers() {
  portENTER_CRITICAL(&_bufferMux);
  uint16_t* tempR = _rDisplay; _rDisplay = _rBuffer; _rBuffer = tempR;
  uint16_t* tempG = _gDisplay; _gDisplay = _gBuffer; _gBuffer = tempG;
  uint16_t* tempB = _bDisplay; _bDisplay = _bBuffer; _bBuffer = tempB;
  portEXIT_CRITICAL(&_bufferMux);
}

// Every character takes one cell: one the font lacks is drawn as the placeholder
// rather than dropped, so "-5" can't turn into "5" and a text's drawn width is
// always length * FONT_4X14_CELL_WIDTH.
static int glyphIndex(uint16_t c) {
  if (c >= 'a' && c <= 'z') c -= 'a' - 'A';
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'Z') return (c - 'A') + 10;
  switch (c) {
    case ':':  return 36;
    case '*':  return 37; // degree sign
    case '.':  return 38;
    case '-':  return 39;
    case '!':  return 40;
    case '?':  return 41;
    case '/':  return 42;
    case '%':  return 43;
    case '+':  return 44;
    case ',':  return 45;
    case '\'': return 46;
    default:   return FONT_4X14_UNKNOWN;
  }
}

void LedMatrix::drawText4x14(const uint16_t* text, int length, int startX, int startY, uint16_t color) {
  int currentX = startX;
  for (int i = 0; i < length; i++) {
    uint16_t c = text[i];
    if (c == ' ' || c == 0x0020) {
      for (int rowOffset = 0; rowOffset < 14; rowOffset++) {
        for (int colOffset = 0; colOffset < FONT_4X14_CELL_WIDTH; colOffset++) drawPixel(currentX + colOffset, startY + rowOffset, 0x0000);
      }
      currentX += FONT_4X14_CELL_WIDTH; continue;
    }
    int fontIndex = glyphIndex(c);

    uint16_t colData0 = 0;
    uint16_t colData1 = 0;
    uint16_t colData2 = 0;
    uint16_t colData3 = 0;

    for (int rowOffset = 0; rowOffset < 14; rowOffset++) {
      uint8_t rowByte = pgm_read_byte(&(font4x14_digits[fontIndex][rowOffset]));
      if ((rowByte >> 3) & 0x01) colData0 |= (1 << (startY + rowOffset));
      if ((rowByte >> 2) & 0x01) colData1 |= (1 << (startY + rowOffset));
      if ((rowByte >> 1) & 0x01) colData2 |= (1 << (startY + rowOffset));
      if ((rowByte >> 0) & 0x01) colData3 |= (1 << (startY + rowOffset));
    }

    for (int rowOffset = 0; rowOffset < 14; rowOffset++) {
      if ((colData0 >> (startY + rowOffset)) & 0x01) drawPixel(currentX + 0, startY + rowOffset, color);
      else drawPixel(currentX + 0, startY + rowOffset, 0x0000);
      
      if ((colData1 >> (startY + rowOffset)) & 0x01) drawPixel(currentX + 1, startY + rowOffset, color);
      else drawPixel(currentX + 1, startY + rowOffset, 0x0000);
      
      if ((colData2 >> (startY + rowOffset)) & 0x01) drawPixel(currentX + 2, startY + rowOffset, color);
      else drawPixel(currentX + 2, startY + rowOffset, 0x0000);
      
      if ((colData3 >> (startY + rowOffset)) & 0x01) drawPixel(currentX + 3, startY + rowOffset, color);
      else drawPixel(currentX + 3, startY + rowOffset, 0x0000);
      
      drawPixel(currentX + 4, startY + rowOffset, 0x0000);
    }
    currentX += FONT_4X14_CELL_WIDTH;
  }
}

void IRAM_ATTR LedMatrix::shiftOutRGBParallel(uint16_t rData, uint16_t gData, uint16_t bData) {
  for (int i = 0; i < 16; i++) {
    uint32_t low_set = 0; uint32_t low_clear = 0;
    uint32_t high_set = 0; uint32_t high_clear = 0;

    if ((gData >> i) & 0x01) { low_set |= G1_MASK_LO; high_set |= G1_MASK_HI; }
    else                     { low_clear |= G1_MASK_LO; high_clear |= G1_MASK_HI; }
    if ((bData >> i) & 0x01) { low_set |= B1_MASK_LO; high_set |= B1_MASK_HI; }
    else                     { low_clear |= B1_MASK_LO; high_clear |= B1_MASK_HI; }
    if ((rData >> i) & 0x01) { low_set |= R1_MASK_LO; high_set |= R1_MASK_HI; }
    else                     { low_clear |= R1_MASK_LO; high_clear |= R1_MASK_HI; }

    if (low_set)    REG_WRITE(GPIO_OUT_W1TS_REG, low_set);
    if (low_clear)  REG_WRITE(GPIO_OUT_W1TC_REG, low_clear);
    if (high_set)   REG_WRITE(GPIO_OUT1_W1TS_REG, high_set);
    if (high_clear) REG_WRITE(GPIO_OUT1_W1TC_REG, high_clear);

    if (CLK_MASK_LO) REG_WRITE(GPIO_OUT_W1TS_REG, CLK_MASK_LO);
    if (CLK_MASK_HI) REG_WRITE(GPIO_OUT1_W1TS_REG, CLK_MASK_HI);
    asm volatile("nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop;");
    if (CLK_MASK_LO) REG_WRITE(GPIO_OUT_W1TC_REG, CLK_MASK_LO);
    if (CLK_MASK_HI) REG_WRITE(GPIO_OUT1_W1TC_REG, CLK_MASK_HI);
    asm volatile("nop; nop; nop; nop; nop; nop;");
  }
}

static inline void IRAM_ATTR fastPinWrite(int pin, int level) {
    gpio_ll_set_level(&GPIO, (gpio_num_t)pin, level);
}

void IRAM_ATTR LedMatrix::displayTaskEngine(void* pvParameters) {
  LedMatrix* panel = reinterpret_cast<LedMatrix*>(pvParameters);
  while (true) {
    fastPinWrite(panel->_config.pins.oe, HIGH);

    uint16_t* rDisp; uint16_t* gDisp; uint16_t* bDisp;
    portENTER_CRITICAL(&panel->_bufferMux);
    rDisp = panel->_rDisplay;
    gDisp = panel->_gDisplay;
    bDisp = panel->_bDisplay;
    portEXIT_CRITICAL(&panel->_bufferMux);

    for (int col = panel->_config.width - 1; col >= 0; col--) {
      fastPinWrite(panel->_config.pins.oe, HIGH);
      esp_rom_delay_us(2);

      fastPinWrite(panel->_config.chip1, LOW);
      fastPinWrite(panel->_config.chip2, LOW);
      fastPinWrite(panel->_config.chip3, LOW);

      int subAddress = col % 8;
      fastPinWrite(panel->_config.pins.a, (subAddress & 0x01));
      fastPinWrite(panel->_config.pins.b, ((subAddress >> 1) & 0x01));
      fastPinWrite(panel->_config.pins.c, ((subAddress >> 2) & 0x01));

      shiftOutRGBParallel(rDisp[col], gDisp[col], bDisp[col]);

      fastPinWrite(panel->_config.pins.lat, HIGH);
      asm volatile("nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop;");
      fastPinWrite(panel->_config.pins.lat, LOW);

      if (col < 8)       fastPinWrite(panel->_config.chip1, HIGH);
      else if (col < 16) fastPinWrite(panel->_config.chip2, HIGH);
      else               fastPinWrite(panel->_config.chip3, HIGH);

      const uint32_t columnWindowUs = 180;
      uint32_t onTimeUs = ((uint32_t)panel->_brightness * columnWindowUs) / 255;
      if (onTimeUs > 0) {
        fastPinWrite(panel->_config.pins.oe, LOW);
        esp_rom_delay_us(onTimeUs);
        fastPinWrite(panel->_config.pins.oe, HIGH);
      }
      if (onTimeUs < columnWindowUs) {
        esp_rom_delay_us(columnWindowUs - onTimeUs);
      }
    }

    fastPinWrite(panel->_config.pins.oe, HIGH);

    vTaskDelay(1);
  }
}