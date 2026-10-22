#!/command/with-contenv bashio
# shellcheck shell=bash
set -e

# bashio prints the text "null" for an option or a service key that is not set. It is no value: a password
# "null" would be sent to the broker as a login.
no_null() {
    if [ "$1" = "null" ]; then
        printf ''
    else
        printf '%s' "$1"
    fi
}

if bashio::config.has_value 'mqtt_username'; then
    export RZL_MQTT_HOST="$(bashio::config 'mqtt_host' 'core-mosquitto')"
    export RZL_MQTT_PORT="$(bashio::config 'mqtt_port' '1883')"
    export RZL_MQTT_USERNAME="$(bashio::config 'mqtt_username')"
    export RZL_MQTT_PASSWORD="$(no_null "$(bashio::config 'mqtt_password')")"
    export RZL_MQTT_SSL="false"
    bashio::log.info "Using the MQTT login from the app options"
elif bashio::services.available "mqtt"; then
    export RZL_MQTT_HOST="$(bashio::services mqtt 'host')"
    export RZL_MQTT_PORT="$(bashio::services mqtt 'port')"
    export RZL_MQTT_USERNAME="$(no_null "$(bashio::services mqtt 'username')")"
    export RZL_MQTT_PASSWORD="$(no_null "$(bashio::services mqtt 'password')")"
    export RZL_MQTT_SSL="$(bashio::services mqtt 'ssl')"
    bashio::log.info "Using the Supervisor MQTT service"
else
    bashio::exit.nok "No MQTT service: start the Mosquitto broker app, or set mqtt_host, mqtt_username and mqtt_password in the app options"
fi

cd /app
exec python3 -u -B -m radar_zone_learner
