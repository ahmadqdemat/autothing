# Behavior Monitor

Learns how each light, switch, AC, TV and cover is normally used — by hour of day,
separately for when someone is home and when everyone is away — and sends a phone
alert when something is unusual:

- **Unusual hour**: on for 20+ minutes at an hour when it is on less than 5% of the time.
- **On too long**: on much longer than its usual run (1.5 × its 95th-percentile run, at least 1 hour).
- **On while away**: everyone has been away 20+ minutes and the device is normally off when nobody is home.

Always-on devices (on more than 80% of the time, like a fridge) are never flagged.

Full user guide: docs/index.html in the repository.

Each alert has buttons: **Turn off**, **Ignore** (this time only) and **This is normal**
(stops that alert for that device at that time of day). Rules can be removed on the add-on page.

## Notifications
Alerts go straight to your phones through the Home Assistant app — no script needed.
Set `phones` to the phones that should get alerts, using each phone's name as shown in the
Home Assistant app (Settings → Companion app), e.g. `S24 Ultra`. Leave it empty to alert every
phone with the app. Use the **Send test notification** button on the app's page to check.

Advanced: set `notify_script` to send through your own script instead (it receives `title`,
`message`, `tag` and `actions`).

## History
With `influx_password` set, it learns from InfluxDB (`learn_days`, default 42 days).
Without it, it uses Home Assistant's own history (about 10 days). It relearns every night after 03:00.

The add-on page shows what is unusual now, a test replay of the last 7 days, the rules,
and the learned usage of every device. The sensor `sensor.behavior_monitor` shows the
number of unusual devices right now.
