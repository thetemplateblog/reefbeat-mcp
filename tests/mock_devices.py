"""Stand-in ReefDose 4 and ReefATO+ for testing the MCP server off the tank.

Serves the same endpoints and JSON shapes as the real devices (shapes taken
from the fixtures of Elwinmage/reefbeat-devices-simulator), keeps state in
memory, and records every write so tests can assert on exactly what was sent.

Run by hand to try the server without touching real hardware:

    python tests/mock_devices.py            # prints the two addresses to use
"""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _head(n: int, name: str, short: str, dd: float, volume: float, state: str = "on") -> dict[str, Any]:
    return {
        "container_volume": volume,
        "dc": True,
        "enable_ratio": True,
        "entire_dd": True,
        "food_delay": 0,
        "is_food_head": False,
        "last_calibrated": 1749296252,
        "manual_dose_scheduled": False,
        "recalibration_required": False,
        "schedule": {
            "days": [1, 2, 3, 4, 5, 6, 7],
            "dd": dd,
            "intervals": [{"end": 1200, "mode": "regular", "nd": 3, "st": 480}],
            "type": "custom",
        },
        "schedule_enabled": True,
        "shortcut_code": "no_shortcut",
        "show": True,
        "slm": n != 3,  # head 3 has stock monitoring off
        "state": state,
        "supplement": {
            "brand_name": "Test",
            "concentration": 0,
            "display_name": name,
            "name": name,
            "short_name": short,
            "uid": f"00000000-0000-0000-0000-00000000000{n}",
        },
        "today_auto_dosed_volume": 0,
        "vps": 0.063636,
    }


def _dash_head(name: str, dd: float, state: str = "on") -> dict[str, Any]:
    return {
        "auto_dosed_today": round(dd / 3, 1),
        "daily_dose": dd,
        "daily_doses": 3,
        "doses_today": 1,
        "is_food_head": False,
        "manual_dosed_today": 0,
        "missed_dose": {"missed_volume": 0, "total_volume": 0},
        "recalibration_required": False,
        "remaining_days": 40,
        "shortcut_code": "no_shortcut",
        "state": state,
        "stock_level": "high" if state == "on" else "off",
        "supplement": name,
    }


def dose_state() -> dict[str, Any]:
    heads = [
        (1, "Nutrient A", "NA", 7, 500.0, "on"),
        (2, "Alkalinity", "KH", 45, 1480.5, "on"),
        (3, "Magnesium", "Mg", 40, 420.0, "on"),
        (4, "Unused", "--", 0, 0, "not-setup"),
    ]
    state: dict[str, Any] = {
        "/device-info": {"hw_model": "RSDOSE4", "hw_type": "reef-dosing", "hwid": "aabbccddeeff", "name": "MOCK-RSDOSE4"},
        "/firmware": {"version": "3.0.1"},
        "/mode": {"mode": "auto"},
        "/dashboard": {
            "battery_level": "high",
            "bundled_heads": False,
            "heads": {str(n): _dash_head(name, dd, st) for n, name, _, dd, _, st in heads},
            "is_active": False,
            "time_error": False,
        },
        "/device-settings": {"dosing_waiting_period": 600, "stock_alert_days": 2},
        "/dosing-queue": [
            {"dose_type": "Auto", "head": "NA", "time": 43200, "volume": 2.333333},
            {"dose_type": "Auto", "head": "KH", "time": 43813, "volume": 15},
        ],
        "/daily-log": {
            "daily": [
                {"1": 2.3, "2": 15.0, "3": 13.4, "4": 0, "date": 1763683200},
                {"1": 7.0, "2": 45.0, "3": 40.0, "4": 0, "date": 1763596800},
            ]
        },
    }
    for n, name, short, dd, volume, st in heads:
        state[f"/head/{n}/settings"] = _head(n, name, short, dd, volume, st)
    return state


def ato_state() -> dict[str, Any]:
    return {
        "/device-info": {"hw_model": "RSATO+", "hw_type": "reef-ato", "hwid": "112233445566", "name": "MOCK-RSATO+"},
        "/firmware": {"version": "2.1.0"},
        "/mode": {"mode": "auto"},
        "/dashboard": {
            "ato_sensor": {
                "connected": True,
                "current_level": "desired",
                "current_read": 25.2125,
                "is_calibrated": True,
                "is_sensor_error": False,
                "is_temp_enabled": True,
                "temperature_probe_status": "connected",
            },
            "check_sensor": False,
            "daily_volume_average": 2500,
            "days_till_empty": 5,
            "is_pump_on": False,
            "last_fill_date": 1751219015,
            "last_pump_on_cause": "ec_sensor_s1",
            "leak_sensor": {"buzzer_enabled": True, "connected": True, "current_read": 0, "enabled": True, "status": "dry"},
            "mode": "auto",
            "pump_state": "off",
            "today_fills": 2,
            "today_volume_usage": 900,
            "total_fills": 36,
            "volume_left": 12500,
            "water_level": "desired_level_2",
        },
        "/configuration": {
            "auto_delay": 30,
            "auto_fill": True,
            "buzzer": {"duty_cycle": 50, "enabled": True, "frequency": 12},
            "leak": {"dry_threshold": 1, "emergency_shutdown": False, "rodi_threshold": 20, "sensor_enabled": True},
        },
    }


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


class MockDevice:
    """One fake device on 127.0.0.1 with an ephemeral port."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.state = dose_state() if kind == "dose" else ato_state()
        self.writes: list[dict[str, Any]] = []
        self.fail_next: dict[str, Any] = {}  # path -> status code, or "drop"
        self.lock = threading.Lock()
        device = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _reply(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                with device.lock:
                    body = copy.deepcopy(device.state.get(self.path))
                if body is None:
                    self._reply(404, {"success": False, "message": "not found"})
                else:
                    self._reply(200, body)

            def _write(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else None
                with device.lock:
                    device.writes.append({"method": method, "path": self.path, "body": payload})
                    fail = device.fail_next.pop(self.path, None)
                    if fail == "drop":
                        self.connection.close()
                        return
                    if fail:
                        self._reply(int(fail), {"success": False, "message": "injected failure"})
                        return
                    status = device.apply(method, self.path, payload)
                if status == 200:
                    self._reply(200, {"success": True, "message": "ok"})
                else:
                    self._reply(status, {"success": False, "message": "not allowed"})

            def do_POST(self) -> None:
                self._write("POST")

            def do_PUT(self) -> None:
                self._write("PUT")

            def do_DELETE(self) -> None:
                self._write("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def apply(self, method: str, path: str, payload: Any) -> int:
        s = self.state
        if (method, path) == ("POST", "/off"):
            s["/mode"]["mode"] = "off"
        elif (method, path) == ("DELETE", "/off"):
            s["/mode"]["mode"] = "auto"
        elif (method, path) == ("POST", "/maintenance"):
            s["/mode"]["mode"] = "maintenance"
        elif (method, path) == ("DELETE", "/maintenance"):
            s["/mode"]["mode"] = "auto"
        elif self.kind == "dose" and method == "PUT" and path.startswith("/head/") and path.endswith("/settings"):
            head = path.split("/")[2]
            _merge(s[path], payload)
            s["/dashboard"]["heads"][head]["daily_dose"] = s[path]["schedule"]["dd"]
        elif self.kind == "dose" and method == "POST" and path.startswith("/head/") and path.endswith("/manual"):
            head = path.split("/")[2]
            s["/dashboard"]["heads"][head]["manual_dosed_today"] += payload["volume"]
        elif self.kind == "ato" and (method, path) == ("PUT", "/configuration"):
            _merge(s[path], payload)
        elif self.kind == "ato" and (method, path) == ("POST", "/update-volume"):
            s["/dashboard"]["volume_left"] = payload["volume"]
        elif self.kind == "ato" and (method, path) == ("POST", "/resume"):
            s["/dashboard"]["pump_state"] = "off"
        else:
            return 405
        if "/dashboard" in s and "mode" in s["/dashboard"]:
            s["/dashboard"]["mode"] = s["/mode"]["mode"]
        return 200

    def start(self) -> "MockDevice":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    dose, ato = MockDevice("dose").start(), MockDevice("ato").start()
    print(f"REEFDOSE_HOST={dose.host}")
    print(f"REEFATO_HOST={ato.host}")
    print("Mock devices running. Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
