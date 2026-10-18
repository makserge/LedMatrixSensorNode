# Home Assistant Washing Machine Automation Guide

This document describes how to configure automated status tracking, visual icon updates, and recurring audio notifications for a washing machine in Home Assistant.

---

## 1. Prerequisites (Input Boolean Helper)

To keep track of whether the washing machine is currently operating, you must define an input boolean helper. 

Add the following configuration to your `configuration.yaml` file and restart Home Assistant, or create it directly in the user interface via **Settings → Devices & Services → Helpers → Create Helper → Toggle**.

```yaml
input_boolean:
  washing_machine_running:
    name: Washing Machine Running
    icon: mdi:washing-machine
```

---

## 2. Automations Code

Add the following code block to your `automations.yaml` file. It includes two automated workflows: one for starting cycle detection and one for managing the periodic stop notification routine.

```yaml
- alias: "Washing Machine - Start Detected"
  id: washing_machine_start
  trigger:
    - platform: state
      entity_id: sensor.tasmota_energy_power
  condition:
    - condition: template
      value_template: "{{ states('sensor.tasmota_energy_power') | float(0) > 10 }}"
    - condition: state
      entity_id: input_boolean.washing_machine_running
      state: "off"
  action:
    - action: input_boolean.turn_on
      target:
        entity_id: input_boolean.washing_machine_running

    # Fixed "start washing" RLE icon
    - action: mqtt.publish
      data:
        topic: "led_matrix_node/display/icon"
        payload: "050000000D00FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000D00FF000B0000000100FF000B0000000100FF000B0000000100FF00050000000100FF00050000000100FF000B0000000100FF00040000000100FF00010000000100FF00040000000100FF000B0000000100FF00030000000100FF00030000000100FF00030000000100FF000B0000000100FF00020000000100FF00050000000100FF00020000000100FF000B0000000100FF00030000000100FF00030000FF0100FF00030000000100FF000B0000000100FF00040000000100FF00010000FF0100FF00040000000100FF000B0000000100FF00050000000100FF00050000000100FF000B0000000100FF000B0000000100FF000B0000000D00FF0006000000"

    # Beep 10 times with 250ms delay
    - repeat:
        count: 10
        sequence:
          - action: mqtt.publish
            data:
              topic: "led_matrix_node/beeper"
              payload: "1"
          - delay:
              milliseconds: 250

- alias: "Washing Machine - Stop Detected"
  id: washing_machine_stop
  trigger:
    - platform: numeric_state
      entity_id: sensor.tasmota_energy_power
      below: 10
      for:
        minutes: 2
  condition:
    - condition: state
      entity_id: input_boolean.washing_machine_running
      state: "on"
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.washing_machine_running

    # Repeat the entire notification loop 5 times
    - repeat:
        count: 5
        sequence:
          # Wait 1 minute for all repeats EXCEPT the very first run
          - if:
              - condition: template
                value_template: "{{ repeat.index > 1 }}"
            then:
              - delay:
                  minutes: 1

          # "stop washing" RLE icon
          - action: mqtt.publish
            data:
              topic: "led_matrix_node/display/icon"
              payload: "010000000D00FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF00080000000200FF00010000000D00FF00070000000200FF00020000000100FF000B0000000100FF00070000000200FF00020000000100FF00050000000100FF00050000000100FF00060000000200FF00030000000100FF00040000000100FF00010000000100FF00040000000100FF00060000000200FF00030000000100FF00030000000100FF00030000000100FF00030000000100FF00020000000100FF00020000000200FF00040000000100FF00020000000100FF00050000000100FF00020000000100FF00020000000200FF00010000000200FF00040000000100FF00030000000100FF00030000FF0100FF00030000000100FF00030000000300FF00050000000100FF00040000000100FF00010000FF0100FF00040000000100FF00030000000300FF00050000000100FF00050000000100FF00050000000100FF00040000000100FF00060000000100FF000B0000000100FF000B0000000D00FF000A000000"

          # Beep 10 times with 250ms delay
          - repeat:
              count: 10
              sequence:
                - action: mqtt.publish
                  data:
                    topic: "led_matrix_node/beeper"
                    payload: "1"
                - delay:
                    milliseconds: 250
```

---

## 3. How It Works

### Start Detected Automation
1. **Trigger:** Fires whenever `sensor.tasmota_energy_power` undergoes a state change.
2. **Condition:** Checks if the measured energy draw passes above **10W** and confirms that the track helper state is currently `off` (preventing multi-triggering).
3. **Action:** Flips the tracking toggle to `on`, transmits an initialization bitmap payload via MQTT to display a "Start" graphic icon on your physical LED matrix screen, and emits 10 rapid alert beeps.

### Stop Detected Automation
1. **Trigger:** Activates once the power drop settles and stays below **10W** continuously for **2 minutes** (signaling a completed execution or standby profile).
2. **Condition:** Ensures the automation executes exclusively if the machine state helper registers as active (`on`).
3. **Action Sequence:** 
   * Flips the tracker helper back to `off`.
   * Enters an engineered `repeat` loop configured to run **5 times**.
   * On every step starting from the second round (`repeat.index > 1`), it holds execution for a **1-minute** delay phase. This design naturally leaves out an unneeded trailing delay window at the conclusion of the 5th final loop round.
   * Transmits the "Stop" RLE icon data pattern over MQTT to flash the active screen state.
   * Runs a nested sequential loop to chime a 10-beep audio profile directly into your physical beeper module.