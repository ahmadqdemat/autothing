# Changelog

## 1.0.0
- First release.
- Learns each device's usage by hour of day, separately for "someone home" and "everyone away".
- Three alert types: unusual hour, on too long, on while everyone is away.
- Notification buttons: Turn off, Ignore, This is normal.
- Learns from InfluxDB (weeks of history) or Home Assistant's own history (about 10 days).
- Status page with a 7-day test replay, rules and a usage heatmap per device.
- `sensor.behavior_monitor` with the number of unusual devices right now.
