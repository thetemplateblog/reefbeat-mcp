"""End-to-end test: launches server.py over stdio (as Claude does) against the
mock devices and checks every tool, including exactly what gets sent.

    python tests/test_server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).parent))
from mock_devices import MockDevice  # noqa: E402

SERVER = str(Path(__file__).parent.parent / "server.py")
passed = 0
failed: list[str] = []


def check(name: str, cond: bool, detail: Any = "") -> None:
    global passed
    if cond:
        passed += 1
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")


def params(dose: str, ato: str, **extra: str) -> StdioServerParameters:
    env = {"PATH": os.environ["PATH"], "REEFDOSE_HOST": dose, "REEFATO_HOST": ato, **extra}
    return StdioServerParameters(command=sys.executable, args=[SERVER], env=env)


async def call(client: Client, tool: str, **args: Any) -> tuple[bool, Any]:
    """Return (is_error, data). data is the structured result, or error text."""
    res = await client.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    if res.is_error:
        return True, text
    data = res.structured_content
    if data is None:
        data = json.loads(text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return False, data


async def read_only(dose: MockDevice, ato: MockDevice) -> None:
    print("read-only server")
    async with Client(params(dose.host, ato.host)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        expected = {
            "list_devices", "discover_devices", "dose_status", "dose_head_settings",
            "dose_queue", "dose_history", "ato_status", "device_get", "dose_manual",
            "dose_set_daily_dose", "dose_set_schedule_enabled", "dose_set_container_volume",
            "ato_set_auto_fill", "ato_set_reservoir_volume", "ato_resume", "set_device_mode",
        }
        check("tool list", set(tools) == expected, set(tools) ^ expected)
        check("read tools flagged read-only", all(
            tools[n].annotations.read_only_hint for n in
            ("list_devices", "dose_status", "ato_status", "device_get", "dose_queue", "dose_history")
        ))
        check("write tools not flagged read-only", not any(
            tools[n].annotations.read_only_hint for n in
            ("dose_manual", "dose_set_daily_dose", "ato_set_auto_fill", "set_device_mode")
        ))

        err, d = await call(c, "list_devices")
        check("list_devices", not err and d["dose"]["model"] == "RSDOSE4" and d["ato"]["model"] == "RSATO+"
              and d["dose"]["reachable"] and d["writes_enabled"] is False, d)
        check("list_devices firmware+mode", d["dose"]["firmware"] == "3.0.1" and d["ato"]["mode"] == "auto", d)

        err, d = await call(c, "dose_status")
        check("dose_status heads", not err and len(d["heads"]) == 4, d)
        h1 = d["heads"][0]
        check("dose_status head 1", h1["supplement"] == "Nutrient A" and h1["daily_dose_ml"] == 7
              and h1["container_volume_ml"] == 500.0 and h1["schedule_enabled"] is True
              and h1["last_calibrated"] == "2025-06-07T11:37:32Z", h1)
        check("dose_status head 4 not set up", d["heads"][3]["state"] == "not-setup")

        err, d = await call(c, "dose_head_settings", head=2)
        check("dose_head_settings", not err and d["schedule"]["dd"] == 45, d)
        err, d = await call(c, "dose_head_settings", head=5)
        check("head out of range rejected", err and "between 1 and 4" in d, d)

        err, d = await call(c, "dose_queue")
        check("dose_queue", not err and d["queued"][0] == {"time": "12:00", "head": "NA", "volume_ml": 2.333333, "type": "Auto"}
              and d["seconds_between_doses"] == 600, d)

        err, d = await call(c, "dose_history")
        check("dose_history", not err and d["days"][0] == {"date": "2025-11-21", "dosed_ml": {"1": 2.3, "2": 15.0, "3": 13.4, "4": 0}}
              and d["heads"]["1"] == "Nutrient A", d)

        err, d = await call(c, "ato_status")
        check("ato_status", not err and d["auto_fill"] is True and d["temperature_c"] == 25.21
              and d["reservoir"]["volume_left_ml"] == 12500 and d["needs_attention"] == []
              and d["pump"]["last_fill"] == "2025-06-29T17:43:35Z", d)

        err, d = await call(c, "device_get", target="ato", path="/configuration")
        check("device_get", not err and d["leak"]["sensor_enabled"] is True, d)
        err, d = await call(c, "device_get", target="ato", path="/../x")
        check("device_get rejects odd paths", err, d)
        err, d = await call(c, "device_get", target="ato", path="/nope")
        check("device_get 404 is an error", err and "HTTP 404" in d, d)

        # Previews work while read-only, and send nothing.
        err, d = await call(c, "dose_manual", head=1, volume_ml=2)
        check("manual dose preview", not err and d["status"] == "preview" and d["writes_enabled"] is False
              and d["request"] == {"method": "POST", "path": "/head/1/manual",
                                   "body": {"manual_dose_scheduled": True, "volume": 2}}
              and d["supplement"] == "Nutrient A", d)
        # Confirming while read-only is refused, for every write tool.
        for tool, args in [
            ("dose_manual", {"head": 1, "volume_ml": 2}),
            ("dose_set_daily_dose", {"head": 1, "daily_ml": 8}),
            ("dose_set_schedule_enabled", {"head": 1, "enabled": False}),
            ("dose_set_container_volume", {"head": 1, "volume_ml": 1000}),
            ("ato_set_auto_fill", {"enabled": False}),
            ("ato_set_reservoir_volume", {"volume_ml": 20000}),
            ("ato_resume", {}),
            ("set_device_mode", {"target": "ato", "mode": "off"}),
        ]:
            err, d = await call(c, tool, confirm=True, **args)
            check(f"read-only blocks {tool}", err and "read-only" in d, d)
        check("read-only server sent no writes", dose.writes == [] and ato.writes == [], (dose.writes, ato.writes))


async def writes(dose: MockDevice, ato: MockDevice) -> None:
    print("write-enabled server")
    async with Client(params(dose.host, ato.host, REEFBEAT_ALLOW_WRITES="1")) as c:
        # --- manual dose
        err, d = await call(c, "dose_manual", head=1, volume_ml=2.5)
        check("preview sends nothing", not err and d["status"] == "preview" and dose.writes == [], d)
        err, d = await call(c, "dose_manual", head=1, volume_ml=2.5, confirm=True)
        check("manual dose sent", not err and d["status"] == "sent" and d["manual_dosed_today_ml_now"] == 2.5, d)
        check("manual dose request", dose.writes == [
            {"method": "POST", "path": "/head/1/manual", "body": {"manual_dose_scheduled": True, "volume": 2.5}}
        ], dose.writes)
        dose.writes.clear()

        err, d = await call(c, "dose_manual", head=1, volume_ml=10.5, confirm=True)
        check("manual dose over limit refused", err and "at most 10 ml" in d and dose.writes == [], d)
        err, d = await call(c, "dose_manual", head=1, volume_ml=0, confirm=True)
        check("zero dose refused", err and dose.writes == [], d)
        err, d = await call(c, "dose_manual", head=1, volume_ml=-3, confirm=True)
        check("negative dose refused", err and dose.writes == [], d)
        err, d = await call(c, "dose_manual", head=4, volume_ml=1, confirm=True)
        check("dose on unconfigured head refused", err and "not set up" in d and dose.writes == [], d)

        dose.fail_next["/head/2/manual"] = 500
        err, d = await call(c, "dose_manual", head=2, volume_ml=1, confirm=True)
        check("device refusal reported", err and "refused" in d, d)
        check("failed dose sent exactly once", len(dose.writes) == 1, dose.writes)
        dose.writes.clear()

        dose.fail_next["/head/2/manual"] = "drop"
        err, d = await call(c, "dose_manual", head=2, volume_ml=1, confirm=True)
        check("unknown outcome warns against repeating", err and "may or may not" in d
              and "Do not repeat" in d, d)
        check("dropped dose sent exactly once (no retry)", len(dose.writes) == 1, dose.writes)
        dose.writes.clear()

        # --- daily dose
        err, d = await call(c, "dose_set_daily_dose", head=1, daily_ml=8)
        check("daily dose preview", not err and d["status"] == "preview" and d["current"] == {"daily_dose_ml": 7}
              and d["request"]["changed_fields"] == {"schedule": {"dd": 8}} and dose.writes == [], d)
        err, d = await call(c, "dose_set_daily_dose", head=1, daily_ml=8, confirm=True)
        check("daily dose set", not err and d["settings_now"]["daily_dose_ml"] == 8, d)
        w = dose.writes[0] if dose.writes else {}
        check("daily dose PUT keeps the rest of the settings", len(dose.writes) == 1 and w["method"] == "PUT"
              and w["path"] == "/head/1/settings" and w["body"]["schedule"]["dd"] == 8
              and w["body"]["schedule"]["intervals"] == [{"end": 1200, "mode": "regular", "nd": 3, "st": 480}]
              and w["body"]["supplement"]["name"] == "Nutrient A" and w["body"]["container_volume"] == 500.0, w)
        dose.writes.clear()
        err, d = await call(c, "dose_set_daily_dose", head=1, daily_ml=100.5, confirm=True)
        check("daily dose over limit refused", err and dose.writes == [], d)
        err, d = await call(c, "dose_set_daily_dose", head=1, daily_ml=7.5, confirm=True)
        check("fractional daily dose", not err and dose.writes[0]["body"]["schedule"]["dd"] == 7.5, d)
        dose.writes.clear()

        # bundled heads: head 2 refused; head 1 strips supplement fields
        dose.state["/dashboard"]["bundled_heads"] = True
        err, d = await call(c, "dose_set_daily_dose", head=2, daily_ml=40, confirm=True)
        check("bundled head 2 refused", err and "bundled" in d and dose.writes == [], d)
        err, d = await call(c, "dose_set_daily_dose", head=1, daily_ml=7, confirm=True)
        body = dose.writes[0]["body"] if dose.writes else {}
        check("bundled PUT strips supplement fields", not err and "supplement" not in body
              and "is_food_head" not in body and "food_delay" not in body and body["schedule"]["dd"] == 7, body)
        dose.state["/dashboard"]["bundled_heads"] = False
        dose.writes.clear()

        # --- schedule on/off
        err, d = await call(c, "dose_set_schedule_enabled", head=2, enabled=False, confirm=True)
        check("schedule paused", not err and d["settings_now"]["schedule_enabled"] is False
              and dose.writes[0]["body"]["schedule_enabled"] is False
              and dose.writes[0]["body"]["schedule"]["dd"] == 45, d)
        dose.writes.clear()

        # --- container volume
        err, d = await call(c, "dose_set_container_volume", head=1, volume_ml=1000, confirm=True)
        check("container volume set", not err and d["settings_now"]["container_volume_ml"] == 1000
              and d["before"] == {"container_volume_ml": 500.0}, d)
        dose.writes.clear()
        err, d = await call(c, "dose_set_container_volume", head=3, volume_ml=1000, confirm=True)
        check("container volume needs stock monitoring", err and "Stock monitoring is off" in d and dose.writes == [], d)
        err, d = await call(c, "dose_set_container_volume", head=1, volume_ml=20001, confirm=True)
        check("container volume over limit refused", err and dose.writes == [], d)

        # --- ATO
        err, d = await call(c, "ato_set_auto_fill", enabled=False)
        check("auto fill preview", not err and d["status"] == "preview" and ato.writes == [], d)
        err, d = await call(c, "ato_set_auto_fill", enabled=False, confirm=True)
        check("auto fill off", not err and d["auto_fill_now"] is False and ato.writes == [
            {"method": "PUT", "path": "/configuration", "body": {"auto_fill": False}}], (d, ato.writes))
        check("auto fill PUT left other config alone", ato.state["/configuration"]["leak"]["sensor_enabled"] is True
              and ato.state["/configuration"]["buzzer"]["enabled"] is True)
        err, d = await call(c, "ato_status")
        check("ato_status flags auto fill off", "Auto fill is turned off" in d["needs_attention"], d)
        ato.writes.clear()
        await call(c, "ato_set_auto_fill", enabled=True, confirm=True)
        ato.writes.clear()

        err, d = await call(c, "ato_set_reservoir_volume", volume_ml=20000, confirm=True)
        check("reservoir volume", not err and d["volume_left_ml_now"] == 20000 and ato.writes == [
            {"method": "POST", "path": "/update-volume", "body": {"volume": 20000}}], (d, ato.writes))
        ato.writes.clear()
        err, d = await call(c, "ato_set_reservoir_volume", volume_ml=200001, confirm=True)
        check("reservoir over limit refused", err and ato.writes == [], d)

        err, d = await call(c, "ato_resume", confirm=True)
        check("ato resume", not err and ato.writes == [{"method": "POST", "path": "/resume", "body": None}], (d, ato.writes))
        ato.writes.clear()

        # --- modes
        err, d = await call(c, "set_device_mode", target="ato", mode="maintenance", confirm=True)
        check("ato to maintenance", not err and d["mode_now"] == "maintenance"
              and [(w["method"], w["path"]) for w in ato.writes] == [("POST", "/maintenance")], (d, ato.writes))
        err, d = await call(c, "ato_status")
        check("ato_status flags maintenance", any("maintenance" in a for a in d["needs_attention"]), d)
        ato.writes.clear()
        err, d = await call(c, "set_device_mode", target="ato", mode="off", confirm=True)
        check("maintenance to off", not err and d["mode_now"] == "off"
              and [(w["method"], w["path"]) for w in ato.writes] == [("DELETE", "/maintenance"), ("POST", "/off")], (d, ato.writes))
        ato.writes.clear()
        err, d = await call(c, "set_device_mode", target="ato", mode="auto", confirm=True)
        check("off to auto", not err and d["mode_now"] == "auto"
              and [(w["method"], w["path"]) for w in ato.writes] == [("DELETE", "/off")], (d, ato.writes))
        ato.writes.clear()
        err, d = await call(c, "set_device_mode", target="ato", mode="auto", confirm=True)
        check("same mode is a no-op", not err and d["status"] == "unchanged" and ato.writes == [], d)
        err, d = await call(c, "set_device_mode", target="dose", mode="off", confirm=True)
        check("doser off", not err and d["mode_now"] == "off", d)
        await call(c, "set_device_mode", target="dose", mode="auto", confirm=True)


async def misconfigured(dose: MockDevice, ato: MockDevice) -> None:
    print("misconfigured servers")
    # Hosts swapped: every device tool must refuse rather than act on the wrong device.
    async with Client(params(ato.host, dose.host, REEFBEAT_ALLOW_WRITES="1")) as c:
        before = (list(dose.writes), list(ato.writes))
        err, d = await call(c, "dose_status")
        check("swapped hosts: dose_status refuses", err and "not a ReefDose" in d, d)
        err, d = await call(c, "ato_status")
        check("swapped hosts: ato_status refuses", err and "not a ReefATO+" in d, d)
        err, d = await call(c, "dose_manual", head=1, volume_ml=1, confirm=True)
        check("swapped hosts: no dose", err, d)
        err, d = await call(c, "ato_set_auto_fill", enabled=False, confirm=True)
        check("swapped hosts: no ato write", err, d)
        err, d = await call(c, "set_device_mode", target="ato", mode="off", confirm=True)
        check("swapped hosts: no mode change", err, d)
        check("swapped hosts sent nothing", (dose.writes, ato.writes) == before, (dose.writes, ato.writes))
        err, d = await call(c, "list_devices")
        check("swapped hosts visible in list_devices", not err and d["dose"]["is_expected_model"] is False, d)

    # Nothing configured / device unreachable.
    async with Client(params("", "127.0.0.1:9")) as c:
        err, d = await call(c, "dose_status")
        check("unset host explained", err and "REEFDOSE_HOST" in d, d)
        err, d = await call(c, "list_devices")
        check("list_devices with unset + dead hosts", not err and d["dose"] == {"configured": False, "set": "REEFDOSE_HOST"}
              and d["ato"]["reachable"] is False, d)
        err, d = await call(c, "ato_status")
        check("unreachable device explained", err and "Could not read" in d, d)
        err, d = await call(c, "discover_devices", subnet="8.8.8.0/24")
        check("discover refuses public networks", err and "private" in d, d)
        err, d = await call(c, "discover_devices", subnet="10.0.0.0/8")
        check("discover refuses huge networks", err and "too large" in d, d)
        err, d = await call(c, "discover_devices", subnet="127.0.0.0/30")
        check("discover runs", not err and d["devices"] == [], d)


async def main() -> int:
    for scenario in (read_only, writes, misconfigured):
        dose, ato = MockDevice("dose").start(), MockDevice("ato").start()
        try:
            await scenario(dose, ato)
        finally:
            dose.stop()
            ato.stop()
    print(f"\n{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
