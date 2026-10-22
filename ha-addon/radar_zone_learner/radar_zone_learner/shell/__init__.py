"""I/O shell of the radar zone learner app: MQTT, discovery, options, persistence, health, PNG.

Every module here imports without paho-mqtt; paho is imported lazily by ``mqtt_io`` when the app
starts its MQTT client.
"""
