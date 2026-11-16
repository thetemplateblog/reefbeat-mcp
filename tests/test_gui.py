"""End-to-end test of the Reef bench web page (gui.py) against the mock
devices: runs its local web server and checks every endpoint, including
exactly what gets sent to the devices.

    python3 tests/test_gui.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
import gui  # noqa: E402
from mock_devices import MockDevice  # noqa: E402

passed = 0
failed: list[str] = []
BASE = ""


def check(name: str, cond: bool, detail: Any = "") -> None:
    global passed
    if cond:
        passed += 1
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")


def req(method: str, path: str, body: Any = None, *, token: bool = True, host: str | None = None) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Bench-Token"] = gui.TOKEN
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode()


def post(path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
    return req("POST", path, body, **kw)


def page_and_status(dose: MockDevice, ato: MockDevice) -> None:
    print("page and status")
    s, page = req("GET", "/")
    check("page served", s == 200 and gui.TOKEN in page and "__TOKEN__" not in page, s)
    s, d = req("GET", "/", host="evil.example")
    check("other Host refused", s == 403, s)
    s, d = req("GET", "/api/status", host="evil.example:8765")
    check("status refused for other Host", s == 403, s)
    s, d = req("GET", "/nope")
    check("unknown path 404", s == 404, s)

    s, d = req("GET", "/api/status")
    check("status ok", s == 200, d)
    check("status facts", d["model"] == "RSDOSE4" and d["firmware"] == "3.0.1" and d["mode"] == "auto"
          and d["host"] == dose.host, d)
    check("status heads", len(d["heads"]) == 4, d["heads"])
    h1, h3 = d["heads"][0], d["heads"][2]
    check("head 1", h1["supplement"] == "Nutrient A" and h1["short_name"] == "NA" and h1["daily_ml"] == 7
          and h1["container_ml"] == 500.0 and h1["stock_monitoring"] is True and h1["schedule_enabled"] is True, h1)
    check("head 1 history", h1["history"] == [{"date": "2025-11-21", "ml": 2.3}, {"date": "2025-11-20", "ml": 7.0}],
          h1["history"])
    check("head 3 no stock monitoring", h3["stock_monitoring"] is False, h3)
    check("queue", d["queue"][0] == {"time": "12:00", "head": "NA", "ml": 2.333333, "type": "Auto"}, d["queue"])
    check("limits", d["limits"]["manual_ml"] == 10 and d["limits"]["daily_ml"] == 100, d["limits"])
    a = d["ato"]
    check("ato", a and a["auto_fill"] is True and a["volume_left_ml"] == 12500 and a["temp_c"] == 25.2125
          and a["attention"] == [], a)
    check("nothing written by reads", not dose.writes and not ato.writes, dose.writes + ato.writes)


def odd_answers(dose: MockDevice, ato: MockDevice) -> None:
    print("odd device answers")
    saved = copy.deepcopy(dose.state["/dashboard"])
    dose.state["/dashboard"] = []
    s, d = req("GET", "/api/status")
    check("non-object dashboard is a clean error", s == 502 and "Unexpected answer" in d["error"], (s, d))
    dose.state["/dashboard"] = saved

    saved = copy.deepcopy(dose.state["/head/2/settings"])
    dose.state["/head/2/settings"] = ["odd"]
    s, d = req("GET", "/api/status")
    check("non-object head settings tolerated", s == 200 and d["heads"][1]["container_ml"] is None, (s, d))
    dose.state["/head/2/settings"] = saved

    ato.state["/device-info"]["hw_model"] = "RSDOSE2"
    s, d = req("GET", "/api/status")
    check("wrong ATO model shown, doser still works", s == 200 and "not a ReefATO+" in d["ato"]["error"]
          and len(d["heads"]) == 4, d)
    ato.state["/device-info"]["hw_model"] = "RSATO+"


def doser_changes(dose: MockDevice) -> None:
    print("doser changes")
    s, d = post("/api/head/2/daily", {"ml": 50}, token=False)
    check("change without token refused", s == 403 and not dose.writes, (s, dose.writes))
    s, d = post("/api/head/2/daily", {"ml": 50}, host="evil.example")
    check("change from other Host refused", s == 403 and not dose.writes, (s, dose.writes))

    s, d = post("/api/head/2/daily", {"ml": 50})
    w = dose.writes[-1] if dose.writes else {}
    check("set daily dose", s == 200 and w.get("method") == "PUT" and w.get("path") == "/head/2/settings"
          and w["body"]["schedule"]["dd"] == 50 and w["body"]["schedule"]["intervals"]
          and w["body"]["supplement"]["name"] == "Alkalinity", (s, w))
    check("set daily answers fresh status", d["heads"][1]["daily_ml"] == 50, d)

    dose.writes.clear()
    for body, why in [({"ml": 101}, "over limit"), ({"ml": -1}, "negative"), ({"ml": None}, "missing"),
                      ({"ml": "5"}, "string"), ({"ml": True}, "bool")]:
        s, d = post("/api/head/2/daily", body)
        check(f"daily {why} rejected", s == 400 and not dose.writes, (s, d, dose.writes))
    s, d = post("/api/head/5/daily", {"ml": 5})
    check("head out of range", s == 400 and "between 1 and 4" in d["error"] and not dose.writes, (s, d))
    s, d = post("/api/head/2/explode", {"ml": 5})
    check("unknown action 404", s == 404, s)

    saved = copy.deepcopy(dose.state["/head/1/settings"]["schedule"])
    del dose.state["/head/1/settings"]["schedule"]["dd"]
    s, d = post("/api/head/1/daily", {"ml": 5})
    check("no daily-dose field refused", s == 400 and "no dosing schedule" in d["error"] and not dose.writes, (s, d))
    dose.state["/head/1/settings"]["schedule"] = saved

    s, d = post("/api/head/2/schedule", {"enabled": False})
    w = dose.writes[-1] if dose.writes else {}
    check("pause schedule", s == 200 and w.get("body", {}).get("schedule_enabled") is False
          and d["heads"][1]["schedule_enabled"] is False, (s, w))
    s, d = post("/api/head/2/schedule", {"enabled": "no"})
    check("schedule needs a bool", s == 400, s)
    post("/api/head/2/schedule", {"enabled": True})

    dose.writes.clear()
    s, d = post("/api/head/1/container", {"ml": 950})
    check("set container", s == 200 and dose.writes[-1]["body"]["container_volume"] == 950
          and d["heads"][0]["container_ml"] == 950, (s, dose.writes))
    dose.writes.clear()
    s, d = post("/api/head/3/container", {"ml": 300})
    check("container refused without stock tracking", s == 400 and not dose.writes, (s, d))

    s, d = post("/api/head/1/manual", {"ml": 2})
    check("manual dose", s == 200 and dose.writes[-1] == {
        "method": "POST", "path": "/head/1/manual", "body": {"manual_dose_scheduled": True, "volume": 2}}
        and d["heads"][0]["manual_today_ml"] == 2, (s, dose.writes))
    dose.writes.clear()
    for body, why in [({"ml": 0}, "zero"), ({"ml": 0.001}, "rounds to zero"), ({"ml": 10.5}, "over limit")]:
        s, d = post("/api/head/1/manual", body)
        check(f"manual {why} rejected", s == 400 and not dose.writes, (s, d))
    s, d = post("/api/head/4/manual", {"ml": 1})
    check("manual on not-set-up head refused", s == 400 and "not set up" in d["error"] and not dose.writes, (s, d))


def doser_failures(dose: MockDevice) -> None:
    print("doser failures")
    for fail in (500, "drop"):
        dose.writes.clear()
        dose.fail_next["/head/1/manual"] = fail
        s, d = post("/api/head/1/manual", {"ml": 1})
        check(f"manual dose {fail}: sent once, outcome unknown", s == 502 and len(dose.writes) == 1
              and "may or may not have run" in d["error"], (s, d, dose.writes))

    dose.writes.clear()
    dose.fail_next["/head/1/manual"] = 400
    s, d = post("/api/head/1/manual", {"ml": 1})
    check("manual dose refused", s == 502 and len(dose.writes) == 1 and "Nothing was changed" in d["error"], (s, d))

    dose.writes.clear()
    dose.fail_next["/head/2/settings"] = 500
    s, d = post("/api/head/2/daily", {"ml": 44})
    check("setting 500 is retried once", s == 200 and len(dose.writes) == 2, (s, d, dose.writes))

    dose.writes.clear()
    dose.fail_next["/head/2/settings"] = 503
    s, d = post("/api/head/2/daily", {"ml": 43})
    check("setting 503 refused, not retried", s == 502 and len(dose.writes) == 1
          and "Nothing was changed" in d["error"], (s, d))

    dose.writes.clear()
    real = gui.CONFIG["host"]
    gui.CONFIG["host"] = "127.0.0.1:1"
    s, d = post("/api/head/2/schedule", {"enabled": False})
    check("unreachable doser", s == 502 and "did not answer" in d["error"] and not dose.writes, (s, d))
    gui.CONFIG["host"] = real


def ato_changes(ato: MockDevice) -> None:
    print("ATO changes")
    s, d = post("/api/ato/autofill", {"enabled": False})
    check("auto fill off", s == 200 and ato.writes[-1] == {"method": "PUT", "path": "/configuration",
          "body": {"auto_fill": False}} and d["ato"]["auto_fill"] is False, (s, ato.writes))
    check("auto fill off flagged", any("Auto fill is off" in t for t in d["ato"]["attention"]), d["ato"])
    post("/api/ato/autofill", {"enabled": True})
    s, d = post("/api/ato/reservoir", {"ml": 20000})
    check("reservoir", s == 200 and ato.writes[-1]["body"] == {"volume": 20000}
          and d["ato"]["volume_left_ml"] == 20000, (s, ato.writes))
    ato.writes.clear()
    s, d = post("/api/ato/reservoir", {"ml": 200001})
    check("reservoir over limit", s == 400 and not ato.writes, s)
    s, d = post("/api/ato/resume")
    check("resume", s == 200 and ato.writes[-1]["path"] == "/resume" and ato.writes[-1]["body"] is None, (s, ato.writes))


def read_only_and_settings(dose: MockDevice, settings_file: Path) -> None:
    print("read-only and tube colors")
    gui.CONFIG["read_only"] = True
    dose.writes.clear()
    s, d = post("/api/head/1/manual", {"ml": 1})
    check("read-only refuses changes", s == 403 and not dose.writes, (s, d))
    s, d = req("GET", "/api/status")
    check("read-only status", s == 200 and d["read_only"] is True, s)
    s, d = post("/api/settings", {"tubes": "red-yellow"})
    check("tube colors allowed read-only", s == 200 and d["tubes"] == ["pink", "red", "orange", "yellow"]
          and d["saved"] is True, d)
    gui.CONFIG["read_only"] = False
    check("tube colors saved", json.loads(settings_file.read_text())["tubes"][0] == "pink")
    s, d = post("/api/settings", {"tubes": "purple,green"})
    check("partial tube list filled", s == 200 and d["tubes"] == ["purple", "green", "blue-green", "green"], d)
    s, d = post("/api/settings", {"tubes": "mauve"})
    check("bad tube color", s == 400, d)
    s, d = post("/api/settings", {"tubes": "red"}, token=False)
    check("tube colors need token", s == 403, s)


def main() -> None:
    global BASE
    gui.time = types.SimpleNamespace(sleep=lambda _s: None)  # no retry pauses
    tmp = tempfile.TemporaryDirectory()
    settings_file = Path(tmp.name) / "reefdose-bench.json"
    gui.SETTINGS_PATH = settings_file  # never touch the real saved choice
    dose, ato = MockDevice("dose").start(), MockDevice("ato").start()
    gui.CONFIG.update(host=dose.host, ato_host=ato.host, read_only=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
    BASE = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page_and_status(dose, ato)
        odd_answers(dose, ato)
        doser_changes(dose)
        doser_failures(dose)
        ato_changes(ato)
        read_only_and_settings(dose, settings_file)
    finally:
        server.shutdown()
        dose.stop()
        ato.stop()
        tmp.cleanup()
    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
