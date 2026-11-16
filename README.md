# reefbeat-mcp

A local MCP server for a Red Sea **ReefDose** and **ReefATO+**. It talks to the
devices' own HTTP API on your home network: no Red Sea cloud login, nothing
leaves the LAN.

Unofficial. Red Sea does not publish this API. The endpoints follow the
open-source Home Assistant integration
[Elwinmage/ha-reefbeat-component](https://github.com/Elwinmage/ha-reefbeat-component).

**Status:** every tool passes an end-to-end test against simulated devices
(`tests/`). It has not yet been run against real hardware, so start read-only.

## What it can do

Read (always available):

| Tool | What you get |
| --- | --- |
| `list_devices` | Both devices: address, model, firmware, mode, reachable or not |
| `discover_devices` | Scans your network for ReefBeat devices and their IPs |
| `dose_status` | Per head: supplement, daily dose, dosed today, container left, days of stock |
| `dose_head_settings` | One head's full settings and schedule |
| `dose_queue` | Doses still queued for today |
| `dose_history` | Volume dosed per head per day, last week or so |
| `ato_status` | Mode, auto fill, water level, temperature, reservoir, fills, leak sensor, anything needing attention |
| `device_get` | Any raw read-only endpoint |

Change (off until you enable it):

| Tool | What it does |
| --- | --- |
| `dose_manual` | One-off dose from a head |
| `dose_set_daily_dose` | Change a head's daily dose |
| `dose_set_schedule_enabled` | Pause or resume a head's schedule |
| `dose_set_container_volume` | Record a refill |
| `ato_set_auto_fill` | Turn top-off on or off |
| `ato_set_reservoir_volume` | Record a reservoir refill |
| `ato_resume` | Resume after the ATO stopped itself |
| `set_device_mode` | auto / off / maintenance, either device |

Not included: head calibration, priming, supplement setup, and ATO manual
fill. Those are hands-on jobs better done in the ReefBeat app.

## Safety

- **Read-only by default.** Nothing changes unless `REEFBEAT_ALLOW_WRITES=1`.
- **Preview first.** Every change tool returns a preview unless called with
  `confirm=true`.
- **Dose limits.** A manual dose is capped at 10 ml and a daily dose at 100 ml
  unless you change `REEFDOSE_MAX_MANUAL_ML` / `REEFDOSE_MAX_DAILY_ML`.
- **No double dosing.** A manual dose is sent once and never retried. If the
  outcome is unknown the tool says so and tells Claude to check before
  dosing again.
- **Wrong device check.** Each tool confirms the address really is a ReefDose
  or ReefATO+ before doing anything.

## Setup (Mac)

1. Install uv: `brew install uv`, then note the output of `which uv`.
2. Put this folder somewhere permanent, e.g. `~/reefbeat-mcp`.
3. Find the device IPs: your router's client list, or set the server up with
   the hosts left empty and ask Claude to "discover my ReefBeat devices".
   Give both devices a fixed IP (DHCP reservation) in your router.
4. In Claude Desktop: menu bar **Claude > Settings... > Developer > Edit
   Config**, and add:

```json
{
  "mcpServers": {
    "reefbeat": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--script", "/Users/YOU/reefbeat-mcp/server.py"],
      "env": {
        "REEFDOSE_HOST": "192.168.1.50",
        "REEFATO_HOST": "192.168.1.51"
      }
    }
  }
}
```

   Use the path `which uv` printed, and absolute paths throughout. Quit Claude
   Desktop completely and reopen it.

5. Ask: "list my reef devices". Once that looks right and you want Claude to
   be able to make changes, add `"REEFBEAT_ALLOW_WRITES": "1"` to `env` and
   restart again.

Claude Code instead:

```bash
claude mcp add --env REEFDOSE_HOST=192.168.1.50 --env REEFATO_HOST=192.168.1.51 \
  --transport stdio reefbeat -- uv run --script ~/reefbeat-mcp/server.py
```

The server has to run on a computer on the same network as the devices.

## A page to look at: gui.py

`gui.py` is a small local web page for the doser and the ATO. For the ATO it shows sump
temperature and water level, today's top-offs, the reservoir, and auto fill on/off
(`--ato 192.168.1.51`, or set `REEFATO_HOST`; leave both out to hide it). For the doser: a measuring cylinder per
head, today's doses, a week of history, and buttons to change the daily dose,
dose now, record a refill, or pause a head. No installs needed.

```bash
python3 gui.py 192.168.1.50                    # opens http://127.0.0.1:8765
python3 gui.py 192.168.1.50 --ato 192.168.1.51  # doser and ATO
python3 gui.py --read-only                     # look, don't touch (addresses from REEFDOSE_HOST / REEFATO_HOST)
```

**Tube colors.** Red Sea sells two 4-color tube sets: blue-green (purple, blue,
blue-green, green) and red-yellow (pink, red, orange, yellow). Click "Tube
colors" on the page to pick a set or choose a color per head; the choice is
saved in `reefdose-bench.json` next to `gui.py`. From the command line:

```bash
python3 gui.py --tubes red-yellow
python3 gui.py --tubes purple,pink,green,red   # one color per head, in head order
```

The on-screen colors are approximations, not official Red Sea swatches.

It only answers on this computer, asks before every change, and uses the same
dose limits as the MCP server. It runs on its own; the MCP server does not
need to be running.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `REEFDOSE_HOST` | none | IP of the ReefDose |
| `REEFATO_HOST` | none | IP of the ReefATO+ |
| `REEFBEAT_ALLOW_WRITES` | off | `1` allows change tools to act |
| `REEFDOSE_MAX_MANUAL_ML` | 10 | Largest single manual dose |
| `REEFDOSE_MAX_DAILY_ML` | 100 | Largest daily dose a head can be set to |

## Try it without touching the tank

```bash
uv run --with "mcp>=2,<3" --with httpx python tests/test_server.py   # full test run
python3 tests/test_gui.py                                            # test the Reef bench page
python3 tests/mock_devices.py                                        # fake devices to point the server at
```

## Troubleshooting

- Logs: `~/Library/Logs/Claude/mcp-server-reefbeat.log`.
- "Could not read ... from the device": check the IP, and that the Mac and
  the device are on the same network (some routers isolate IoT/guest Wi-Fi).
- A device answers slowly while the ReefBeat app is open on it; retry.

## License

MIT. See [LICENSE](LICENSE). Not affiliated with or endorsed by Red Sea.
