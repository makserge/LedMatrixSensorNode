const WIDTH = 24;
const HEIGHT = 16;
const EXPECTED_PIXELS = WIDTH * HEIGHT; // 384 pixels

const grid = document.getElementById('pixelGrid');
const colorPicker = document.getElementById('colorPicker');
const rleOutput = document.getElementById('rleOutput');
const mqttOutput = document.getElementById('mqttOutput');

// MQTT Input Elements
const mqttHost = document.getElementById('mqttHost');
const mqttUser = document.getElementById('mqttUser');
const mqttPass = document.getElementById('mqttPass');

// Drag & Drop / Preview Upload Elements
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const thumbPreview = document.getElementById('thumbPreview');
const dropZoneContent = document.getElementById('dropZoneContent');
const importBtn = document.getElementById('importBtn');

let isDrawing = false;
let pixelData = Array(EXPECTED_PIXELS).fill('000000'); 

// Restore saved settings automatically
if (localStorage.getItem('mqtt_host')) mqttHost.value = localStorage.getItem('mqtt_host');
if (localStorage.getItem('mqtt_user')) mqttUser.value = localStorage.getItem('mqtt_user');
if (localStorage.getItem('mqtt_pass')) mqttPass.value = localStorage.getItem('mqtt_pass');

function initGrid() {
  grid.innerHTML = '';
  for (let i = 0; i < EXPECTED_PIXELS; i++) {
    const pixel = document.createElement('div');
    pixel.classList.add('pixel');
    pixel.dataset.index = i;
    pixel.style.backgroundColor = '#' + pixelData[i];
    
    pixel.addEventListener('mousedown', (e) => {
      isDrawing = true;
      paintPixel(e.target);
    });
    pixel.addEventListener('mouseenter', (e) => {
      if (isDrawing) paintPixel(e.target);
    });
    
    grid.appendChild(pixel);
  }
}

window.addEventListener('mouseup', () => isDrawing = false);

function paintPixel(pixelEl) {
  const idx = pixelEl.dataset.index;
  const selectedColor = colorPicker.value.substring(1).toUpperCase();
  pixelData[idx] = selectedColor;
  pixelEl.style.backgroundColor = '#' + selectedColor;
  generateStrings();
}

function generateStrings() {
  let rleString = "";
  let currentRunColor = pixelData[0];
  let runCount = 1;

  for (let i = 1; i < pixelData.length; i++) {
    if (pixelData[i] === currentRunColor && runCount < 255) {
      runCount++;
    } else {
      rleString += runCount.toString(16).padStart(2, '0').toUpperCase() + currentRunColor;
      currentRunColor = pixelData[i];
      runCount = 1;
    }
  }
  rleString += runCount.toString(16).padStart(2, '0').toUpperCase() + currentRunColor;
  
  rleOutput.value = rleString;
  
  const host = mqttHost.value || 'x.x.x.x';
  const user = mqttUser.value || 'user';
  const pass = mqttPass.value || 'pass';
  
  mqttOutput.value = `mosquitto_pub -h ${host} -t led_matrix_node/display/icon -u "${user}" -P "${pass}" -m "${rleString}"`;
  
  localStorage.setItem('mqtt_host', mqttHost.value);
  localStorage.setItem('mqtt_user', mqttUser.value);
  localStorage.setItem('mqtt_pass', mqttPass.value);
}

// BACK-CONVERTER ENGINE (Decodes custom RLE packets back into workspace array indices)
function importRLEStream() {
  let input = rleOutput.value.trim();
  if (!input) return alert("Please enter an RLE hex string to parse!");

  // If a full command string is pasted, use Regex to strip everything outside the quotation marks
  const commandMatch = input.match(/-m\s+"([A-Fa-f0-9]+)"/) || input.match(/"([A-Fa-f0-9]+)"$/);
  if (commandMatch && commandMatch[1]) {
    input = commandMatch[1];
  }

  // Sanity check validation: Clean string can only contain legal Hexadecimal pairs
  const hexRegex = /^[A-Fa-f0-9]+$/;
  if (!hexRegex.test(input)) {
    return alert("Invalid Hex Stream! Characters must only be A-F and 0-9.");
  }

  // Packets must evaluate to sets of 8 characters exactly (1-byte count + 3-byte RGB)
  if (input.length % 8 !== 0) {
    return alert("Malformed structure packet array length! Length must be a multiple of 8.");
  }

  let temporaryPixelData = [];
  
  // Unpack chunks step-by-step
  for (let i = 0; i < input.length; i += 8) {
    const chunk = input.substring(i, i + 8);
    const countHex = chunk.substring(0, 2);
    const colorHex = chunk.substring(2, 8).toUpperCase();
    
    const pixelCount = parseInt(countHex, 16);
    if (isNaN(pixelCount) || pixelCount <= 0) {
      return alert("Malformed packet data stream parsing error! Run length evaluate logic failed.");
    }

    // Push the colors iteratively into the memory array stack
    for (let c = 0; c < pixelCount; c++) {
      temporaryPixelData.push(colorHex);
    }
  }

  // Cross-check that total volume targets match the display matrix profile boundaries exactly
  if (temporaryPixelData.length !== EXPECTED_PIXELS) {
    return alert(`Pixel count mismatch! The RLE string decoded to ${temporaryPixelData.length} pixels, but the display matrix requires exactly ${EXPECTED_PIXELS} pixels.`);
  }

  // Apply parsed arrays over active working index matrices and push execution update refresh
  pixelData = temporaryPixelData;
  
  // Wipe out dropzone thumbnail previews to reflect data import state override
  thumbPreview.style.display = 'none';
  thumbPreview.src = '';
  dropZoneContent.innerHTML = `<span class="drop-zone-text">Drop an image here or click to upload (Auto-converts to 24x16)</span>`;
  
  initGrid();
  generateStrings();
}

// Image handling downscaler block
function processImageFile(fileList) {
  if (!fileList || fileList.length === 0) return;
  const file = fileList[0];
  if (!file || !file.type.startsWith('image/')) return;

  const objectUrl = URL.createObjectURL(file);
  thumbPreview.src = objectUrl;
  thumbPreview.style.display = 'block';
  dropZoneContent.innerHTML = `<span class="drop-zone-text">Converted: <strong>${file.name}</strong></span>`;

  const img = new Image();
  img.onload = function() {
    const canvas = document.createElement('canvas');
    canvas.width = WIDTH;
    canvas.height = HEIGHT;
    const ctx = canvas.getContext('2d');
    
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, WIDTH, HEIGHT);
    ctx.drawImage(img, 0, 0, WIDTH, HEIGHT);
    
    const imgData = ctx.getImageData(0, 0, WIDTH, HEIGHT).data;
    for (let i = 0; i < imgData.length; i += 4) {
      const r = imgData[i].toString(16).padStart(2, '0');
      const g = imgData[i+1].toString(16).padStart(2, '0');
      const b = imgData[i+2].toString(16).padStart(2, '0');
      pixelData[i / 4] = (r + g + b).toUpperCase();
    }
    initGrid();
    generateStrings();
  };
  img.src = objectUrl;
}

// Interactive Hooks UI Event Listeners
importBtn.addEventListener('click', importRLEStream);

dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => processImageFile(e.target.files));

dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});

['dragleave', 'dragend'].forEach(eventName => {
  dropZone.addEventListener(eventName, () => dropZone.classList.remove('drag-over'));
});

dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer && e.dataTransfer.files) {
    processImageFile(e.dataTransfer.files);
  }
});

[mqttHost, mqttUser, mqttPass].forEach(element => {
  element.addEventListener('input', generateStrings);
});

document.getElementById('clearBtn').addEventListener('click', () => {
  pixelData.fill('000000');
  thumbPreview.style.display = 'none';
  thumbPreview.src = '';
  dropZoneContent.innerHTML = `<span class="drop-zone-text">Drop an image here or click to upload (Auto-converts to 24x16)</span>`;
  initGrid();
  generateStrings();
});

function setupCopy(buttonId, textAreaId) {
  const btn = document.getElementById(buttonId);
  const txt = document.getElementById(textAreaId);
  btn.addEventListener('click', () => {
    txt.select();
    navigator.clipboard.writeText(txt.value);
    const origText = btn.innerText;
    btn.innerText = "Copied!";
    setTimeout(() => btn.innerText = origText, 1200);
  });
}

setupCopy('copyRawBtn', 'rleOutput');
setupCopy('copyMqttBtn', 'mqttOutput');

initGrid();
generateStrings();
