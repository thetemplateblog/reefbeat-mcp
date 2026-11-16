#!/usr/bin/env python3
"""Reef bench: a small local web page for your Red Sea ReefDose and ReefATO+.

    python3 gui.py 192.168.1.50     # the doser's address (or set REEFDOSE_HOST)
    python3 gui.py 192.168.1.50 --ato 192.168.1.51   # add the ATO (or set REEFATO_HOST)
    python3 gui.py --read-only      # look, don't touch
    python3 gui.py --tubes red-yellow            # Red Sea's red-yellow tube set
    python3 gui.py --tubes purple,pink,green,red # or one color per head

Then it opens http://127.0.0.1:8765 in your browser. No installs needed
(Python standard library only). It talks to the doser over your home
network, the same way the MCP server does, and is only reachable from
this computer.

Tube colors can also be changed on the page ("Tube colors"); the choice is
kept in reefdose-bench.json next to this file.

Limits (same environment variables as the MCP server):
  REEFDOSE_MAX_MANUAL_ML   largest one-off dose (default 10)
  REEFDOSE_MAX_DAILY_ML    largest daily dose (default 100)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PORT = 8765
TOKEN = secrets.token_urlsafe(24)

HARD_MAX_DOSE_ML = 300.0
MAX_CONTAINER_ML = 20_000
MAX_RESERVOIR_ML = 200_000


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        sys.exit(f"{name} must be a number")
    if value <= 0:
        sys.exit(f"{name} must be greater than 0")
    return min(value, HARD_MAX_DOSE_ML)


MAX_MANUAL_ML = _env_float("REEFDOSE_MAX_MANUAL_ML", 10.0)
MAX_DAILY_ML = _env_float("REEFDOSE_MAX_DAILY_ML", 100.0)

# Red Sea sells two 4-color ReefDose tube sets. The color names are Red
# Sea's; the hex values are screen approximations, not official swatches.
TUBES: dict[str, dict[str, str]] = {
    "purple": {"label": "Purple", "hex": "#7d52bd", "set": "blue-green"},
    "blue": {"label": "Blue", "hex": "#2f72d6", "set": "blue-green"},
    "blue-green": {"label": "Blue-green", "hex": "#18a79c", "set": "blue-green"},
    "green": {"label": "Green", "hex": "#4dab3c", "set": "blue-green"},
    "pink": {"label": "Pink", "hex": "#e45f9f", "set": "red-yellow"},
    "red": {"label": "Red", "hex": "#d9382f", "set": "red-yellow"},
    "orange": {"label": "Orange", "hex": "#f08a1c", "set": "red-yellow"},
    "yellow": {"label": "Yellow", "hex": "#e6c11a", "set": "red-yellow"},
}
TUBE_SETS = {
    "blue-green": ["purple", "blue", "blue-green", "green"],
    "red-yellow": ["pink", "red", "orange", "yellow"],
}
SETTINGS_PATH = Path(__file__).with_name("reefdose-bench.json")

CONFIG: dict[str, Any] = {"host": "", "ato_host": "", "read_only": False, "tubes": list(TUBE_SETS["blue-green"])}


def parse_tubes(value: Any) -> list[str]:
    """Accept a set name, a comma list, or a list of color names (one per head)."""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in TUBE_SETS:
            return list(TUBE_SETS[key])
        value = [part.strip().lower() for part in key.split(",") if part.strip()]
    if not isinstance(value, list) or not 1 <= len(value) <= 4 or any(v not in TUBES for v in value):
        raise ValueError(
            "Tube colors must be a set (blue-green or red-yellow) or up to four of: " + ", ".join(TUBES)
        )
    # Fill any heads not named from the blue-green set.
    return value + TUBE_SETS["blue-green"][len(value):]


def load_settings() -> None:
    try:
        CONFIG["tubes"] = parse_tubes(json.loads(SETTINGS_PATH.read_text()).get("tubes"))
    except (OSError, ValueError, AttributeError):
        pass  # no saved choice yet, or an unreadable one: keep the default


def save_settings() -> bool:
    try:
        SETTINGS_PATH.write_text(json.dumps({"tubes": CONFIG["tubes"]}, indent=2) + "\n")
        return True
    except OSError:
        return False
_pool = ThreadPoolExecutor(max_workers=3)  # the doser is a small microcontroller
_write_lock = threading.Lock()


class DeviceError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Talking to the doser
# --------------------------------------------------------------------------


def _request(host: str, method: str, path: str, payload: Any = None, timeout: float = 10.0) -> Any:
    url = f"http://{host}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def device_get(path: str, optional: bool = False, host: str | None = None, name: str = "doser") -> Any:
    host = host or CONFIG["host"]
    last = "no answer"
    for attempt in range(3):
        if attempt:
            time.sleep(1.0)
        try:
            return _request(host, "GET", path)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (400, 404):
                break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = str(getattr(exc, "reason", exc))
    if optional:
        return None
    raise DeviceError(
        f"The {name} at {host} did not answer ({last}). Check that it is powered and on Wi-Fi."
    )


def device_send(method: str, path: str, payload: Any, once: bool = False,
                host: str | None = None, name: str = "doser") -> None:
    """Send a change. `once` requests (a manual dose) are never retried."""
    host = host or CONFIG["host"]
    attempts = 1 if once else 2
    last = "no answer"
    reached = False
    for attempt in range(attempts):
        if attempt:
            time.sleep(1.0)
        try:
            _request(host, method, path, payload, timeout=20.0)
            return
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 or exc.code == 503:
                raise DeviceError(
                    f"The {name} refused the change (HTTP {exc.code}). Nothing was changed.", 502
                ) from None
            # Any other server error: the device got the request and may have acted on it.
            reached = True
            last = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last = str(exc.reason)
            if not isinstance(exc.reason, ConnectionRefusedError):
                reached = True
        except (OSError, TimeoutError) as exc:
            reached = True
            last = str(exc)
    if reached and once:
        raise DeviceError(
            f"The dose was sent but the doser did not confirm it ({last}). It may or may not "
            "have run: check today's total before dosing again."
        )
    if reached:
        raise DeviceError(
            f"The change to the {name} at {host} did not go through cleanly ({last}). It may or "
            "may not have been applied: check the page after it refreshes before trying again."
        )
    raise DeviceError(f"Could not reach the {name} at {host} ({last}). Nothing was sent.")


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _num(value: float) -> int | float:
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def head_count(info: Any, dash: Any) -> int:
    if not isinstance(info, dict) or not isinstance(dash, dict):
        raise DeviceError(f"Unexpected answer from the doser at {CONFIG['host']}.", 502)
    model = str(info.get("hw_model", ""))
    if not model.startswith("RSDOSE"):
        raise DeviceError(
            f"{CONFIG['host']} is a {model or 'different device'}, not a ReefDose.", 502
        )
    if model.endswith("2"):
        return 2
    if model.endswith("4"):
        return 4
    return len(dash.get("heads", {}))


def read_status() -> dict[str, Any]:
    f_info = _pool.submit(device_get, "/device-info")
    f_dash = _pool.submit(device_get, "/dashboard")
    f_mode = _pool.submit(device_get, "/mode", True)
    f_fw = _pool.submit(device_get, "/firmware", True)
    f_queue = _pool.submit(device_get, "/dosing-queue", True)
    f_log = _pool.submit(device_get, "/daily-log", True)
    info, dash = f_info.result(), f_dash.result()
    count = head_count(info, dash)
    f_heads = [_pool.submit(device_get, f"/head/{h}/settings", True) for h in range(1, count + 1)]
    mode, fw, queue, log = f_mode.result(), f_fw.result(), f_queue.result(), f_log.result()

    days = []
    for entry in (log or {}).get("daily", []) if isinstance(log, dict) else []:
        if isinstance(entry, dict):
            days.append({"date": _iso(entry.get("date")), "ml": {k: v for k, v in entry.items() if k != "date"}})

    heads = []
    for h in range(1, count + 1):
        d = dash.get("heads", {}).get(str(h), {})
        s = f_heads[h - 1].result()
        s = s if isinstance(s, dict) else {}
        supplement = s.get("supplement") or {}
        heads.append(
            {
                "head": h,
                "supplement": d.get("supplement") or supplement.get("name") or f"Head {h}",
                "short_name": supplement.get("short_name"),
                "state": d.get("state"),
                "schedule_enabled": s.get("schedule_enabled"),
                "daily_ml": d.get("daily_dose", (s.get("schedule") or {}).get("dd")),
                "doses_per_day": d.get("daily_doses"),
                "doses_done": d.get("doses_today"),
                "auto_today_ml": d.get("auto_dosed_today") or 0,
                "manual_today_ml": d.get("manual_dosed_today") or 0,
                "container_ml": s.get("container_volume"),
                "stock_monitoring": bool(s.get("slm")),
                "remaining_days": d.get("remaining_days"),
                "recalibrate": bool(d.get("recalibration_required")),
                "history": [{"date": day["date"], "ml": day["ml"].get(str(h), 0)} for day in days],
            }
        )

    queued = []
    for item in queue if isinstance(queue, list) else []:
        if isinstance(item, dict) and isinstance(item.get("time"), (int, float)):
            t = int(item["time"]) % 86400
            queued.append(
                {
                    "time": f"{t // 3600:02d}:{t % 3600 // 60:02d}",
                    "head": item.get("head"),
                    "ml": item.get("volume"),
                    "type": item.get("dose_type"),
                }
            )

    return {
        "host": CONFIG["host"],
        "name": info.get("name"),
        "model": info.get("hw_model"),
        "firmware": fw.get("version") if isinstance(fw, dict) else None,
        "mode": mode.get("mode") if isinstance(mode, dict) else None,
        "battery": dash.get("battery_level"),
        "bundled": bool(dash.get("bundled_heads")),
        "read_only": CONFIG["read_only"],
        "tubes": CONFIG["tubes"],
        "palette": TUBES,
        "tube_sets": TUBE_SETS,
        "limits": {"manual_ml": MAX_MANUAL_ML, "daily_ml": MAX_DAILY_ML, "container_ml": MAX_CONTAINER_ML,
                   "reservoir_ml": MAX_RESERVOIR_ML},
        "heads": heads,
        "queue": queued,
        "ato": read_ato(),
    }


# --------------------------------------------------------------------------
# ReefATO+
# --------------------------------------------------------------------------


def _ato_get(path: str, optional: bool = False) -> Any:
    return device_get(path, optional, host=CONFIG["ato_host"], name="ATO")


def _check_ato() -> None:
    if not CONFIG["ato_host"]:
        raise DeviceError("No ATO address is set.", 400)
    model = str((_ato_get("/device-info") or {}).get("hw_model", ""))
    if not model.startswith("RSATO"):
        raise DeviceError(f"{CONFIG['ato_host']} is a {model or 'different device'}, not a ReefATO+.", 502)


def read_ato() -> dict[str, Any] | None:
    """ATO status, or {"error": ...} so an ATO problem never hides the doser."""
    host = CONFIG["ato_host"]
    if not host:
        return None
    try:
        f_info = _pool.submit(_ato_get, "/device-info")
        f_dash = _pool.submit(_ato_get, "/dashboard")
        f_conf = _pool.submit(_ato_get, "/configuration", True)
        f_mode = _pool.submit(_ato_get, "/mode", True)
        f_fw = _pool.submit(_ato_get, "/firmware", True)
        info, dash = f_info.result(), f_dash.result()
        conf, mode, fw = f_conf.result(), f_mode.result(), f_fw.result()
    except DeviceError as exc:
        return {"host": host, "error": str(exc)}
    model = str((info or {}).get("hw_model", ""))
    if not model.startswith("RSATO") or not isinstance(dash, dict):
        return {"host": host, "error": f"{host} is a {model or 'different device'}, not a ReefATO+."}
    conf = conf if isinstance(conf, dict) else {}
    sensor = dash.get("ato_sensor") or {}
    leak = dash.get("leak_sensor") or {}
    mode_name = (mode.get("mode") if isinstance(mode, dict) else None) or dash.get("mode")
    auto_fill = conf.get("auto_fill")
    days = dash.get("days_till_empty")
    temp = sensor.get("current_read")
    if sensor.get("temperature_probe_status") != "connected" or not isinstance(temp, (int, float)):
        temp = None

    attention = []
    if leak.get("status") not in (None, "dry"):
        attention.append(f"Leak sensor reports: {leak.get('status')}")
    if leak.get("connected") is False:
        attention.append("Leak sensor is not connected")
    elif leak.get("enabled") is False:
        attention.append("Leak sensor is turned off")
    if sensor.get("connected") is False:
        attention.append("Water level sensor is not connected")
    if sensor.get("is_sensor_error") or dash.get("check_sensor"):
        attention.append("Water level sensor needs checking")
    if mode_name not in (None, "auto"):
        attention.append(f"The ATO is in {mode_name} mode, not auto")
    if auto_fill is False:
        attention.append("Auto fill is off, so evaporated water is not being replaced")
    if isinstance(days, (int, float)) and days <= 2 and (dash.get("volume_left") or 0) > 0:
        attention.append(f"The reservoir runs out in about {days} day(s)")

    return {
        "host": host,
        "name": info.get("name"),
        "firmware": fw.get("version") if isinstance(fw, dict) else None,
        "mode": mode_name,
        "auto_fill": auto_fill,
        "water_level": dash.get("water_level"),
        "sensor_level": sensor.get("current_level"),
        "temp_c": temp,
        "volume_left_ml": dash.get("volume_left"),
        "days_till_empty": days,
        "daily_avg_ml": dash.get("daily_volume_average"),
        "today_fills": dash.get("today_fills"),
        "today_ml": dash.get("today_volume_usage"),
        "total_fills": dash.get("total_fills"),
        "pump_on": bool(dash.get("is_pump_on")),
        "pump_state": dash.get("pump_state"),
        "last_cause": dash.get("last_pump_on_cause"),
        "last_fill": dash.get("last_fill_date") if isinstance(dash.get("last_fill_date"), (int, float)) else None,
        "leak": {"status": leak.get("status"), "connected": leak.get("connected"),
                 "enabled": leak.get("enabled"), "buzzer": leak.get("buzzer_enabled")},
        "attention": attention,
    }


def ato_autofill(body: dict[str, Any]) -> None:
    _check_ato()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise DeviceError("Say whether auto fill should be on or off.", 400)
    device_send("PUT", "/configuration", {"auto_fill": enabled}, host=CONFIG["ato_host"], name="ATO")


def ato_reservoir(body: dict[str, Any]) -> None:
    _check_ato()
    amount = _amount(body, False, MAX_RESERVOIR_ML, "reservoir volume")
    device_send("POST", "/update-volume", {"volume": int(round(amount))}, host=CONFIG["ato_host"], name="ATO")


def ato_resume(body: dict[str, Any]) -> None:
    _check_ato()
    device_send("POST", "/resume", None, host=CONFIG["ato_host"], name="ATO")


ATO_ACTIONS = {"autofill": ato_autofill, "reservoir": ato_reservoir, "resume": ato_resume}


# --------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------


def _check_head(head: int) -> dict[str, Any]:
    info, dash = device_get("/device-info"), device_get("/dashboard")
    count = head_count(info, dash)
    if not 1 <= head <= count:
        raise DeviceError(f"Head must be between 1 and {count}.", 400)
    return dash


def _amount(body: dict[str, Any], low_exclusive: bool, limit: float, what: str) -> int | float:
    value = body.get("ml")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise DeviceError(f"Enter a number of milliliters for the {what}.", 400)
    too_low = value <= 0 if low_exclusive else value < 0
    if too_low or value > limit:
        floor = "above 0" if low_exclusive else "0 or more"
        raise DeviceError(f"The {what} must be {floor} and at most {limit:g} ml.", 400)
    amount = _num(value)
    if low_exclusive and amount <= 0:
        raise DeviceError(f"The {what} is too small.", 400)
    return amount


def _put_head(head: int, dash: dict[str, Any], change) -> None:
    settings = device_get(f"/head/{head}/settings")
    if not isinstance(settings, dict):
        raise DeviceError("Unexpected answer from the doser.")
    payload = copy.deepcopy(settings)
    change(payload)
    if dash.get("bundled_heads"):
        for field in ("supplement", "is_food_head", "food_delay"):
            payload.pop(field, None)
    device_send("PUT", f"/head/{head}/settings", payload)


def set_daily(head: int, body: dict[str, Any]) -> None:
    dash = _check_head(head)
    amount = _amount(body, False, MAX_DAILY_ML, "daily dose")
    if dash.get("bundled_heads") and head > 1:
        raise DeviceError("The heads are bundled: change head 1 and the others follow.", 400)

    def change(p: dict[str, Any]) -> None:
        if not isinstance(p.get("schedule"), dict) or "dd" not in p["schedule"]:
            raise DeviceError("This head has no dosing schedule to change.", 400)
        p["schedule"]["dd"] = amount

    _put_head(head, dash, change)


def set_schedule(head: int, body: dict[str, Any]) -> None:
    dash = _check_head(head)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise DeviceError("Say whether the schedule should be on or off.", 400)
    _put_head(head, dash, lambda p: p.__setitem__("schedule_enabled", enabled))


def set_container(head: int, body: dict[str, Any]) -> None:
    dash = _check_head(head)
    amount = _amount(body, False, MAX_CONTAINER_ML, "container volume")

    def change(p: dict[str, Any]) -> None:
        if not p.get("slm"):
            raise DeviceError("Stock tracking is off for this head. Turn it on in the ReefBeat app.", 400)
        p["container_volume"] = amount

    _put_head(head, dash, change)


def dose_now(head: int, body: dict[str, Any]) -> None:
    dash = _check_head(head)
    amount = _amount(body, True, MAX_MANUAL_ML, "dose")
    if dash.get("heads", {}).get(str(head), {}).get("state") == "not-setup":
        raise DeviceError("This head is not set up, so it cannot dose.", 400)
    device_send("POST", f"/head/{head}/manual", {"manual_dose_scheduled": True, "volume": amount}, once=True)


ACTIONS = {"daily": set_daily, "schedule": set_schedule, "container": set_container, "manual": dose_now}


# --------------------------------------------------------------------------
# Web server (this computer only)
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ReefDoseBench"

    def log_message(self, *args: Any) -> None:
        pass

    def _local(self) -> bool:
        # Refuse requests that arrive under another site's name (DNS rebinding).
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._local():
            self._json(403, {"error": "This page only answers on this computer."})
            return
        if self.path == "/":
            data = PAGE.replace("__TOKEN__", TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/status":
            try:
                self._json(200, read_status())
            except DeviceError as exc:
                self._json(exc.status, {"error": str(exc)})
        else:
            self._json(404, {"error": "Not found."})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if 0 < length < 4096 else {}
        if not isinstance(body, dict):
            raise ValueError
        return body

    def _settings(self) -> None:
        # Display choice only: nothing is sent to the doser.
        try:
            tubes = parse_tubes(self._body().get("tubes"))
        except ValueError as exc:
            self._json(400, {"error": str(exc) or "Could not read the request."})
            return
        CONFIG["tubes"] = tubes
        self._json(200, {"tubes": tubes, "saved": save_settings()})

    def do_POST(self) -> None:
        # A change needs the page's own token, so no other website open in the
        # browser can make this computer dose the tank.
        if not self._local() or not secrets.compare_digest(self.headers.get("X-Bench-Token") or "", TOKEN):
            self._json(403, {"error": "Reload the page and try again."})
            return
        if self.path == "/api/settings":
            self._settings()
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "ato"] and parts[2] in ATO_ACTIONS:
            def call(body: dict[str, Any]) -> None:
                ATO_ACTIONS[parts[2]](body)
        elif len(parts) == 4 and parts[:2] == ["api", "head"] and parts[2].isdigit() and parts[3] in ACTIONS:
            def call(body: dict[str, Any]) -> None:
                ACTIONS[parts[3]](int(parts[2]), body)
        else:
            self._json(404, {"error": "Not found."})
            return
        if CONFIG["read_only"]:
            self._json(403, {"error": "This page was started read-only. Restart it without --read-only to make changes."})
            return
        try:
            body = self._body()
        except ValueError:
            self._json(400, {"error": "Could not read the request."})
            return
        try:
            with _write_lock:  # one change at a time
                call(body)
                status = read_status()
            self._json(200, status)
        except DeviceError as exc:
            self._json(exc.status, {"error": str(exc)})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reef bench</title>
<style>
  :root {
    --paper: #e6eeeb; --surface: #f7faf9; --ink: #10333a; --muted: #5a7479;
    --rule: #c5d3cf; --glass: #dbe7e3; --warn: #a4471c; --ok: #1f7a5c;
    --focus: #3b6fb0; --water: #2b7ea6;
    --display: "Avenir Next Condensed", "Avenir Next", "Helvetica Neue", "Segoe UI", sans-serif;
    --text: "Avenir Next", "Avenir", "Helvetica Neue", "Segoe UI", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root { --paper: #0c262b; --surface: #12333a; --ink: #e4f0ec; --muted: #94b0b0;
      --rule: #24484f; --glass: #1a4048; --warn: #f0a070; --ok: #6fd3ae;
      --focus: #8fb8f0; --water: #62b5dc; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink); font: 15px/1.45 var(--text);
    -webkit-font-smoothing: antialiased; }
  main { max-width: 1120px; margin: 0 auto; padding: 28px 16px 48px; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 20px; margin-bottom: 6px; }
  h1 { font: 600 30px/1.1 var(--display); letter-spacing: .01em; margin: 0; }
  .facts { color: var(--muted); font-size: 14px; }
  .facts b { color: var(--ink); font-weight: 600; }
  .fact { display: inline-block; margin-right: 18px; }
  .next { margin: 0 0 22px; font-size: 15px; color: var(--muted); }
  .next b { color: var(--ink); font-weight: 600; }
  .alert { background: var(--surface); border-left: 4px solid var(--warn); padding: 12px 14px; margin: 0 0 20px;
    border-radius: 0 6px 6px 0; }
  .bench { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
  .head { background: var(--surface); border-radius: 10px; padding: 18px 16px 14px; border-top: 5px solid var(--c);
    display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .head.paused { border-top-color: var(--rule); }
  .head h2 { font: 600 19px/1.2 var(--text); margin: 0; overflow-wrap: anywhere; }
  .where { color: var(--muted); font-size: 13px; }
  .vial-row { display: flex; gap: 16px; align-items: stretch; }
  /* The measuring cylinder: the one loud element on the page. */
  .vial { position: relative; width: 62px; height: 190px; flex: none; background: var(--glass);
    border-radius: 6px 6px 16px 16px; overflow: hidden; }
  .liquid { position: absolute; left: 0; right: 0; bottom: 0; background: var(--c); transition: height .6s ease; }
  .paused .liquid { opacity: .45; }
  /* Graduations must read over any tube color and over empty glass. */
  .vial i { position: absolute; left: 0; width: 13px; height: 1px; background: #fff;
    box-shadow: 0 0 0 .5px rgba(8, 40, 46, .55); }
  .vial i.major { width: 22px; height: 2px; }
  .vial span { position: absolute; right: 4px; font: 600 10px/1 var(--text); color: #0c2c32;
    background: rgba(255, 255, 255, .78); padding: 2px 3px; border-radius: 3px; transform: translateY(50%); }
  .read { display: flex; flex-direction: column; justify-content: space-between; min-width: 0; flex: 1; }
  .big { font: 600 46px/1 var(--display); font-variant-numeric: tabular-nums; }
  .big small { font: 500 15px/1 var(--text); color: var(--muted); margin-left: 4px; }
  .line { font-size: 13.5px; color: var(--muted); }
  .line b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
  .today { height: 6px; background: var(--glass); border-radius: 3px; overflow: hidden; margin-top: 5px; }
  .today div { height: 100%; background: var(--c); }
  .flag { color: var(--warn); font-weight: 600; font-size: 13.5px; }
  .week { display: flex; align-items: flex-end; gap: 4px; height: 44px; }
  .week div { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 3px;
    height: 100%; font-size: 10.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .week u { display: block; width: 100%; background: var(--c); opacity: .5; border-radius: 2px 2px 0 0; min-height: 2px; }
  .week div:last-child u { opacity: 1; }
  .week u.none { background: var(--rule); opacity: 1; }
  .controls { display: grid; gap: 8px; border-top: 1px solid var(--rule); padding-top: 12px; }
  .row { display: flex; gap: 6px; align-items: center; }
  .row label { font-size: 13px; color: var(--muted); width: 66px; flex: none; }
  input[type=number] { width: 100%; min-width: 0; font: 600 15px var(--text); color: var(--ink); background: var(--paper);
    border: 1px solid var(--rule); border-radius: 6px; padding: 7px 8px; font-variant-numeric: tabular-nums; }
  button { font: 600 13.5px var(--text); color: var(--ink); background: transparent; border: 1px solid var(--ink);
    border-radius: 6px; padding: 7px 10px; cursor: pointer; white-space: nowrap; }
  button:hover { background: var(--glass); }
  button.go { background: var(--ink); color: var(--surface); }
  button.go:hover { opacity: .88; background: var(--ink); }
  button.quiet { border-color: var(--rule); font-weight: 500; }
  button:disabled { opacity: .4; cursor: default; }
  :focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
  .ask { background: var(--paper); border-radius: 8px; padding: 10px; display: grid; gap: 8px; font-size: 14px; }
  .ask .row { justify-content: flex-end; }
  .note { font-size: 13.5px; min-height: 1.4em; }
  .note.ok { color: var(--ok); } .note.bad { color: var(--warn); }
  .tools { margin-left: auto; }
  .tubes { background: var(--surface); border-radius: 10px; padding: 16px; margin: 0 0 16px; display: grid; gap: 14px; }
  .tubes h2 { font: 600 16px/1.2 var(--text); margin: 0; }
  .tubes p { margin: 2px 0 0; color: var(--muted); font-size: 13.5px; max-width: 70ch; }
  .sets { display: flex; flex-wrap: wrap; gap: 8px; }
  .sets button { display: flex; align-items: center; gap: 8px; }
  .sets button[aria-pressed=true] { background: var(--glass); }
  .chips { display: inline-flex; gap: 3px; }
  .chips b { width: 12px; height: 18px; border-radius: 3px; display: block; }
  .pick { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 14px; align-items: center; }
  .pick .name { font-size: 14px; overflow-wrap: anywhere; }
  .pick .name small { color: var(--muted); }
  .swatches { display: flex; gap: 5px; flex-wrap: wrap; }
  .swatches button { width: 28px; height: 28px; padding: 0; border-radius: 50%; border: 2px solid var(--surface);
    box-shadow: 0 0 0 1px var(--rule); }
  .swatches button.gap { margin-left: 8px; }
  .swatches button[aria-pressed=true] { box-shadow: 0 0 0 2px var(--ink); }
  .ato-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 20px; margin: 34px 0 12px; }
  .ato-head h2 { font: 600 30px/1.1 var(--display); letter-spacing: .01em; margin: 0; }
  .tile { background: var(--surface); border-radius: 10px; padding: 18px 16px 14px; border-top: 5px solid var(--water);
    display: flex; flex-direction: column; gap: 10px; min-width: 0; --c: var(--water); }
  .tile h3 { font: 600 15px/1.2 var(--text); margin: 0; color: var(--muted); }
  .tile .big { font-size: 38px; }
  .tile .controls { margin-top: auto; }
  .tile .vial { height: 120px; }
  footer { margin-top: 22px; color: var(--muted); font-size: 13px; }
  @media (prefers-reduced-motion: reduce) { .liquid { transition: none; } }
</style>
</head>
<body>
<main>
  <header>
    <h1 id="title">ReefDose</h1>
    <div class="facts" id="facts">Reading the doser…</div>
    <div class="tools"><button class="quiet" id="tubebtn" aria-expanded="false">Tube colors</button></div>
  </header>
  <p class="next" id="next"></p>
  <div id="alert"></div>
  <div id="tubes"></div>
  <section class="bench" id="bench" aria-live="polite"></section>
  <div id="ato"></div>
  <footer id="foot"></footer>
</main>
<script>
const TOKEN = "__TOKEN__";
let status = null, busy = false, tubesOpen = false, tubeNote = "";
const atoUi = {};   // {refill, ask, note}
const tubeHex = i => status.palette[status.tubes[i]]?.hex || "#888";
const ui = {};   // per head: {daily, manual, refill, ask, note}

const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) n.append(kid);
  return n;
};
const ml = v => (v == null ? "–" : (+(+v).toFixed(v < 10 ? 2 : 1)).toString());

function scaleFor(v) {            // the cylinder's top graduation
  for (const s of [50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000]) if (v <= s) return s;
  return 20000;
}

function vial(h) {
  const v = h.container_ml;
  const box = el("div", { class: "vial", role: "img",
    "aria-label": v == null ? "Container level unknown" : `${ml(v)} milliliters left in the container` });
  if (v == null) return box;
  const top = scaleFor(v), usable = 0.9;        // top graduation sits at 90% height
  box.append(el("div", { class: "liquid", style: `height:${Math.min(100, v / top * usable * 100)}%` }));
  for (let i = 1; i <= 10; i++) {
    const major = i % 5 === 0, y = i / 10 * usable * 100;
    box.append(el("i", { class: major ? "major" : "", style: `bottom:${y}%` }));
    if (major) box.append(el("span", { style: `bottom:${y}%` }, String(top * i / 10 / (h.per || 1))));
  }
  return box;
}

function week(h) {
  const days = h.history.slice(0, 7).reverse();
  const max = Math.max(...days.map(d => d.ml || 0), 0.001);
  return el("div", { class: "week", role: "img",
      "aria-label": "Dosed per day: " + days.map(d => `${d.date}: ${ml(d.ml)} ml`).join(", ") },
    days.map(d => el("div", { title: `${d.date}: ${ml(d.ml)} ml` },
      el("u", { class: d.ml ? "" : "none", style: `height:${d.ml ? Math.max(6, d.ml / max * 100) : 0}%` }),
      d.date ? String(+d.date.slice(8)) : "")));
}

function ask(h, text, label, path, body) {
  ui[h.head].ask = { text, label, path, body }; ui[h.head].note = null; render();
}

async function send(h, a) {
  busy = true; ui[h.head].ask = null; ui[h.head].note = { text: "Sending…", cls: "" }; render();
  try {
    const r = await fetch(`/api/head/${h.head}/${a.path}`, { method: "POST",
      headers: { "Content-Type": "application/json", "X-Bench-Token": TOKEN }, body: JSON.stringify(a.body) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "The change failed.");
    status = data; ui[h.head] = { note: { text: a.done, cls: "ok" } };
  } catch (e) { ui[h.head].note = { text: e.message, cls: "bad" }; }
  busy = false; render();
}

function card(h, i) {
  const u = ui[h.head] ||= {};
  const lim = status.limits, ro = status.read_only, paused = h.schedule_enabled === false;
  const dosed = (h.auto_today_ml || 0) + (h.manual_today_ml || 0);
  const daily = u.daily ?? h.daily_ml ?? 0;
  const num = (key, value, max, step) => el("input", { type: "number", min: 0, max, step, value, disabled: ro || busy,
    "aria-label": key, oninput: e => { u[key] = e.target.value; } });

  const controls = el("div", { class: "controls" },
    el("div", { class: "row" }, el("label", { for: `d${h.head}` }, "Daily ml"),
      Object.assign(num("daily", daily, lim.daily_ml, 0.1), { id: `d${h.head}` }),
      el("button", { disabled: ro || busy, onclick: () => {
        const v = parseFloat(u.daily ?? h.daily_ml);
        if (!(v >= 0)) { u.note = { text: "Enter the daily dose in ml.", cls: "bad" }; return render(); }
        ask(h, `Change ${h.supplement} from ${ml(h.daily_ml)} to ${ml(v)} ml a day?`, `Set ${ml(v)} ml a day`,
          "daily", { ml: v }); ui[h.head].ask.done = `Daily dose is now ${ml(v)} ml.`; render(); } }, "Save")),
    el("div", { class: "row" }, el("label", { for: `m${h.head}` }, "Dose now"),
      Object.assign(num("manual", u.manual ?? "", lim.manual_ml, 0.1), { id: `m${h.head}`, placeholder: "ml" }),
      el("button", { disabled: ro || busy, onclick: () => {
        const v = parseFloat(u.manual);
        if (!(v > 0)) { u.note = { text: "Enter how many ml to dose.", cls: "bad" }; return render(); }
        ask(h, `Dose ${ml(v)} ml of ${h.supplement} into the tank now? This cannot be undone.`, `Dose ${ml(v)} ml`,
          "manual", { ml: v }); ui[h.head].ask.done = `Dosed ${ml(v)} ml. Today's total can take a few minutes to update.`; render(); } }, "Dose")),
    h.stock_monitoring && el("div", { class: "row" }, el("label", { for: `r${h.head}` }, "Refilled to"),
      Object.assign(num("refill", u.refill ?? "", lim.container_ml, 1), { id: `r${h.head}`, placeholder: "ml" }),
      el("button", { class: "quiet", disabled: ro || busy, onclick: () => {
        const v = parseFloat(u.refill);
        if (!(v >= 0)) { u.note = { text: "Enter the container's volume in ml.", cls: "bad" }; return render(); }
        ask(h, `Record ${ml(v)} ml in the ${h.supplement} container?`, `Record ${ml(v)} ml`, "container", { ml: v });
        ui[h.head].ask.done = `Container set to ${ml(v)} ml.`; render(); } }, "Record")),
    el("div", { class: "row" }, el("button", { class: "quiet", style: "flex:1", disabled: ro || busy, onclick: () => {
        ask(h, paused ? `Resume the ${h.supplement} schedule at ${ml(h.daily_ml)} ml a day?`
                      : `Pause the ${h.supplement} schedule? It stops dosing until you resume it.`,
          paused ? "Resume schedule" : "Pause schedule", "schedule", { enabled: paused });
        ui[h.head].ask.done = paused ? "Schedule resumed." : "Schedule paused."; render(); } },
      paused ? "Resume schedule" : "Pause schedule")),
    u.ask && el("div", { class: "ask", role: "alertdialog" }, u.ask.text,
      el("div", { class: "row" },
        el("button", { class: "quiet", onclick: () => { u.ask = null; render(); } }, "Cancel"),
        el("button", { class: "go", onclick: () => send(h, u.ask) }, u.ask.label))),
    el("div", { class: "note " + (u.note?.cls || ""), role: "status" }, u.note?.text || ""));

  return el("article", { class: "head" + (paused ? " paused" : ""), style: `--c:${tubeHex(i)}` },
    el("div", {}, el("h2", {}, h.supplement),
      el("div", { class: "where" }, `Head ${h.head}` + (paused ? ", schedule paused" : ""))),
    el("div", { class: "vial-row" }, vial(h),
      el("div", { class: "read" },
        el("div", {}, el("div", { class: "big" }, ml(h.daily_ml), el("small", {}, "ml a day")),
          paused ? el("div", { class: "line" }, "Not dosing") : el("div", { class: "line" },
            el("b", {}, ml(dosed)), ` ml today, ${h.doses_done ?? 0} of ${h.doses_per_day ?? 0} doses`,
            el("div", { class: "today" }, el("div", { style: `width:${Math.min(100, h.daily_ml ? dosed / h.daily_ml * 100 : 0)}%` })))),
        el("div", {},
          el("div", { class: "line" }, el("b", {}, ml(h.container_ml)), " ml in the container"),
          !paused && h.remaining_days != null && el("div", { class: h.remaining_days <= 7 ? "flag" : "line" },
            el("b", {}, String(h.remaining_days)), ` day${h.remaining_days === 1 ? "" : "s"} of stock left`),
          h.recalibrate && el("div", { class: "flag" }, "Recalibration due")))),
    week(h), controls);
}

async function setTubes(tubes) {
  status.tubes = tubes; tubeNote = ""; render();          // show it at once
  try {
    const r = await fetch("/api/settings", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Bench-Token": TOKEN }, body: JSON.stringify({ tubes }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error);
    status.tubes = data.tubes;
    tubeNote = data.saved ? "" : "Colors changed for now, but the choice could not be saved to disk.";
  } catch (e) { tubeNote = e.message || "Could not save the colors."; }
  render();
}

function tubePanel() {
  const box = document.getElementById("tubes"); box.replaceChildren();
  document.getElementById("tubebtn").setAttribute("aria-expanded", String(tubesOpen));
  if (!tubesOpen) return;
  const n = status.heads.length, names = Object.keys(status.palette);
  const same = set => status.tube_sets[set].slice(0, n).every((c, i) => c === status.tubes[i]);
  const setLabel = { "blue-green": "Blue-green set", "red-yellow": "Red-yellow set" };
  box.append(el("section", { class: "tubes", "aria-label": "Tube colors" },
    el("div", {}, el("h2", {}, "Tube colors"),
      el("p", {}, "Match each head to the tube on your doser. Red Sea sells two sets of four. " +
        "This only changes the colors on this page.")),
    el("div", { class: "sets" }, Object.keys(status.tube_sets).map(set =>
      el("button", { class: "quiet", "aria-pressed": String(same(set)),
          onclick: () => setTubes(status.tube_sets[set].slice()) },
        el("span", { class: "chips" }, status.tube_sets[set].map(c => el("b", { style: `background:${status.palette[c].hex}` }))),
        setLabel[set] || set))),
    el("div", { class: "pick" }, status.heads.map((h, i) => [
      el("div", { class: "name" }, h.supplement, " ", el("small", {}, `head ${h.head}`)),
      el("div", { class: "swatches", role: "group", "aria-label": `Tube color for head ${h.head}` },
        names.map((c, k) => el("button", { class: k === 4 ? "gap" : "", style: `background:${status.palette[c].hex}`,
          title: status.palette[c].label, "aria-label": status.palette[c].label,
          "aria-pressed": String(status.tubes[i] === c),
          onclick: () => { const t = status.tubes.slice(); t[i] = c; setTubes(t); } })))])),
    tubeNote && el("div", { class: "note bad", role: "status" }, tubeNote)));
}
document.getElementById("tubebtn").addEventListener("click", () => { tubesOpen = !tubesOpen; render(); });

const liters = v => (v == null ? "–" : (+(v / 1000).toFixed(v < 1000 ? 2 : 1)).toString());
const words = v => (v == null ? "unknown" : String(v).replace(/_/g, " "));

async function sendAto(a) {
  busy = true; atoUi.ask = null; atoUi.note = { text: "Sending…", cls: "" }; render();
  try {
    const r = await fetch(`/api/ato/${a.path}`, { method: "POST",
      headers: { "Content-Type": "application/json", "X-Bench-Token": TOKEN }, body: JSON.stringify(a.body) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "The change failed.");
    status = data; atoUi.refill = undefined; atoUi.note = { text: a.done, cls: "ok" };
  } catch (e) { atoUi.note = { text: e.message, cls: "bad" }; }
  busy = false; render();
}

function atoPanel() {
  const box = document.getElementById("ato"); box.replaceChildren();
  const a = status.ato;
  if (!a) return;
  const head = el("div", { class: "ato-head" }, el("h2", {}, "ReefATO+"));
  box.append(head);
  if (a.error) { box.append(el("div", { class: "alert" }, a.error)); return; }
  const facts = el("div", { class: "facts" });
  const fact = (label, value) => value && facts.append(el("span", { class: "fact" }, label + " ", el("b", {}, value)));
  fact("at", a.host); fact("mode", a.mode); fact("firmware", a.firmware);
  head.append(facts);
  for (const text of a.attention) box.append(el("div", { class: "alert" }, text));

  const ro = status.read_only, tracked = (a.volume_left_ml || 0) > 0;
  const f = a.temp_c == null ? null : a.temp_c * 9 / 5 + 32;
  const avg = a.daily_avg_ml || 0, today = a.today_ml || 0;
  const ask = (text, label, path, body, done) => { atoUi.ask = { text, label, path, body, done }; atoUi.note = null; render(); };

  const water = el("article", { class: "tile" }, el("h3", {}, "Sump water"),
    el("div", { class: "big" }, f == null ? "–" : f.toFixed(1), el("small", {}, "°F")),
    f != null && el("div", { class: "line" }, `${a.temp_c.toFixed(1)} °C at the level sensor`),
    el("div", { class: "line" }, "Water level ", el("b", {}, words(a.water_level))),
    el("div", { class: "line" }, "Sensor reads ", el("b", {}, words(a.sensor_level))));

  const todayTile = el("article", { class: "tile" }, el("h3", {}, "Topped off today"),
    el("div", { class: "big" }, liters(today), el("small", {}, "L")),
    el("div", { class: "line" }, el("b", {}, String(a.today_fills ?? 0)), ` fill${a.today_fills === 1 ? "" : "s"} today`),
    avg > 0 ? el("div", { class: "line" }, "Daily average ", el("b", {}, liters(avg)), " L",
        el("div", { class: "today" }, el("div", { style: `width:${Math.min(100, today / avg * 100)}%` })))
      : el("div", { class: "line" }, "No daily average yet"),
    avg > 0 && today > avg * 2 && el("div", { class: "flag" }, "More than twice the usual amount"),
    el("div", { class: "line" }, a.pump_on ? el("b", {}, "Pump is running") : "Pump is off",
      a.last_fill ? `, last fill ${new Date(a.last_fill * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""),
    el("div", { class: "line" }, "Leak sensor ", el("b", {}, words(a.leak.status))));

  const res = el("article", { class: "tile" }, el("h3", {}, "Fresh water reservoir"),
    tracked ? el("div", { class: "vial-row" }, vial({ container_ml: a.volume_left_ml, per: 1000 }),
        el("div", { class: "read" }, el("div", {},
          el("div", { class: "big" }, liters(a.volume_left_ml), el("small", {}, "L left")),
          a.days_till_empty != null && el("div", { class: a.days_till_empty <= 2 ? "flag" : "line" },
            el("b", {}, String(a.days_till_empty)), ` day${a.days_till_empty === 1 ? "" : "s"} until empty`))))
      : el("div", { class: "line" }, "Not tracked yet. Record how much water is in the reservoir and the ATO will count it down."),
    el("div", { class: "controls" },
      el("div", { class: "row" }, el("label", { for: "atofill" }, "Refilled to"),
        el("input", { id: "atofill", type: "number", min: 0, max: status.limits.reservoir_ml / 1000, step: 0.5,
          placeholder: "liters", value: atoUi.refill ?? "", disabled: ro || busy,
          oninput: e => { atoUi.refill = e.target.value; } }),
        el("button", { class: "quiet", disabled: ro || busy, onclick: () => {
          const v = parseFloat(atoUi.refill);
          if (!(v >= 0)) { atoUi.note = { text: "Enter the reservoir's volume in liters.", cls: "bad" }; return render(); }
          ask(`Record ${v} L in the reservoir?`, `Record ${v} L`, "reservoir", { ml: Math.round(v * 1000) },
            `Reservoir set to ${v} L.`); } }, "Record"))));

  const on = a.auto_fill !== false;
  const ctl = el("article", { class: "tile" }, el("h3", {}, "Auto fill"),
    el("div", { class: "big" }, on ? "On" : "Off"),
    el("div", { class: "line" }, on ? "The ATO replaces evaporated water by itself."
      : "The ATO is not adding water. Salinity rises as water evaporates."),
    el("div", { class: "controls" },
      el("div", { class: "row" }, el("button", { class: "quiet", style: "flex:1", disabled: ro || busy, onclick: () =>
        ask(on ? "Turn auto fill off? The ATO stops adding water until you turn it back on."
               : "Turn auto fill on?", on ? "Turn auto fill off" : "Turn auto fill on", "autofill", { enabled: !on },
          on ? "Auto fill is off." : "Auto fill is on.") }, on ? "Turn auto fill off" : "Turn auto fill on")),
      el("div", { class: "row" }, el("button", { class: "quiet", style: "flex:1", disabled: ro || busy, onclick: () =>
        ask("Resume the ATO? Use this after it stopped itself, for example once an empty reservoir is refilled.",
          "Resume", "resume", {}, "Resume sent.") }, "Resume after a stop")),
      atoUi.ask && el("div", { class: "ask", role: "alertdialog" }, atoUi.ask.text,
        el("div", { class: "row" },
          el("button", { class: "quiet", onclick: () => { atoUi.ask = null; render(); } }, "Cancel"),
          el("button", { class: "go", onclick: () => sendAto(atoUi.ask) }, atoUi.ask.label))),
      el("div", { class: "note " + (atoUi.note?.cls || ""), role: "status" }, atoUi.note?.text || "")));

  box.append(el("section", { class: "bench", "aria-label": "ReefATO+" }, water, todayTile, res, ctl));
}

function render() {
  if (!status) return;
  tubePanel();
  document.getElementById("title").textContent = status.model === "RSDOSE2" ? "ReefDose 2" : "ReefDose 4";
  const facts = document.getElementById("facts"); facts.replaceChildren();
  const fact = (label, value) => value && facts.append(el("span", { class: "fact" }, label + " ", el("b", {}, value)));
  fact("at", status.host); fact("mode", status.mode); fact("firmware", status.firmware); fact("battery", status.battery);
  const next = document.getElementById("next"); next.replaceChildren();
  if (status.queue.length) {
    const q = status.queue[0];
    // The doser names a queued dose's head by its supplement's short name ("KH"); allow a number too.
    const what = status.heads.find(h => h.short_name === q.head || String(h.head) === String(q.head))?.supplement
      || q.head;
    next.append("Next dose at ", el("b", {}, q.time), ` (doser's clock): ${ml(q.ml)} ml of ${what}`
      + (status.queue.length > 1 ? `, then ${status.queue.length - 1} more today.` : "."));
  } else next.append("No more doses queued for today.");
  const alert = document.getElementById("alert"); alert.replaceChildren();
  if (status.mode && status.mode !== "auto")
    alert.append(el("div", { class: "alert" }, `The doser is in ${status.mode} mode, so scheduled doses are not running.`));
  if (status.read_only) alert.append(el("div", { class: "alert" }, "Read-only: this page was started with --read-only."));
  // Keep focus and typing intact across refreshes.
  const active = document.activeElement?.id, bench = document.getElementById("bench");
  bench.replaceChildren(...status.heads.map(card));
  atoPanel();
  if (active) document.getElementById(active)?.focus();
  document.getElementById("foot").textContent = "Read at " + new Date().toLocaleTimeString() +
    ". Refreshes every 30 seconds. Limits: " + ml(status.limits.manual_ml) + " ml per manual dose, " +
    ml(status.limits.daily_ml) + " ml per day.";
}

async function refresh() {
  if (busy || atoUi.ask || Object.values(ui).some(u => u.ask)
      || /^(d|m|r)\d$|^atofill$/.test(document.activeElement?.id || "")) return;
  try {
    const r = await fetch("/api/status"); const data = await r.json();
    if (!r.ok) throw new Error(data.error);
    status = data; render();
  } catch (e) {
    document.getElementById("alert").replaceChildren(el("div", { class: "alert" },
      (e.message || "Could not reach this page's helper.") + " Trying again in 30 seconds."));
    if (!status) document.getElementById("facts").textContent = "";
  }
}
refresh(); setInterval(refresh, 30000);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web page for a Red Sea ReefDose.")
    parser.add_argument("host", nargs="?", default=os.environ.get("REEFDOSE_HOST", ""),
                        help="the doser's IP address (default: REEFDOSE_HOST)")
    parser.add_argument("--ato", default=os.environ.get("REEFATO_HOST", ""),
                        help="the ReefATO+'s IP address (default: REEFATO_HOST); leave out to hide it")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--read-only", action="store_true", help="show status only")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--tubes", help="tube colors: blue-green, red-yellow, or one color per head, "
                        "e.g. purple,pink,green,red")
    args = parser.parse_args()
    if not args.host.strip():
        sys.exit("Give the doser's IP address, e.g. python3 gui.py 192.168.1.50, or set REEFDOSE_HOST.")
    args.host = args.host.strip()
    CONFIG["host"], CONFIG["read_only"] = args.host, args.read_only
    CONFIG["ato_host"] = "" if args.ato.strip().lower() in ("none", "off", "") else args.ato.strip()
    load_settings()
    if args.tubes:
        try:
            CONFIG["tubes"] = parse_tubes(args.tubes)
        except ValueError as exc:
            sys.exit(str(exc))
        save_settings()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        sys.exit(f"Could not start on port {args.port} ({exc}). Is it already running? Try --port 8766.")
    url = f"http://127.0.0.1:{args.port}"
    devices = f"doser {args.host}" + (f", ATO {CONFIG['ato_host']}" if CONFIG["ato_host"] else "")
    print(f"Reef bench ({devices}) is running at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
