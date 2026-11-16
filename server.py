#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "httpx>=0.27"]
# ///
"""ReefBeat MCP server: local control of a Red Sea ReefDose and ReefATO+.

Talks to the devices' own HTTP API on your home network (no Red Sea cloud
account, nothing leaves the LAN). Unofficial: Red Sea does not publish or
support this API. Endpoints follow the open-source Home Assistant integration
Elwinmage/ha-reefbeat-component.

Configuration (environment variables):
  REEFDOSE_HOST            IP (or ip:port) of the ReefDose.
  REEFATO_HOST             IP (or ip:port) of the ReefATO+.
  REEFBEAT_ALLOW_WRITES    "1" to let tools change anything. Default: read-only.
  REEFDOSE_MAX_MANUAL_ML   Largest single manual dose allowed (default 10).
  REEFDOSE_MAX_DAILY_ML    Largest daily dose a head may be set to (default 100).

Every tool that changes something takes confirm=false by default and then
only returns a preview of what it would send.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise SystemExit(f"{name} must be greater than 0, got {raw!r}")
    return value


HOSTS: dict[str, str] = {
    "dose": os.environ.get("REEFDOSE_HOST", "").strip(),
    "ato": os.environ.get("REEFATO_HOST", "").strip(),
}
HOST_ENV = {"dose": "REEFDOSE_HOST", "ato": "REEFATO_HOST"}
EXPECTED_MODEL = {"dose": "RSDOSE", "ato": "RSATO"}

ALLOW_WRITES = os.environ.get("REEFBEAT_ALLOW_WRITES", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
MAX_MANUAL_ML = _env_float("REEFDOSE_MAX_MANUAL_ML", 10.0)
MAX_DAILY_ML = _env_float("REEFDOSE_MAX_DAILY_ML", 100.0)

# Hard ceilings, whatever the environment says. They match the limits the
# Home Assistant integration enforces for the same settings.
HARD_MAX_DOSE_ML = 300.0
MAX_CONTAINER_ML = 20_000
MAX_RESERVOIR_ML = 200_000

GET_TIMEOUT = 10.0
WRITE_TIMEOUT = 20.0
GET_ATTEMPTS = 3
RETRY_DELAY = 1.0

DeviceName = Literal["dose", "ato"]

mcp = MCPServer(
    "reefbeat",
    log_level="WARNING",
    instructions=(
        "Local control of a Red Sea ReefDose (dosing pump) and ReefATO+ "
        "(auto top-off) on the user's home network. Read tools are always "
        "safe. Tools that change something return a preview unless called "
        "with confirm=true; show the user the preview and get their go-ahead "
        "before confirming. Dosing puts chemicals into a live reef aquarium "
        "and cannot be undone, so never confirm a dose or a dose change the "
        "user did not explicitly ask for, and never repeat a manual dose "
        "whose outcome is unknown without checking dose_status first. "
        "Volumes are millilitres."
    ),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)
WRITE_ONCE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_limits: dict[str, asyncio.Semaphore] = {}
_head_locks: dict[int, asyncio.Lock] = {}


def _head_lock(head: int) -> asyncio.Lock:
    # Head settings are written back whole, so changes to one head must not overlap.
    if head not in _head_locks:
        _head_locks[head] = asyncio.Lock()
    return _head_locks[head]


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(follow_redirects=False)
    return _client


def _host(device: str) -> str:
    host = HOSTS.get(device, "")
    if not host:
        raise ToolError(
            f"No address configured for the {device} device. Set {HOST_ENV[device]} "
            "to its IP address (discover_devices can find it) and restart the server."
        )
    return host


def _limit(host: str) -> asyncio.Semaphore:
    # The devices are small microcontrollers: keep parallel requests low.
    if host not in _limits:
        _limits[host] = asyncio.Semaphore(3)
    return _limits[host]


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect"
    return f"{type(exc).__name__}: {exc}"


async def _get(device: str, path: str, *, optional: bool = False) -> Any:
    """GET a JSON endpoint, retrying a few times. `optional` returns None on failure."""
    host = _host(device)
    url = f"http://{host}{path}"
    last = "no attempt made"
    for attempt in range(GET_ATTEMPTS):
        if attempt:
            await asyncio.sleep(RETRY_DELAY)
        try:
            async with _limit(host):
                resp = await _http().get(url, timeout=GET_TIMEOUT)
        except httpx.HTTPError as exc:
            last = _describe(exc)
            continue
        if resp.status_code in (400, 404):
            last = f"HTTP {resp.status_code}"
            break
        if not 200 <= resp.status_code < 300:
            last = f"HTTP {resp.status_code}"
            continue
        try:
            return resp.json()
        except ValueError:
            last = "answer was not JSON"
            break
    if optional:
        return None
    raise ToolError(
        f"Could not read {path} from the {device} device at {host} ({last}). "
        "Check that it is powered, on Wi-Fi, and reachable from this computer."
    )


_NO_BODY = object()


async def _send(
    device: str,
    method: str,
    path: str,
    payload: Any = _NO_BODY,
    *,
    attempts: int = 1,
) -> dict[str, Any]:
    """Send a write request. Raises ToolError on failure.

    `attempts` above 1 is only used for requests that are safe to repeat
    (setting a value). A manual dose is sent exactly once.
    """
    host = _host(device)
    url = f"http://{host}{path}"
    kwargs: dict[str, Any] = {"timeout": WRITE_TIMEOUT}
    if payload is not _NO_BODY:
        kwargs["json"] = payload
    last = "no attempt made"
    sent_any = False
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(RETRY_DELAY)
        try:
            async with _limit(host):
                resp = await _http().request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            last = _describe(exc)  # never reached the device
            continue
        except httpx.HTTPError as exc:
            sent_any = True
            last = _describe(exc)
            continue
        sent_any = True
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text.strip() or None
        if 200 <= resp.status_code < 300:
            return {"http_status": resp.status_code, "device_answer": body}
        last = f"HTTP {resp.status_code}: {body}"
        refused = isinstance(body, dict) and body.get("success") is False
        if 400 <= resp.status_code < 500 or resp.status_code == 503 or refused:
            raise ToolError(
                f"The {device} device refused {method} {path} ({last}). Nothing was changed."
            )
    if sent_any:
        raise ToolError(
            f"{method} {path} to the {device} device at {host} failed ({last}). "
            "The request may or may not have been applied: read the current state "
            "before trying again."
        )
    raise ToolError(
        f"Could not reach the {device} device at {host} ({last}). Nothing was sent."
    )


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    seconds = int(seconds) % 86400
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def _num(value: float) -> int | float:
    """Send whole numbers as integers, like the Red Sea app does."""
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


# --------------------------------------------------------------------------
# Write guard
# --------------------------------------------------------------------------


def _preview(
    summary: str, request: dict[str, Any], **context: Any
) -> dict[str, Any]:
    note = (
        "Nothing was sent. Call again with confirm=true to do it."
        if ALLOW_WRITES
        else "Nothing was sent. This server is read-only: set REEFBEAT_ALLOW_WRITES=1 "
        "in its configuration and restart it to allow changes."
    )
    return {
        "status": "preview",
        "would_do": summary,
        "request": request,
        "writes_enabled": ALLOW_WRITES,
        "note": note,
        **context,
    }


def _require_writes() -> None:
    if not ALLOW_WRITES:
        raise ToolError(
            "This server is read-only. Set REEFBEAT_ALLOW_WRITES=1 in its "
            "configuration and restart it to allow changes. Nothing was sent."
        )


# --------------------------------------------------------------------------
# Shared reads
# --------------------------------------------------------------------------


async def _device_info(device: str) -> dict[str, Any]:
    info = await _get(device, "/device-info")
    if not isinstance(info, dict):
        raise ToolError(f"Unexpected /device-info answer from the {device} device.")
    model = str(info.get("hw_model", ""))
    if not model.startswith(EXPECTED_MODEL[device]):
        raise ToolError(
            f"{HOST_ENV[device]} points at a {model or 'unknown device'}, not a "
            f"{'ReefDose' if device == 'dose' else 'ReefATO+'}. Fix the address "
            "before using this tool."
        )
    return info


async def _mode(device: str) -> str | None:
    data = await _get(device, "/mode", optional=True)
    return data.get("mode") if isinstance(data, dict) else None


async def _dose_heads() -> tuple[int, dict[str, Any]]:
    """Return (number of heads, dashboard)."""
    info, dash = await asyncio.gather(_device_info("dose"), _get("dose", "/dashboard"))
    model = str(info.get("hw_model", ""))
    if model.endswith("2"):
        count = 2
    elif model.endswith("4"):
        count = 4
    else:
        count = len(dash.get("heads", {})) if isinstance(dash, dict) else 0
    if not isinstance(dash, dict) or count == 0:
        raise ToolError("Could not work out how many heads the ReefDose has.")
    return count, dash


async def _check_head(head: int) -> tuple[int, dict[str, Any]]:
    count, dash = await _dose_heads()
    if not 1 <= head <= count:
        raise ToolError(f"head must be between 1 and {count}, got {head}.")
    return count, dash


async def _head_settings(head: int) -> dict[str, Any]:
    settings = await _get("dose", f"/head/{head}/settings")
    if not isinstance(settings, dict):
        raise ToolError(f"Unexpected settings answer for head {head}.")
    return settings


def _head_summary(
    head: int, dash_head: dict[str, Any], settings: dict[str, Any] | None
) -> dict[str, Any]:
    settings = settings or {}
    supplement = settings.get("supplement") or {}
    schedule = settings.get("schedule") or {}
    out: dict[str, Any] = {
        "head": head,
        "supplement": dash_head.get("supplement") or supplement.get("name"),
        "short_name": supplement.get("short_name"),
        "state": dash_head.get("state"),
        "schedule_enabled": settings.get("schedule_enabled"),
        "daily_dose_ml": dash_head.get("daily_dose", schedule.get("dd")),
        "doses_per_day": dash_head.get("daily_doses"),
        "doses_done_today": dash_head.get("doses_today"),
        "auto_dosed_today_ml": dash_head.get("auto_dosed_today"),
        "manual_dosed_today_ml": dash_head.get("manual_dosed_today"),
        "missed_dose": dash_head.get("missed_dose"),
        "container_volume_ml": settings.get("container_volume"),
        "stock_monitoring": settings.get("slm"),
        "stock_level": dash_head.get("stock_level"),
        "remaining_days": dash_head.get("remaining_days"),
        "recalibration_required": dash_head.get("recalibration_required"),
        "last_calibrated": _iso(settings.get("last_calibrated")),
        "is_food_head": dash_head.get("is_food_head"),
    }
    return out


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------


@mcp.tool(annotations=READ)
async def list_devices() -> dict[str, Any]:
    """Show the configured ReefDose and ReefATO+: address, model, name,
    firmware, mode, and whether each one answers. Also reports whether this
    server is allowed to change anything and the dose limits in force."""

    async def one(device: str) -> dict[str, Any]:
        host = HOSTS[device]
        if not host:
            return {"configured": False, "set": HOST_ENV[device]}
        info = await _get(device, "/device-info", optional=True)
        if not isinstance(info, dict):
            return {"configured": True, "host": host, "reachable": False}
        firmware, mode = await asyncio.gather(
            _get(device, "/firmware", optional=True), _mode(device)
        )
        model = str(info.get("hw_model", ""))
        return {
            "configured": True,
            "host": host,
            "reachable": True,
            "model": model,
            "is_expected_model": model.startswith(EXPECTED_MODEL[device]),
            "name": info.get("name"),
            "hwid": info.get("hwid"),
            "firmware": firmware.get("version") if isinstance(firmware, dict) else None,
            "mode": mode,
        }

    dose, ato = await asyncio.gather(one("dose"), one("ato"))
    return {
        "dose": dose,
        "ato": ato,
        "writes_enabled": ALLOW_WRITES,
        "limits": {
            "max_manual_dose_ml": min(MAX_MANUAL_ML, HARD_MAX_DOSE_ML),
            "max_daily_dose_ml": min(MAX_DAILY_ML, HARD_MAX_DOSE_ML),
        },
    }


def _local_subnet() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            ip = s.getsockname()[0]
    except OSError:
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


@mcp.tool(annotations=READ)
async def discover_devices(subnet: str | None = None) -> dict[str, Any]:
    """Scan the home network for Red Sea ReefBeat devices and return their IP
    addresses, models and names. Use this to find the addresses to put in
    REEFDOSE_HOST and REEFATO_HOST. `subnet` is a private IPv4 range in CIDR
    form such as 192.168.1.0/24; left out, this computer's own /24 is used.
    Takes about ten seconds."""
    cidr = subnet or _local_subnet()
    if not cidr:
        raise ToolError("Could not work out the local network. Pass subnet, e.g. 192.168.1.0/24.")
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        raise ToolError(f"{cidr!r} is not a valid network. Example: 192.168.1.0/24.") from None
    if net.version != 4 or not net.is_private:
        raise ToolError("Only private IPv4 home networks can be scanned.")
    if net.num_addresses > 1024:
        raise ToolError("Network too large to scan: use a /22 or smaller.")

    sem = asyncio.Semaphore(64)
    found: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=1.5, follow_redirects=False) as client:

        async def probe(ip: str) -> None:
            async with sem:
                try:
                    resp = await client.get(f"http://{ip}/device-info")
                    info = resp.json() if resp.status_code == 200 else None
                except (httpx.HTTPError, ValueError):
                    return
            if isinstance(info, dict) and str(info.get("hw_model", "")).startswith("RS"):
                found.append(
                    {
                        "ip": ip,
                        "model": info.get("hw_model"),
                        "name": info.get("name"),
                        "hwid": info.get("hwid"),
                    }
                )

        await asyncio.gather(*(probe(str(ip)) for ip in net.hosts()))

    found.sort(key=lambda d: ipaddress.ip_address(d["ip"]))
    return {
        "scanned": str(net),
        "devices": found,
        "hint": "RSDOSE2/RSDOSE4 is the ReefDose (REEFDOSE_HOST); RSATO+ is the ReefATO+ (REEFATO_HOST).",
    }


@mcp.tool(annotations=READ)
async def dose_status() -> dict[str, Any]:
    """Current state of the ReefDose: mode, battery, and for every head the
    supplement, daily dose, how much was dosed today (scheduled and manual),
    missed doses, container volume left, days of stock remaining, and whether
    recalibration is due. Volumes are millilitres."""
    count, dash = await _dose_heads()
    mode, *settings = await asyncio.gather(
        _mode("dose"),
        *(_get("dose", f"/head/{h}/settings", optional=True) for h in range(1, count + 1)),
    )
    heads = dash.get("heads", {})
    return {
        "mode": mode,
        "battery_level": dash.get("battery_level"),
        "bundled_heads": dash.get("bundled_heads"),
        "time_error": dash.get("time_error"),
        "heads": [
            _head_summary(
                h,
                heads.get(str(h), {}),
                settings[h - 1] if isinstance(settings[h - 1], dict) else None,
            )
            for h in range(1, count + 1)
        ],
    }


@mcp.tool(annotations=READ)
async def dose_head_settings(head: int) -> dict[str, Any]:
    """Full settings of one ReefDose head exactly as the device reports them,
    including the dosing schedule (days, time windows, doses per window),
    supplement details and calibration data."""
    await _check_head(head)
    return await _head_settings(head)


@mcp.tool(annotations=READ)
async def dose_queue() -> dict[str, Any]:
    """Doses the ReefDose still has queued for today, in order: time of day
    (device local time), head, volume in millilitres, and whether each is a
    scheduled or manual dose."""
    queue, settings = await asyncio.gather(
        _get("dose", "/dosing-queue"), _get("dose", "/device-settings", optional=True)
    )
    items = [
        {
            "time": _clock(item.get("time")),
            "head": item.get("head"),
            "volume_ml": item.get("volume"),
            "type": item.get("dose_type"),
        }
        for item in (queue if isinstance(queue, list) else [])
        if isinstance(item, dict)
    ]
    return {
        "queued": items,
        "seconds_between_doses": settings.get("dosing_waiting_period")
        if isinstance(settings, dict)
        else None,
    }


@mcp.tool(annotations=READ)
async def dose_history() -> dict[str, Any]:
    """Total volume each ReefDose head dosed per day over roughly the last
    week (millilitres), newest first."""
    (count, dash), log = await asyncio.gather(_dose_heads(), _get("dose", "/daily-log"))
    heads = dash.get("heads", {})
    names = {str(h): heads.get(str(h), {}).get("supplement") for h in range(1, count + 1)}
    days = []
    for entry in (log.get("daily", []) if isinstance(log, dict) else []):
        if not isinstance(entry, dict):
            continue
        date = _iso(entry.get("date"))
        days.append(
            {
                "date": date[:10] if date else None,
                "dosed_ml": {h: entry.get(h) for h in names if h in entry},
            }
        )
    return {"heads": names, "days": days}


@mcp.tool(annotations=READ)
async def ato_status() -> dict[str, Any]:
    """Current state of the ReefATO+: mode, whether auto fill is on, water
    level, water temperature, reservoir volume left and days until empty,
    today's fills and usage, pump state, leak sensor, and a list of anything
    that needs attention. Volumes are millilitres, temperature is Celsius."""
    _, dash, config, mode = await asyncio.gather(
        _device_info("ato"),
        _get("ato", "/dashboard"),
        _get("ato", "/configuration", optional=True),
        _mode("ato"),
    )
    if not isinstance(dash, dict):
        raise ToolError("Unexpected /dashboard answer from the ReefATO+.")
    config = config if isinstance(config, dict) else {}
    sensor = dash.get("ato_sensor") or {}
    leak = dash.get("leak_sensor") or {}
    mode = mode or dash.get("mode")
    auto_fill = config.get("auto_fill")
    temp_ok = sensor.get("temperature_probe_status") == "connected"

    attention: list[str] = []
    if leak.get("status") not in (None, "dry"):
        attention.append(f"Leak sensor reports: {leak.get('status')}")
    if leak.get("connected") is False:
        attention.append("Leak sensor is not connected")
    elif leak.get("enabled") is False:
        attention.append("Leak sensor is disabled")
    if sensor.get("connected") is False:
        attention.append("Water level sensor is not connected")
    if sensor.get("is_sensor_error") or dash.get("check_sensor"):
        attention.append("Water level sensor needs checking")
    if mode not in (None, "auto"):
        attention.append(f"Device mode is {mode}, not auto")
    if auto_fill is False:
        attention.append("Auto fill is turned off")
    days = dash.get("days_till_empty")
    if isinstance(days, (int, float)) and days <= 2:
        attention.append(f"Reservoir runs out in about {days} day(s)")

    return {
        "mode": mode,
        "auto_fill": auto_fill,
        "water_level": dash.get("water_level"),
        "sensor_level": sensor.get("current_level"),
        "temperature_c": round(sensor["current_read"], 2)
        if temp_ok and isinstance(sensor.get("current_read"), (int, float))
        else None,
        "reservoir": {
            "volume_left_ml": dash.get("volume_left"),
            "days_till_empty": days,
            "daily_volume_average_ml": dash.get("daily_volume_average"),
        },
        "today": {
            "fills": dash.get("today_fills"),
            "volume_used_ml": dash.get("today_volume_usage"),
        },
        "total_fills": dash.get("total_fills"),
        "pump": {
            "is_on": dash.get("is_pump_on"),
            "state": dash.get("pump_state"),
            "last_on_cause": dash.get("last_pump_on_cause"),
            "last_fill": _iso(dash.get("last_fill_date")),
        },
        "leak_sensor": {
            "status": leak.get("status"),
            "connected": leak.get("connected"),
            "enabled": leak.get("enabled"),
            "buzzer_enabled": leak.get("buzzer_enabled"),
        },
        "needs_attention": attention,
    }


_SAFE_PATH = re.compile(r"^/[A-Za-z0-9_\-./]{0,120}$")


@mcp.tool(annotations=READ)
async def device_get(target: DeviceName, path: str) -> Any:
    """Read any endpoint of one device (target "dose" or "ato") as raw JSON (read-only GET), for details
    the other tools do not surface. Useful paths: /dashboard, /device-info,
    /firmware, /mode, /wifi; on the doser also /device-settings,
    /dosing-queue, /daily-log, /head/1/settings; on the ATO also
    /configuration."""
    if not _SAFE_PATH.match(path) or ".." in path or "//" in path:
        raise ToolError("path must look like /dashboard or /head/1/settings.")
    return await _get(target, path)


# --------------------------------------------------------------------------
# ReefDose write tools
# --------------------------------------------------------------------------


@mcp.tool(annotations=WRITE_ONCE)
async def dose_manual(head: int, volume_ml: float, confirm: bool = False) -> dict[str, Any]:
    """Dose a one-off volume from one ReefDose head now, on top of its
    schedule. This adds supplement to the aquarium and cannot be undone.
    Without confirm=true it only previews. The dose is sent once and never
    retried: if the result is unknown, check dose_status before dosing again."""
    _, dash = await _check_head(head)
    limit = min(MAX_MANUAL_ML, HARD_MAX_DOSE_ML)
    if not 0 < volume_ml <= limit:
        raise ToolError(
            f"volume_ml must be above 0 and at most {limit:g} ml "
            "(the REEFDOSE_MAX_MANUAL_ML limit). Nothing was sent."
        )
    volume = _num(volume_ml)
    if volume <= 0:
        raise ToolError("volume_ml is too small to dose. Nothing was sent.")
    dash_head = dash.get("heads", {}).get(str(head), {})
    state = dash_head.get("state")
    context = {
        "head": head,
        "supplement": dash_head.get("supplement"),
        "head_state": state,
        "daily_dose_ml": dash_head.get("daily_dose"),
        "auto_dosed_today_ml": dash_head.get("auto_dosed_today"),
        "manual_dosed_today_ml": dash_head.get("manual_dosed_today"),
    }
    if state == "not-setup":
        raise ToolError(f"Head {head} is not set up, so it cannot dose. Nothing was sent.")
    path = f"/head/{head}/manual"
    payload = {"manual_dose_scheduled": True, "volume": volume}
    if not confirm:
        return _preview(
            f"Dose {volume} ml of {context['supplement'] or 'the supplement'} from head {head} now",
            {"method": "POST", "path": path, "body": payload},
            **context,
        )
    _require_writes()
    try:
        result = await _send("dose", "POST", path, payload, attempts=1)
    except ToolError as exc:
        raise ToolError(f"{exc} Do not repeat this dose without checking dose_status.") from None
    after = await _get("dose", "/dashboard", optional=True)
    after_head = (after or {}).get("heads", {}).get(str(head), {}) if isinstance(after, dict) else {}
    return {
        "status": "sent",
        "dosed_ml": volume,
        **context,
        "manual_dosed_today_ml_now": after_head.get("manual_dosed_today"),
        "note": "The doser accepted the dose. It runs manual doses through its queue, "
        "so the total dosed today can take a few minutes to update.",
        **result,
    }


async def _update_head(
    head: int,
    confirm: bool,
    summary: str,
    change: dict[str, Any],
    current: dict[str, Any],
    settings: dict[str, Any],
    bundled: bool,
) -> dict[str, Any]:
    """PUT a head's full settings back with `change` applied (the device
    expects the whole object, as the Home Assistant integration sends it)."""
    path = f"/head/{head}/settings"
    supplement = (settings.get("supplement") or {}).get("name")
    if not confirm:
        return _preview(
            summary,
            {"method": "PUT", "path": path, "changed_fields": change},
            head=head,
            supplement=supplement,
            current=current,
        )
    _require_writes()
    async with _head_lock(head):
        # Re-read inside the lock so a change made meanwhile is not overwritten.
        payload = copy.deepcopy(await _head_settings(head))
        for key, value in change.items():
            if isinstance(value, dict):
                payload.setdefault(key, {}).update(value)
            else:
                payload[key] = value
        if bundled:
            for field in ("supplement", "is_food_head", "food_delay"):
                payload.pop(field, None)
        result = await _send("dose", "PUT", path, payload, attempts=2)
        after = await _get("dose", path, optional=True)
    return {
        "status": "done",
        "head": head,
        "supplement": supplement,
        "before": current,
        "requested": change,
        "settings_now": {
            "daily_dose_ml": (after.get("schedule") or {}).get("dd"),
            "schedule_enabled": after.get("schedule_enabled"),
            "container_volume_ml": after.get("container_volume"),
        }
        if isinstance(after, dict)
        else None,
        **result,
    }


@mcp.tool(annotations=WRITE)
async def dose_set_daily_dose(head: int, daily_ml: float, confirm: bool = False) -> dict[str, Any]:
    """Change how much one ReefDose head doses per day (millilitres). The
    head's schedule spreads the new total over its existing dose times.
    Without confirm=true it only previews the change."""
    _, dash = await _check_head(head)
    limit = min(MAX_DAILY_ML, HARD_MAX_DOSE_ML)
    if not 0 <= daily_ml <= limit:
        raise ToolError(
            f"daily_ml must be between 0 and {limit:g} ml "
            "(the REEFDOSE_MAX_DAILY_ML limit). Nothing was sent."
        )
    bundled = bool(dash.get("bundled_heads"))
    if bundled and head > 1:
        raise ToolError(
            "The heads are bundled: only head 1 has a daily dose and the doser "
            "derives the others from it. Nothing was sent."
        )
    settings = await _head_settings(head)
    schedule = settings.get("schedule")
    if not isinstance(schedule, dict) or "dd" not in schedule:
        raise ToolError(f"Head {head} has no dosing schedule to change. Nothing was sent.")
    new = _num(daily_ml)
    return await _update_head(
        head,
        confirm,
        f"Change head {head} daily dose from {schedule['dd']} ml to {new} ml",
        {"schedule": {"dd": new}},
        {"daily_dose_ml": schedule["dd"]},
        settings,
        bundled,
    )


@mcp.tool(annotations=WRITE)
async def dose_set_schedule_enabled(head: int, enabled: bool, confirm: bool = False) -> dict[str, Any]:
    """Pause (enabled=false) or resume (enabled=true) the automatic dosing
    schedule of one ReefDose head. Without confirm=true it only previews."""
    _, dash = await _check_head(head)
    settings = await _head_settings(head)
    if "schedule_enabled" not in settings:
        raise ToolError(f"Head {head} does not report a schedule switch. Nothing was sent.")
    return await _update_head(
        head,
        confirm,
        f"{'Resume' if enabled else 'Pause'} the dosing schedule of head {head}",
        {"schedule_enabled": bool(enabled)},
        {"schedule_enabled": settings["schedule_enabled"]},
        settings,
        bool(dash.get("bundled_heads")),
    )


@mcp.tool(annotations=WRITE)
async def dose_set_container_volume(head: int, volume_ml: float, confirm: bool = False) -> dict[str, Any]:
    """Tell the ReefDose how much supplement is in one head's container
    (millilitres), for example after a refill, so its stock tracking and
    days-remaining estimate are right. Without confirm=true it only previews."""
    _, dash = await _check_head(head)
    if not 0 <= volume_ml <= MAX_CONTAINER_ML:
        raise ToolError(f"volume_ml must be between 0 and {MAX_CONTAINER_ML} ml. Nothing was sent.")
    settings = await _head_settings(head)
    if not settings.get("slm"):
        raise ToolError(
            f"Stock monitoring is off for head {head}, so it does not track a "
            "container volume. Turn it on in the ReefBeat app first. Nothing was sent."
        )
    new = _num(volume_ml)
    return await _update_head(
        head,
        confirm,
        f"Set head {head} container volume to {new} ml",
        {"container_volume": new},
        {"container_volume_ml": settings.get("container_volume")},
        settings,
        bool(dash.get("bundled_heads")),
    )


# --------------------------------------------------------------------------
# ReefATO+ write tools
# --------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def ato_set_auto_fill(enabled: bool, confirm: bool = False) -> dict[str, Any]:
    """Turn the ReefATO+ automatic top-off on or off. While off, evaporated
    water is not replaced and salinity rises. Without confirm=true it only
    previews."""
    await _device_info("ato")
    config = await _get("ato", "/configuration")
    current = config.get("auto_fill") if isinstance(config, dict) else None
    payload = {"auto_fill": bool(enabled)}
    if not confirm:
        return _preview(
            f"Turn ATO auto fill {'on' if enabled else 'off'}",
            {"method": "PUT", "path": "/configuration", "body": payload},
            current={"auto_fill": current},
        )
    _require_writes()
    result = await _send("ato", "PUT", "/configuration", payload, attempts=2)
    after = await _get("ato", "/configuration", optional=True)
    return {
        "status": "done",
        "before": {"auto_fill": current},
        "auto_fill_now": after.get("auto_fill") if isinstance(after, dict) else None,
        **result,
    }


@mcp.tool(annotations=WRITE)
async def ato_set_reservoir_volume(volume_ml: int, confirm: bool = False) -> dict[str, Any]:
    """Tell the ReefATO+ how much fresh water is in its reservoir
    (millilitres), for example after refilling it, so the volume-left and
    days-until-empty figures are right. Without confirm=true it only previews."""
    await _device_info("ato")
    if not 0 <= volume_ml <= MAX_RESERVOIR_ML:
        raise ToolError(f"volume_ml must be between 0 and {MAX_RESERVOIR_ML} ml. Nothing was sent.")
    dash = await _get("ato", "/dashboard")
    current = dash.get("volume_left") if isinstance(dash, dict) else None
    payload = {"volume": int(volume_ml)}
    if not confirm:
        return _preview(
            f"Set the ATO reservoir volume to {int(volume_ml)} ml",
            {"method": "POST", "path": "/update-volume", "body": payload},
            current={"volume_left_ml": current},
        )
    _require_writes()
    result = await _send("ato", "POST", "/update-volume", payload, attempts=2)
    after = await _get("ato", "/dashboard", optional=True)
    return {
        "status": "done",
        "before": {"volume_left_ml": current},
        "volume_left_ml_now": after.get("volume_left") if isinstance(after, dict) else None,
        **result,
    }


@mcp.tool(annotations=WRITE)
async def ato_resume(confirm: bool = False) -> dict[str, Any]:
    """Resume the ReefATO+ after it stopped itself, for example once an empty
    reservoir has been refilled. Clears the stop and lets it top off again.
    Without confirm=true it only previews."""
    await _device_info("ato")
    dash = await _get("ato", "/dashboard")
    current = {
        "mode": dash.get("mode"),
        "pump_state": dash.get("pump_state"),
        "water_level": dash.get("water_level"),
    } if isinstance(dash, dict) else {}
    if not confirm:
        return _preview(
            "Resume ATO operation",
            {"method": "POST", "path": "/resume"},
            current=current,
        )
    _require_writes()
    result = await _send("ato", "POST", "/resume", attempts=2)
    after = await _get("ato", "/dashboard", optional=True)
    return {
        "status": "done",
        "before": current,
        "now": {"mode": after.get("mode"), "pump_state": after.get("pump_state")}
        if isinstance(after, dict)
        else None,
        **result,
    }


# --------------------------------------------------------------------------
# Mode (both devices)
# --------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def set_device_mode(
    target: DeviceName,
    mode: Literal["auto", "off", "maintenance"],
    confirm: bool = False,
) -> dict[str, Any]:
    """Switch the ReefDose or ReefATO+ between normal operation ("auto"),
    "off" (doser stops dosing; ATO stops topping off) and "maintenance"
    (paused for work on the tank, e.g. a water change). Remember to put it
    back to "auto" afterwards. Without confirm=true it only previews."""
    device = target  # "device" as an argument name is dropped by the Claude desktop bridge
    await _device_info(device)
    current = await _mode(device)
    if current is None:
        raise ToolError(f"Could not read the current mode of the {device} device. Nothing was sent.")
    steps: list[tuple[str, str, Any]] = []
    if mode == current:
        return {"status": "unchanged", "device": device, "mode": current}
    if current == "off":
        steps.append(("DELETE", "/off", _NO_BODY))
    elif current == "maintenance":
        steps.append(("DELETE", "/maintenance", _NO_BODY))
    elif mode == "auto":
        raise ToolError(
            f"The {device} device is in {current!r} mode, which this tool cannot "
            "leave. Use the ReefBeat app. Nothing was sent."
        )
    if mode == "off":
        steps.append(("POST", "/off", ""))
    elif mode == "maintenance":
        steps.append(("POST", "/maintenance", ""))
    shown = [{"method": m, "path": p} for m, p, _ in steps]
    if not confirm:
        return _preview(
            f"Switch the {device} device from {current} to {mode}",
            {"steps": shown},
            device=device,
            current={"mode": current},
        )
    _require_writes()
    for method, path, body in steps:
        await _send(device, method, path, body, attempts=2)
    now = await _mode(device)
    return {
        "status": "done",
        "device": device,
        "before": {"mode": current},
        "requested": mode,
        "mode_now": now,
        "sent": shown,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
