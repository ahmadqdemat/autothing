# Autothing – Home Assistant apps

A collection of Home Assistant apps (formerly add-ons) by Autothing.

## Install
Settings → **Apps** → **App store** → ⋮ → **Repositories** → add
`https://github.com/ahmadqdemat/autothing` → **Add** → close → ⋮ → **Check for updates**.

These are apps, not HACS integrations — add the repository in the App store, not in HACS.

## Apps
| App | What it does | Docs |
|---|---|---|
| **Behavior Monitor** | Learns how your devices are normally used and alerts you when something is unusual, with Turn off / Ignore / This is normal buttons | [User guide](behavior_monitor/docs/index.html) · [DOCS](behavior_monitor/DOCS.md) |
| **Jerusalem Parking Tickets** | Active Jerusalem municipality parking tickets (email code read from Gmail) | [DOCS](jerusalem_parking/DOCS.md) |
| **Kvish 6 Invoices** | Open Highway 6 toll invoices | [DOCS](kvish6_invoices/DOCS.md) |
| **JDECo Electricity Bills** | Unpaid electricity bills and last meter reading (login token renewed automatically) | [DOCS](jdeco_bills/DOCS.md) |
| **Mentor** | AI tutor that guides students without giving away answers | [README](mentor/README.md) |
| **Debater** | AI decision-support debate | [README](debater/README.md) |
| **Device Power Cost Tracker** | Device runtime, energy use and electricity cost, written to InfluxDB | [README](device_cost_tracker/README.md) |

The bill and ticket apps are read-only: they never pay, appeal or submit anything.
Each app's setup is also shown in its **Documentation** tab in Home Assistant.

## Adding or changing an app
Each app is a folder with `config.yaml` (or `config.json`), `Dockerfile` and its code.
Every change bumps `version` and adds an entry to the app's `CHANGELOG.md`
(Home Assistant shows it in the update dialog).
