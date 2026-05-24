#include "led_matrix.h"

LedMatrix::LedMatrix(const HUB75_I2S_CFG& mxconfig) : _config(mxconfig) {
  _brightness = 2; 
  _displayTaskHandle = NULL; 

  _rBuffer = new uint16_t[_config.width];
  _gBuffer = new uint16_t[_config.width];
  _bBuffer = new uint16_t[_config.width];
}

LedMatrix::~LedMatrix() {
  if (_displayTaskHandle != NULL) {
    vTaskDelete(_displayTaskHandle);
  }
  delete[] _rBuffer; 
  delete[] _gBuffer; 
  delete[] _bBuffer;
}

bool LedMatrix::begin() {
  pinMode(_config.pins.clk, OUTPUT); 
  pinMode(_config.pins.lat, OUTPUT);
  pinMode(_config.pins.r1, OUTPUT); 
  pinMode(_config.pins.g1, OUTPUT); 
  pinMode(_config.pins.b1, OUTPUT);
  pinMode(_config.pins.a, OUTPUT); 
  pinMode(_config.pins.b, OUTPUT); 
  pinMode(_config.pins.c, OUTPUT);
  pinMode(_config.chip1, OUTPUT); 
  pinMode(_config.chip2, OUTPUT); 
  pinMode(_config.chip3, OUTPUT);
  
  digitalWrite(_config.chip1, LOW); 
  digitalWrite(_config.chip2, LOW); 
  digitalWrite(_config.chip3, LOW);
  digitalWrite(_config.pins.lat, LOW);

  ledcAttach(_config.pins.oe, 50000, 8); 
  ledcWrite(_config.pins.oe, 255 - _brightness); 

  clearScreen();

  if (_displayTaskHandle == NULL) {
    xTaskCreatePinnedToCore(
      this->displayTaskEngine, "DisplayTask", 4096, this, 24, &_displayTaskHandle, 0
    );
  }
  return true;
}

void LedMatrix::setPanelBrightness(int brightness) {
  setBrightness8((uint8_t)brightness);
}

void LedMatrix::setBrightness8(uint8_t brightness) { 
  _brightness = constrain(brightness, 0, 255); 
  ledcWrite(_config.pins.oe, 255 - _brightness); 
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
  bool r_high = (color & 0x0004);
  bool g_high = (color & 0x0002);
  bool b_high = (color & 0x0001);

  uint16_t rMask = r_high ? 0xFFFF : 0x0000;
  uint16_t gMask = g_high ? 0xFFFF : 0x0000;
  uint16_t bMask = b_high ? 0xFFFF : 0x0000;

  for (int col = 0; col < _config.width; col++) {
    _rBuffer[col] = rMask;
    _gBuffer[col] = gMask;
    _bBuffer[col] = bMask;
  }
}

void LedMatrix::clearScreen() { 
  fillScreen(0x0000); 
}

void LedMatrix::drawText4x14(const uint16_t* text, int length, int startX, int startY, uint16_t color) {
  int currentX = startX;
  for (int i = 0; i < length; i++) {
    uint16_t c = text[i];
    if (c == ' ' || c == 0x0020) {
      for (int rowOffset = 0; rowOffset < 14; rowOffset++) {
        for (int colOffset = 0; colOffset < 5; colOffset++) drawPixel(currentX + colOffset, startY + rowOffset, 0x0000);
      }
      currentX += 5; continue;
    }
    int fontIndex = -1;
    if (c >= '0' && c <= '9')         fontIndex = c - '0';       
    else if (c >= 'A' && c <= 'Z')    fontIndex = (c - 'A') + 10; 
    else if (c == ':')                fontIndex = 36;            
    else if (c == '*')                fontIndex = 37;            
    else continue;                  

    for (int rowOffset = 0; rowOffset < 14; rowOffset++) {
      uint8_t rowByte = pgm_read_byte(&(font4x14_digits[fontIndex][rowOffset]));
      for (int colOffset = 0; colOffset < 4; colOffset++) {
        if ((rowByte >> (3 - colOffset)) & 0x01) drawPixel(currentX + colOffset, startY + rowOffset, color);
        else drawPixel(currentX + colOffset, startY + rowOffset, 0x0000);
      }
      drawPixel(currentX + 4, startY + rowOffset, 0x0000);
    }
    currentX += 5; 
  }
}

void IRAM_ATTR LedMatrix::shiftOutRGBParallel(int clk, int rPin, int gPin, int bPin, uint16_t rData, uint16_t gData, uint16_t bData) {
  for (int i = 0; i < 16; i++) {
    digitalWrite(rPin, (rData >> i) & 0x01); 
    digitalWrite(gPin, (gData >> i) & 0x01); 
    digitalWrite(bPin, (bData >> i) & 0x01);
    digitalWrite(clk, HIGH); 
    asm volatile("nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop; nop;"); 
    digitalWrite(clk, LOW);
    asm volatile("nop; nop; nop; nop; nop; nop;");
  }
}

void LedMatrix::displayTaskEngine(void* pvParameters) {
  LedMatrix* panel = reinterpret_cast<LedMatrix*>(pvParameters);
  while (true) {
    for (int col = panel->_config.width - 1; col >= 0; col--) {
      pinMode(panel->_config.pins.oe, OUTPUT);
      digitalWrite(panel->_config.pins.oe, HIGH); 
      esp_rom_delay_us(25); 
      
      digitalWrite(panel->_config.chip1, LOW); digitalWrite(panel->_config.chip2, LOW); digitalWrite(panel->_config.chip3, LOW);
      
      int subAddress = col % 8; 
      digitalWrite(panel->_config.pins.a, (subAddress & 0x01)); 
      digitalWrite(panel->_config.pins.b, ((subAddress >> 1) & 0x01)); 
      digitalWrite(panel->_config.pins.c, ((subAddress >> 2) & 0x01));
      
      shiftOutRGBParallel(panel->_config.pins.clk, panel->_config.pins.r1, panel->_config.pins.g1, panel->_config.pins.b1, panel->_rBuffer[col], panel->_gBuffer[col], panel->_bBuffer[col]);
      
      digitalWrite(panel->_config.pins.lat, HIGH); 
      asm volatile("nop; nop; nop; nop; nop; nop; nop; nop;"); 
      digitalWrite(panel->_config.pins.lat, LOW);
      
      if (col < 8)       digitalWrite(panel->_config.chip1, HIGH);  
      else if (col < 16) digitalWrite(panel->_config.chip2, HIGH);  
      else               digitalWrite(panel->_config.chip3, HIGH);  
      
      ledcAttach(panel->_config.pins.oe, 50000, 8); 
      
      esp_rom_delay_us(180); 
    }
    vTaskDelay(1); 
  }
}
