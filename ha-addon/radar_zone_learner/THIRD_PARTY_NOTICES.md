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
- zomco/mmwave-engine (MIT): the dwell pass speed. Since P6 a detection of a track moving at 0.8 m/s or more is not counted
  for a dwell, bed or manual zone. Idea reference, no code used.
- P43YM/ld2450_zones (MIT): k-of-n zone entry. Since P6 a track enters a zone after at least 3 of its last 10 detections
  inside the zone. Idea reference, no code used.
- TillFleisch/ESPHome-HLK-LD2450 (MIT): zone margin. Since P6 the zone area is the stored cell mask grown by a margin of 25 cm
  (adjustable per zone, -50 to +100 cm). Idea reference, no code used.
- paulin011/hlk_radar_fusion (MIT): still-presence fusion. Since P6 the second radar (LD2412) may only shorten a held zone
  (early clear after 60 s of absence, for zones where it agreed at least 80 % of the time); it never extends a hold. Idea
  reference, no code used.

P7 (0.7.0: drift, moves, retire, radar moved, tuning) uses no new third-party code and no new idea from another project; it
adds no dependency (the app still needs only the Python standard library and paho-mqtt 2.1.0).

If code from any of these projects is ever ported into this app, its MIT copyright and licence text
will be added to this file.
