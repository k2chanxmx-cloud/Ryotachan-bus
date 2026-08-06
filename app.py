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


def route_label(pattern: dict) -> str:
    candidates = [
        title_text(pattern.get("odpt:busroutePatternTitle")),
        title_text(pattern.get("dc:title")),
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


def stop_name_from_order(item: dict) -> str:
    return title_text(item.get("odpt:busstopPoleTitle")) or compact_id(item.get("odpt:busstopPole"))


def stop_id_from_order(item: dict) -> str:
    return str(item.get("odpt:busstopPole") or item.get("owl:sameAs") or "")


def get_patterns() -> list[dict]:
    def load():
        return odpt_get("odpt:BusroutePattern", {"odpt:operator": "odpt.Operator:Toei"})
    return cached("patterns", 6 * 60 * 60, load)


def selected_patterns() -> dict[str, dict]:
    selected: dict[str, dict] = {}
    for pattern in get_patterns():
        route = route_label(pattern)
        if route not in TARGET_ROUTES:
            continue
        orders = pattern.get("odpt:busstopPoleOrder") or []
        names = [stop_name_from_order(x) for x in orders]
        if not any(TARGET_STOP_NAME in name for name in names):
            continue
        destination = destination_text(pattern)
        # Direction filter: routes on the opposite side do not head toward these termini.
        if TARGET_DESTINATIONS and not any(dest in destination for dest in TARGET_DESTINATIONS):
            # Some records omit destination text. Accept patterns where target is near route end.
            target_indexes = [i for i, n in enumerate(names) if TARGET_STOP_NAME in n]
            if not target_indexes or target_indexes[0] > len(names) * 0.7:
                continue
        selected[str(pattern.get("owl:sameAs"))] = pattern
    return selected


def get_buses() -> list[dict]:
    return odpt_get("odpt:Bus", {"odpt:operator": "odpt.Operator:Toei"})


def index_of_stop(orders: list[dict], stop_id: str | None, stop_name: str | None = None) -> int | None:
    if stop_id:
        for i, item in enumerate(orders):
            item_id = stop_id_from_order(item)
            if item_id == stop_id or compact_id(item_id) == compact_id(stop_id):
                return i
    if stop_name:
        for i, item in enumerate(orders):
            if stop_name in stop_name_from_order(item):
                return i
    return None


def build_arrival(bus: dict, pattern: dict) -> dict | None:
    orders = pattern.get("odpt:busstopPoleOrder") or []
    target_i = index_of_stop(orders, None, TARGET_STOP_NAME)
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

    route = route_label(pattern)
    destination = destination_text(pattern)
    destination_name = next((d for d in TARGET_DESTINATIONS if d in destination), "亀戸方面")
    location_name = stop_name_from_order(orders[current_i])

    return {
        "route": route,
        "destination": destination_name,
        "minutes": eta_minutes,
        "stopsAway": stops_away,
        "location": location_name,
        "arrivalTime": datetime.fromtimestamp(arrival_time, JST).strftime("%H:%M"),
        "delayMinutes": round(delay_sec / 60),
        "vehicleId": compact_id(bus.get("odpt:vehicleNumber") or bus.get("owl:sameAs")),
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
        for bus in get_buses():
            pattern_id = str(bus.get("odpt:busroutePattern") or "")
            pattern = patterns.get(pattern_id)
            if not pattern:
                # ODPT IDs can differ by prefix versions; compare compact tail as fallback.
                pattern = next((p for pid, p in patterns.items() if compact_id(pid) == compact_id(pattern_id)), None)
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
            "message": "接近中の車両が見つかりません" if not rows else "",
            "estimateNote": "到着分数は車両位置・停留所数・遅延から算出した目安です。",
        })
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        detail = "API認証に失敗しました" if status in (401, 403) else "交通データを取得できませんでした"
        return jsonify({"ok": False, "updatedAt": now.strftime("%H:%M:%S"), "error": detail}), 502
    except Exception as exc:
        app.logger.exception("arrival API failed")
        return jsonify({"ok": False, "updatedAt": now.strftime("%H:%M:%S"), "error": str(exc)}), 500


@app.get("/health")
def health():
    return jsonify({"status": "ok", "tokenConfigured": bool(ODPT_TOKEN)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
