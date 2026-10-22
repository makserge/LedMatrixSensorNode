# Third-party notices

## Installed into the app image at build time

### paho-mqtt 2.1.0

- Licence: EPL-2.0 OR BSD-3-Clause (Eclipse Public License 2.0 or Eclipse Distribution License 1.0,
  which is the BSD-3-Clause licence), as stated on PyPI.
- How it is used: `requirements.txt` pins `paho-mqtt==2.1.0` with its PyPI sha256 hashes. The
  Supervisor installs it with `pip` while it builds the image on the Home Assistant host.
- Not contained in this folder. Whoever distributes a built image of this app must ship the paho-mqtt
  licence file with it.

### Home Assistant base image

`ghcr.io/home-assistant/base-python:3.13-alpine3.24` is pulled by the Supervisor when it builds the
image. Its own licences apply to the image, not to this folder.

## Ideas credited (MIT licensed projects)

The learner uses ideas from the following projects. No code from these projects is included.

- PeterkoCZ91/HLK-LD2450-security (MIT): 3×3 association of radar detections to tracks.
- zomco/mmwave-engine (MIT): alpha-beta filter with alpha 0.35 and beta 0.08.
- P43YM/ld2450_zones (MIT): k-of-n zone entry. Not used yet (planned for phase P6).
- TillFleisch/ESPHome-HLK-LD2450 (MIT): zone margin. Not used yet (planned for phase P6).
- paulin011/hlk_radar_fusion (MIT): still-presence fusion. Not used yet (planned for phase P6).

If code from any of these projects is ever ported into this app, its MIT copyright and licence text
will be added to this file.
