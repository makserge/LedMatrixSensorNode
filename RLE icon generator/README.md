# 24x16 RLE Icon Editor and MQTT Publisher

A lightweight, high-utility web application designed to create, edit, and convert custom pixel artwork into custom Run-Length Encoded (RLE) Hex streams. It is specifically optimized for hardware displays with rectangular pixels (2:1 width-to-height ratio) and features a bidirectional compiler that can both pack canvas data and unpack/decode existing RLE strings back into editable pixels.

---

## Features

- **Interactive 24x16 Matrix Editor:** Click and drag to paint pixel art in real-time. The browser UI grid matches the hardware's 2:1 rectangular aspect ratio natively (What You See Is What You Get).
- **Instant Drag and Drop Image Converter:** Drop standard images (.png, .jpg, .webp) to automatically downscale and map them onto the 24x16 display grid.
- **Advanced Back-Converter / Decompressor:** Paste a raw RLE hex string (or an entire mosquitto_pub terminal command line) to unpack it back into fully editable grid pixels.
- **Custom Real-Time RLE Compiler:** Generates compact data streams using a 1-byte count + 3-byte RGB structure.
- **Live MQTT Command Generator:** Auto-interpolates Host, Username, and Password fields directly into a standalone shell execution script.
- **Persistent Configuration Storage:** Saves your MQTT configuration variables locally using localStorage so you never have to re-type them.

---

## Project Structure

To run the application, ensure the following 3 core files are saved in the exact same directory. No local web server or terminal configuration is required to execute the workspace:

```text
├── rle_icon_generator.html   # Main application interface and engine
├── style.css                 # Dark-mode workspace and fluid 2:1 grid layout styling
└── app.js                    # Core logic (drawing, RLE compression/decompression, image scaling)
```

---

## Understanding the RLE Hex Format

The compiler uses a simple 4-Byte Packet Compression Logic optimized for hardware components (like ESP32/Arduino display drivers):
- **1st Byte (2 Hex Characters):** The Run Count (Repetition depth up to 255 / Hex FF).
- **2nd, 3rd, 4th Byte (6 Hex Characters):** The 24-bit RGB Color Code in UpperCase Hex format.

### Example:
- **`1E000000`** translates to `1E` (30 in decimal) pixels of `000000` (Pure Black).
- **`0CFFFFFF`** translates to `0C` (12 in decimal) pixels of `FFFFFF` (Pure White).

---

## Technical Limitations and Pixel Aspect Ratio

While the drag-and-drop workflow is completely automated, converting standard graphics into a restrictive 24x16 grid with 2:1 rectangular pixels requires specific data handling considerations:

### 1. 2:1 Pixel Aspect Ratio (Physical 3:1 Matrix Screen)
- **The 2:1 Rule:** Each pixel on your physical LED matrix is twice as wide as it is tall (2w x 1h). 
- **Physical Aspect Ratio:** Although the grid resolution is 24x16 (a 3:2 index count), the physical shape of the running screen is 3:1 widescreen (24 x 2 : 16 x 1 = 48:16 = 3:1).
- **Image Distortion Warning:** If you drop a standard 3:2 square-pixel image into the converter, it will appear stretched horizontally by 200% when displayed on your physical hardware. For correct, un-distorted deployment, your source image needs to look squeezed horizontally before conversion.

### 2. Resolution and Forced Scaling
- **Strict Matrix Fit:** The engine forces any input image to fit a strict 24x16 matrix pixel array, regardless of its original size. Input images are directly mapped to the 24x16 color array indices without letterboxing or cropping. 

### 3. Downsampling Noise (Anti-Aliasing)
- **Bilinear Blur:** Browsers use smooth anti-aliasing algorithms when downscaling. If you upload a massive photo, fine lines, text, or intricate details will blur together into combined average color blocks.
- **Best Practice:** The converter works best on crisp, low-resolution pixel art templates designed natively for your hardware profile.

### 4. Color Depth and Transparency
- **Raw 24-bit Output:** The code captures exactly what the browser samples and outputs true 24-bit RGB Hex (000000 to FFFFFF). No palette quantization or color reduction is applied.
- **Forced Black Canvas Background:** The hidden rendering scratchpad canvas is pre-filled with pure black (#000000) right before printing your image over it. Transparent .png areas will merge into pure black.

### 5. RLE Run Length Multiplier Cap
- **255 Pixel Max Sequence Chunks:** A single run-length packet can only count up to 255 (FF in hex) before it must close out the sequence chunk and start a new packet string segment.
- **Byte Chunks:** Because your entire display consists of 384 pixels total (24 x 16 = 384), a completely solid image (like pure black or solid red) will always output exactly two packet clusters in the hex code text area instead of a single string line block.

---

## How to Use

### 1. Launching the App
Simply open rle_icon_generator.html in any modern web browser. It runs seamlessly directly off your filesystem (file:///) without local server overhead.

### 2. Configuration Setup
Enter your network credentials into the MQTT Configuration Block at the top of the interface. Modifying any character here updates the command line text output instantly.

### 3. Creating and Editing Icons

#### Method A: Drawing Manually
1. Select a color using the Brush Color picker input.
2. Left-click and hold your mouse button down while dragging across the 24x16 Matrix Grid Canvas.
3. Use the Clear (Black) button to instantly reset your workspace canvas to default solid black (1E000000).

#### Method B: Drag and Drop Converter
1. Take any compatible image file from your computer.
2. Drag and drop it into the dashed blue area labeled: "Drop an image here or click to upload".
3. The image processing engine will parse it into a clean 24x16 matrix representation.

#### Method C: Importing and Decoding Existing RLE Codes
1. Paste a raw RLE hex string into the "Raw RLE Hex Code / Import Stream" text box at the bottom.
2. Alternative: You can paste a full terminal command (e.g., mosquitto_pub ... -m "1E000000..."). The engine's Regex string parser will automatically strip the wrapper and extract the payload.
3. Click the green "Import and Decode to Grid" button. The matrix canvas will instantly render the icon, allowing you to manipulate and modify it manually.

### 4. Deploying the Payload
- Look at the bottom Mosquitto Pub Shell Command text area box.
- Click "Copy Command" to copy the generated script directly onto your clipboard.
- Open your terminal or bash prompt and paste the script line to publish your icon stream instantly to your matrix nodes over the led_matrix_node/display/icon topic.

---

## Pro-Tip for Perfect Rectangular Pixel Conversions

To design icons that display with perfect proportions on your 2:1 pixel aspect ratio hardware matrix:

1. Open your pixel art editor (e.g., Aseprite, Photoshop, or Piskel).
2. Create a canvas sized exactly 48x16 pixels (this matches the true physical aspect ratio of your screen).
3. Draw your artwork normally within this 48x16 space so that circles look circular and proportions look correct.
4. When finished, resize/scale the image down to exactly 24x16 pixels using Nearest Neighbor resampling (disable linear interpolation/smoothing).
5. The resulting image will look horizontally squished on your computer screen. Save this 24x16 squished image as a PNG.
6. Drag and drop this PNG into the generator app. The index mapping will be pixel-perfect, and when sent to your matrix display node, the 2:1 hardware pixels will stretch the image back out to its original, perfectly proportioned look!

---

## License
This tool is open-source and free to be customized for your embedded hardware and home automation projects.
