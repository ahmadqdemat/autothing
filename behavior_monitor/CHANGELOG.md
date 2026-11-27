# Changelog

## 1.1.0
- Notifications are built in: alerts go straight to your phones, no script needed.
- New `phones` setting: list phones by their name in the Home Assistant app (e.g. `S24 Ultra`); empty = every phone with the app. Names are looked up on every alert, so a phone that re-registers keeps working.
- `notify_script` is now optional (use it only if you want your own script).
- "Send test notification" button on the status page.

## 1.0.1
- Pressing a notification button now removes the notification from the phone.
- Pressing "This is normal" more than once no longer creates duplicate rules (existing duplicates are cleaned up).


## 1.0.0
- First release.
- Learns each device's usage by hour of day, separately for "someone home" and "everyone away".
- Three alert types: unusual hour, on too long, on while everyone is away.
- Notification buttons: Turn off, Ignore, This is normal.
- Learns from InfluxDB (weeks of history) or Home Assistant's own history (about 10 days).
- Status page with a 7-day test replay, rules and a usage heatmap per device.
- `sensor.behavior_monitor` with the number of unusual devices right now.
