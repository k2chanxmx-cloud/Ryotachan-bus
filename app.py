import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

ODPT_TOKEN = os.getenv("ODPT_ACCESS_TOKEN", "").strip()
ODPT_API_BASE = os.getenv("ODPT_API_BASE", "https://api.odpt.org/api/v4").rstrip("/")
TARGET_STOP_NAME = os.getenv("TARGET_STOP_NAME", "亀戸七丁目")
TARGET_ROUTES = tuple(x.strip() for x in os.getenv("TARGET_ROUTES", "亀26,錦25,錦27").split(",") if x.strip())
TARGET_DESTINATIONS = tuple(x.strip() for x in os.getenv("TARGET_DESTINATIONS", "亀戸駅前,錦糸町駅前,両国駅前").split(",") if x.strip())
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "20"))
MINUTES_PER_STOP = float(os.getenv("MINUTES_PER_STOP", "1.8"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "12"))

CACHE: dict[str, tuple[float, Any]] = {}
JST = ZoneInfo("Asia/Tokyo")


def cached(key: str, ttl: int, loader):
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < ttl:
        return CACHE[key][1]
    value = loader()
    CACHE[key] = (now, value)
    return value


def odpt_get(resource: str, params: dict[str, str] | None = None) -> list[dict]:
    if not ODPT_TOKEN:
        raise RuntimeError("ODPT_ACCESS_TOKEN が設定されていません")
    query = dict(params or {})
    query["acl:consumerKey"] = ODPT_TOKEN
    response = requests.get(
        f"{ODPT_API_BASE}/{resource}", params=query, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("ODPT APIから想定外の応答が返りました")
    return data


def title_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("ja") or value.get("en") or next(iter(value.values()), ""))
    return ""


def compact_id(value: Any) -> str:
    text = str(value or "")
    return text.rsplit(":", 1)[-1]


def route_label(pattern: dict, route_titles: dict[str, str] | None = None) -> str:
    route_id = str(pattern.get("odpt:busroute") or "")
    candidates = [
        title_text(pattern.get("odpt:busroutePatternTitle")),
        title_text(pattern.get("dc:title")),
        (route_titles or {}).get(route_id, ""),
        (route_titles or {}).get(compact_id(route_id), ""),
        compact_id(route_id),
        compact_id(pattern.get("owl:sameAs")),
    ]
    merged = " ".join(candidates)
    for route in TARGET_ROUTES:
        if route in merged:
            return route
    # IDs sometimes omit Japanese characters; retain a readable fallback.
    return candidates[0] or compact_id(pattern.get("owl:sameAs"))


def destination_text(pattern: dict) -> str:
    values = [
        title_text(pattern.get("odpt:destinationSign")),
        title_text(pattern.get("odpt:busDirection")),
        title_text(pattern.get("odpt:busroutePatternTitle")),
        title_text(pattern.get("dc:title")),
    ]
    return " ".join(v for v in values if v)


def stop_name_from_order(item: dict, stop_titles: dict[str, str] | None = None) -> str:
    stop_id = str(item.get("odpt:busstopPole") or item.get("owl:sameAs") or "")
    return (
        title_text(item.get("odpt:busstopPoleTitle"))
        or (stop_titles or {}).get(stop_id, "")
        or (stop_titles or {}).get(compact_id(stop_id), "")
        or compact_id(stop_id)
    )


def stop_id_from_order(item: dict) -> str:
    return str(item.get("odpt:busstopPole") or item.get("owl:sameAs") or "")



def get_stop_titles() -> dict[str, str]:
    def load():
        rows = odpt_get("odpt:BusstopPole", {"odpt:operator": "odpt.Operator:Toei"})
        result: dict[str, str] = {}
        for row in rows:
            stop_id = str(row.get("owl:sameAs") or "")
            name = title_text(row.get("odpt:busstopPoleTitle")) or title_text(row.get("dc:title"))
            if stop_id and name:
                result[stop_id] = name
                result[compact_id(stop_id)] = name
        return result
    return cached("stop_titles", 6 * 60 * 60, load)


def get_patterns() -> list[dict]:
    def load():
        return odpt_get("odpt:BusroutePattern", {"odpt:operator": "odpt.Operator:Toei"})
    return cached("patterns", 6 * 60 * 60, load)


def selected_patterns() -> dict[str, dict]:
    """Select routes by their stop sequence and terminal stop.

    ODPT route titles/IDs are not stable enough to identify Japanese route labels.
    For this app, the destination terminal uniquely identifies each desired route:
      亀戸駅前 -> 亀26
      錦糸町駅前 -> 錦25
      両国駅前 -> 錦27
    """
    destination_to_route = {
        "亀戸駅前": "亀26",
        "錦糸町駅前": "錦25",
        "両国駅前": "錦27",
    }
    selected: dict[str, dict] = {}
    stop_titles = get_stop_titles()

    for pattern in get_patterns():
        orders = pattern.get("odpt:busstopPoleOrder") or []
        if not orders:
            continue

        names = [stop_name_from_order(x, stop_titles) for x in orders]
        target_indexes = [i for i, name in enumerate(names) if TARGET_STOP_NAME in name]
        if not target_indexes:
            continue

        # Determine direction from the actual final stop in the ordered stop list.
        terminal_name = names[-1] if names else ""
        resolved_destination = next(
            (dest for dest in TARGET_DESTINATIONS if dest in terminal_name),
            None,
        )

        # Some feeds expose the destination only in a metadata field.
        if resolved_destination is None:
            metadata_destination = destination_text(pattern)
            resolved_destination = next(
                (dest for dest in TARGET_DESTINATIONS if dest in metadata_destination),
                None,
            )

        if resolved_destination is None:
            continue

        resolved_route = destination_to_route.get(resolved_destination)
        if resolved_route not in TARGET_ROUTES:
            continue

        # The target must occur before the terminal stop.
        target_index = target_indexes[0]
        if target_index >= len(names) - 1:
            continue

        copied = dict(pattern)
        copied["_resolved_route"] = resolved_route
        copied["_resolved_destination"] = resolved_destination
        copied["_stop_titles"] = stop_titles

        pattern_id = str(pattern.get("owl:sameAs") or "")
        busroute_id = str(pattern.get("odpt:busroute") or "")
        for key in (busroute_id, pattern_id):
            if key:
                selected[key] = copied
                selected[compact_id(key)] = copied

    return selected


def get_buses() -> list[dict]:
    return odpt_get("odpt:Bus", {"odpt:operator": "odpt.Operator:Toei"})


def index_of_stop(orders: list[dict], stop_id: str | None, stop_name: str | None = None, stop_titles: dict[str, str] | None = None) -> int | None:
    if stop_id:
        for i, item in enumerate(orders):
            item_id = stop_id_from_order(item)
            if item_id == stop_id or compact_id(item_id) == compact_id(stop_id):
                return i
    if stop_name:
        for i, item in enumerate(orders):
            if stop_name in stop_name_from_order(item, stop_titles):
                return i
    return None


def build_arrival(bus: dict, pattern: dict) -> dict | None:
    orders = pattern.get("odpt:busstopPoleOrder") or []
    stop_titles = pattern.get("_stop_titles") or {}
    target_i = index_of_stop(orders, None, TARGET_STOP_NAME, stop_titles)
    if target_i is None:
        return None

    from_id = bus.get("odpt:fromBusstopPole")
    to_id = bus.get("odpt:toBusstopPole")
    from_i = index_of_stop(orders, from_id)
    to_i = index_of_stop(orders, to_id)

    # Prefer the next stop. A bus between stops A and B is counted from B to target.
    current_i = to_i if to_i is not None else from_i
    if current_i is None:
        return None
    stops_away = target_i - current_i
    if stops_away < 0:
        return None

    delay_sec = int(bus.get("odpt:delay") or 0)
    progress = bus.get("odpt:progress")
    try:
        progress_value = min(1.0, max(0.0, float(progress)))
    except (TypeError, ValueError):
        progress_value = 0.0

    # Current segment remainder + following segments. This is explicitly an estimate.
    segments = max(0.15, stops_away + (1.0 - progress_value if to_i is not None else 0.7))
    eta_minutes = max(1, round(segments * MINUTES_PER_STOP + delay_sec / 60))
    arrival_time = datetime.now(JST).timestamp() + eta_minutes * 60

    route = str(pattern.get("_resolved_route") or route_label(pattern))
    destination = destination_text(pattern)
    destination_name = str(pattern.get("_resolved_destination") or next((d for d in TARGET_DESTINATIONS if d in destination), "亀戸方面"))
    location_name = stop_name_from_order(orders[current_i], stop_titles)

    return {
        "route": route,
        "destination": destination_name,
        "minutes": eta_minutes,
        "stopsAway": stops_away,
        "location": location_name,
        "arrivalTime": datetime.fromtimestamp(arrival_time, JST).strftime("%H:%M"),
        "delayMinutes": round(delay_sec / 60),
        "vehicleId": compact_id(
            bus.get("odpt:busNumber")
            or bus.get("odpt:vehicleNumber")
            or bus.get("owl:sameAs")
        ),
        "isRealtime": True,
    }


@app.get("/")
def index():
    return render_template(
        "index.html",
        target_stop=TARGET_STOP_NAME,
        routes="・".join(TARGET_ROUTES),
        refresh_seconds=REFRESH_SECONDS,
    )


@app.get("/api/arrivals")
def arrivals():
    now = datetime.now(JST)
    try:
        patterns = selected_patterns()
        if not patterns:
            raise RuntimeError("対象方向の路線データを特定できませんでした")
        rows = []
        buses = get_buses()
        app.logger.info(
            "ODPT resolved: selected_pattern_keys=%s realtime_buses=%s",
            len(patterns), len(buses)
        )
        for bus in buses:
            # Toei odpt:Bus provides odpt:busroute. Older/other feeds may
            # additionally expose odpt:busroutePattern, so support both.
            route_id = str(bus.get("odpt:busroute") or "")
            pattern_id = str(bus.get("odpt:busroutePattern") or "")
            pattern = (
                patterns.get(route_id)
                or patterns.get(compact_id(route_id))
                or patterns.get(pattern_id)
                or patterns.get(compact_id(pattern_id))
            )
            if not pattern:
                continue
            row = build_arrival(bus, pattern)
            if row:
                rows.append(row)

        # Deduplicate vehicles and sort all three routes together by estimated arrival.
        unique = {}
        for row in rows:
            key = row["vehicleId"] or f'{row["route"]}-{row["arrivalTime"]}-{row["stopsAway"]}'
            unique[key] = row
        rows = sorted(unique.values(), key=lambda x: (x["minutes"], x["route"]))[:8]

        return jsonify({
            "ok": True,
            "stop": TARGET_STOP_NAME,
            "direction": "亀戸方面",
            "updatedAt": now.strftime("%H:%M:%S"),
            "refreshSeconds": REFRESH_SECONDS,
            "arrivals": rows,
            "message": "現在、亀戸七丁目へ接近中の対象車両はありません" if not rows else "",
            "debug": {
                "selectedPatternKeys": len(patterns),
                "realtimeBusCount": len(buses),
                "matchedArrivalCount": len(rows),
            },
            "estimateNote": "到着分数は車両位置・停留所数・遅延から算出した目安です。",
        })
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else 502
        request_url = response.request.url if response is not None and response.request is not None else "unknown"
        response_preview = (response.text or "")[:500] if response is not None else ""
        app.logger.error(
            "ODPT request failed: status=%s url=%s body=%s",
            status,
            request_url.replace(ODPT_TOKEN, "***") if ODPT_TOKEN else request_url,
            response_preview,
        )
        detail = "API認証に失敗しました" if status in (401, 403) else f"交通データを取得できませんでした（ODPT: {status}）"
        return jsonify({"ok": False, "updatedAt": now.strftime("%H:%M:%S"), "error": detail}), 502
    except Exception as exc:
        app.logger.exception("arrival API failed")
        return jsonify({"ok": False, "updatedAt": now.strftime("%H:%M:%S"), "error": str(exc)}), 500


@app.get("/api/diagnostics")
def diagnostics():
    """Return non-secret matching diagnostics for deployment troubleshooting."""
    now = datetime.now(JST)
    try:
        stop_titles = get_stop_titles()
        candidates = []
        for pattern in get_patterns():
            orders = pattern.get("odpt:busstopPoleOrder") or []
            names = [stop_name_from_order(x, stop_titles) for x in orders]
            if any(TARGET_STOP_NAME in name for name in names):
                candidates.append({
                    "patternId": compact_id(pattern.get("owl:sameAs")),
                    "busrouteId": compact_id(pattern.get("odpt:busroute")),
                    "title": route_label(pattern),
                    "metadataDestination": destination_text(pattern),
                    "firstStop": names[0] if names else "",
                    "lastStop": names[-1] if names else "",
                    "targetIndex": next((i for i, n in enumerate(names) if TARGET_STOP_NAME in n), None),
                    "stopCount": len(names),
                })
        selected = selected_patterns()
        return jsonify({
            "ok": True,
            "updatedAt": now.strftime("%H:%M:%S"),
            "targetStop": TARGET_STOP_NAME,
            "candidateCount": len(candidates),
            "selectedPatternKeys": len(selected),
            "candidates": candidates[:30],
        })
    except Exception as exc:
        app.logger.exception("diagnostics failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/health")
def health():
    return jsonify({"status": "ok", "tokenConfigured": bool(ODPT_TOKEN)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
