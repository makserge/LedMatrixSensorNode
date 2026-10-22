const WIDTH = 24;
const HEIGHT = 16;
const EXPECTED_PIXELS = WIDTH * HEIGHT; // 384 pixels
const ICON_TOPIC = 'led_matrix_node/display/icon';

// The panel has one bit per color channel: the firmware lights a channel when its value
// is above 127 (LedMatrix::color565), so these 8 colors are all it can show. Everything the
// editor holds is reduced to them, so the grid shows what the panel will show.
const PALETTE = [
  { name: 'Black',   hex: '000000' },
  { name: 'Red',     hex: 'FF0000' },
  { name: 'Green',   hex: '00FF00' },
  { name: 'Blue',    hex: '0000FF' },
  { name: 'Yellow',  hex: 'FFFF00' },
  { name: 'Cyan',    hex: '00FFFF' },
  { name: 'Magenta', hex: 'FF00FF' },
  { name: 'White',   hex: 'FFFFFF' },
];

function quantizeRgb(r, g, b) {
  return (r > 127 ? 'FF' : '00') + (g > 127 ? 'FF' : '00') + (b > 127 ? 'FF' : '00');
}

function quantizeHex(hex) {
  return quantizeRgb(parseInt(hex.substring(0, 2), 16),
                     parseInt(hex.substring(2, 4), 16),
                     parseInt(hex.substring(4, 6), 16));
}

// Every value goes into the command quoted for the chosen shell, so a password such as
// pa$$w0rd!"x reaches mosquitto_pub as typed instead of being expanded or ending the quote.
const SHELLS = {
  posix: {
    // Nothing is special inside single quotes ($, `, \, ! included); a single quote itself
    // closes the quote, is escaped and reopens it: ' -> '\''
    quote: (s) => "'" + s.replace(/'/g, "'\\''") + "'",
    passwordVar: '"$MQTT_PASS"',
    passwordHint: 'Password field empty: the command reads $MQTT_PASS. Set it first, without echo or shell history (bash, zsh): read -rs MQTT_PASS',
  },
  powershell: {
    // Single quotes are literal; a single quote (PowerShell also takes the typographic
    // ones for it) is doubled.
    quote: (s) => "'" + s.replace(/['\u2018\u2019\u201A\u201B]/g, '$&$&') + "'",
    passwordVar: '$env:MQTT_PASS',
    // Plain Read-Host echoes what is typed; -AsSecureString masks it, and NetworkCredential
    // turns the SecureString back into the plain string mosquitto_pub needs (PS 5.1 and 7).
    passwordHint: "Password field empty: the command reads $env:MQTT_PASS. Set it first, without echo or shell history: $env:MQTT_PASS = [Net.NetworkCredential]::new('', (Read-Host 'MQTT password' -AsSecureString)).Password",
  },
};

const grid = document.getElementById('pixelGrid');
const palette = document.getElementById('palette');
const rleOutput = document.getElementById('rleOutput');
const mqttOutput = document.getElementById('mqttOutput');
const mqttNote = document.getElementById('mqttNote');

// MQTT Input Elements
const mqttHost = document.getElementById('mqttHost');
const mqttUser = document.getElementById('mqttUser');
const mqttPass = document.getElementById('mqttPass');
const rememberPass = document.getElementById('rememberPass');
const shellSelect = document.getElementById('shellSelect');

// Drag & Drop / Preview Upload Elements
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const thumbPreview = document.getElementById('thumbPreview');
const dropZoneContent = document.getElementById('dropZoneContent');
const importBtn = document.getElementById('importBtn');
const DROP_ZONE_HINT = dropZoneContent.textContent.trim();

let isDrawing = false;
let drawingPointerId = null;
let brushColor = 'FFFFFF';
let thumbUrl = null;
let pixelData = Array(EXPECTED_PIXELS).fill('000000');

// localStorage throws when site data is blocked (e.g. for file://): the editor still
// works then, it just doesn't remember the settings.
function storageGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* not remembered */ }
}
function storageRemove(key) {
  try { localStorage.removeItem(key); } catch (e) { /* nothing stored */ }
}

// Restore saved settings. The password only if the user asked for it: localStorage is
// plaintext, and in Chromium every local HTML file opened from disk shares this page's
// storage, so any of them could read it.
function restoreSettings() {
  const host = storageGet('mqtt_host');
  const user = storageGet('mqtt_user');
  const shell = storageGet('mqtt_shell');
  if (host) mqttHost.value = host;
  if (user) mqttUser.value = user;
  if (shell && SHELLS[shell]) shellSelect.value = shell;

  rememberPass.checked = storageGet('mqtt_pass_remember') === '1';
  if (rememberPass.checked) {
    const pass = storageGet('mqtt_pass');
    if (pass) mqttPass.value = pass;
  } else {
    storageRemove('mqtt_pass'); // saved unasked by earlier versions of this page
  }
}

function saveSettings() {
  storageSet('mqtt_host', mqttHost.value);
  storageSet('mqtt_user', mqttUser.value);
  storageSet('mqtt_shell', shellSelect.value);
  if (rememberPass.checked) {
    storageSet('mqtt_pass_remember', '1');
    storageSet('mqtt_pass', mqttPass.value);
  } else {
    storageRemove('mqtt_pass_remember');
    storageRemove('mqtt_pass');
  }
}

function initPalette() {
  for (const { name, hex } of PALETTE) {
    const swatch = document.createElement('button');
    swatch.type = 'button';
    swatch.className = 'swatch';
    swatch.style.backgroundColor = '#' + hex;
    swatch.title = `${name} (${hex})`;
    swatch.setAttribute('aria-label', name);
    swatch.dataset.hex = hex;
    swatch.addEventListener('click', () => selectBrush(hex));
    palette.appendChild(swatch);
  }
  selectBrush(brushColor);
}

function selectBrush(hex) {
  brushColor = hex;
  for (const swatch of palette.children) {
    swatch.setAttribute('aria-pressed', String(swatch.dataset.hex === hex));
  }
}

function initGrid() {
  grid.replaceChildren();
  for (let i = 0; i < EXPECTED_PIXELS; i++) {
    const pixel = document.createElement('div');
    pixel.classList.add('pixel');
    pixel.dataset.index = i;
    pixel.style.backgroundColor = '#' + pixelData[i];
    grid.appendChild(pixel);
  }
}

function pixelAt(clientX, clientY) {
  const el = document.elementFromPoint(clientX, clientY);
  return (el && el.parentElement === grid && el.classList.contains('pixel')) ? el : null;
}

function stopDrawing() {
  isDrawing = false;
  drawingPointerId = null;
}

// Primary button only (left mouse button, finger, pen tip). A right-click used to paint
// too, and on macOS/Linux its context menu swallowed the release, so the brush stayed down.
grid.addEventListener('pointerdown', (e) => {
  if (e.button !== 0 || isDrawing) return;
  const pixelEl = pixelAt(e.clientX, e.clientY);
  if (!pixelEl) return;
  e.preventDefault();
  isDrawing = true;
  drawingPointerId = e.pointerId;
  paintPixel(pixelEl);
});

// Hit-tested on the window instead of per-pixel enter events: a touch pointer stays
// captured by the pixel it started on and never "enters" the others.
window.addEventListener('pointermove', (e) => {
  if (!isDrawing || e.pointerId !== drawingPointerId) return;
  if ((e.buttons & 1) === 0) { // released somewhere no pointerup reached this page
    stopDrawing();
    return;
  }
  const pixelEl = pixelAt(e.clientX, e.clientY);
  if (pixelEl) paintPixel(pixelEl);
});

window.addEventListener('pointerup', (e) => { if (e.pointerId === drawingPointerId) stopDrawing(); });
window.addEventListener('pointercancel', (e) => { if (e.pointerId === drawingPointerId) stopDrawing(); });
window.addEventListener('blur', stopDrawing);
grid.addEventListener('contextmenu', (e) => e.preventDefault());

function paintPixel(pixelEl) {
  const idx = Number(pixelEl.dataset.index);
  if (pixelData[idx] === brushColor) return;
  pixelData[idx] = brushColor;
  pixelEl.style.backgroundColor = '#' + brushColor;
  generateStrings();
}

function buildRleString() {
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
  return rleString;
}

// Empty fields are left out: no -h publishes to localhost, no -u connects without a login.
// With a user but no password, the command takes it from an environment variable, which
// keeps it off the screen and out of the shell history.
function buildCommand(rleString) {
  const shell = SHELLS[shellSelect.value] || SHELLS.posix;
  const host = mqttHost.value.trim();
  const user = mqttUser.value;
  const pass = mqttPass.value;

  const parts = ['mosquitto_pub'];
  if (host) parts.push('-h', shell.quote(host));
  parts.push('-t', ICON_TOPIC);
  if (user) parts.push('-u', shell.quote(user), '-P', pass ? shell.quote(pass) : shell.passwordVar);
  parts.push('-m', shell.quote(rleString));

  const notes = [];
  if (user && !pass) notes.push(shell.passwordHint);
  // Windows PowerShell 5.1 hands arguments to native programs without escaping a " inside
  // them, and wraps one with a space in quotes, so a trailing \ then escapes the closing one.
  const ps51Breaks = (v) => v.includes('"') || (/\s/.test(v) && v.endsWith('\\'));
  if (shell === SHELLS.powershell && [host, user, pass].some(ps51Breaks)) {
    notes.push('Windows PowerShell 5.1 passes a value with a " (or with a space and a trailing \\) to mosquitto_pub altered. Use PowerShell 7.3 or later, or bash (Git Bash, WSL).');
  }
  return { command: parts.join(' '), note: notes.join('\n') };
}

function generateStrings() {
  const rleString = buildRleString();
  rleOutput.value = rleString;

  const { command, note } = buildCommand(rleString);
  mqttOutput.value = command;
  mqttNote.textContent = note;
  mqttNote.hidden = note === '';
}

// The drop zone's text: built from nodes, so a file name is only ever text.
function showDropZoneText(text, fileName) {
  const span = document.createElement('span');
  span.className = 'drop-zone-text';
  span.append(text);
  if (fileName !== undefined) {
    const strong = document.createElement('strong');
    strong.textContent = fileName;
    span.append(strong);
  }
  dropZoneContent.replaceChildren(span);
}

function setThumbnail(url) {
  if (thumbUrl) URL.revokeObjectURL(thumbUrl);
  thumbUrl = url;
  if (url) {
    thumbPreview.src = url;
    thumbPreview.style.display = 'block';
  } else {
    thumbPreview.removeAttribute('src');
    thumbPreview.style.display = 'none';
  }
}

// BACK-CONVERTER ENGINE (Decodes custom RLE packets back into workspace array indices)
function importRLEStream() {
  let input = rleOutput.value.trim();
  if (!input) return alert("Please enter an RLE hex string to parse!");

  // A pasted command: take the -m argument, quoted ('...' or "...") or bare.
  const commandMatch = input.match(/(?:^|\s)-m\s+(['"]?)([A-Fa-f0-9\s]+)\1(?=\s|$)/) ||
                       input.match(/(['"])([A-Fa-f0-9\s]+)\1\s*$/);
  if (commandMatch && commandMatch[2]) {
    input = commandMatch[2];
  }
  input = input.replace(/\s+/g, ''); // the firmware ignores whitespace in hex payloads too

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
    const colorHex = quantizeHex(chunk.substring(2, 8)); // as the panel will show it

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
  setThumbnail(null);
  showDropZoneText(DROP_ZONE_HINT);

  initGrid();
  generateStrings();
}

// Image handling downscaler block
function processImageFile(fileList) {
  if (!fileList || fileList.length === 0) return;
  const file = fileList[0];
  if (!file || !file.type.startsWith('image/')) return;

  const objectUrl = URL.createObjectURL(file);
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
      pixelData[i / 4] = quantizeRgb(imgData[i], imgData[i + 1], imgData[i + 2]);
    }
    setThumbnail(objectUrl);
    showDropZoneText('Converted: ', file.name);
    initGrid();
    generateStrings();
  };
  img.onerror = function() {
    URL.revokeObjectURL(objectUrl);
    setThumbnail(null);
    showDropZoneText('Could not read this image: ', file.name);
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

[mqttHost, mqttUser, mqttPass, rememberPass, shellSelect].forEach(element => {
  element.addEventListener(element === rememberPass || element === shellSelect ? 'change' : 'input', () => {
    saveSettings();
    generateStrings();
  });
});

document.getElementById('clearBtn').addEventListener('click', () => {
  pixelData.fill('000000');
  setThumbnail(null);
  showDropZoneText(DROP_ZONE_HINT);
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

restoreSettings();
initPalette();
initGrid();
generateStrings();
