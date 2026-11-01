"""
EDGE-RESQ MCP tools: help someone in a disaster find a nearby safe place and
make a calm, ordered decision instead of panicking.

Standalone on purpose: it imports nothing from the rest of the app, so it runs
from anywhere.  Only needs `mcp[cli]` and `httpx`.

Data sources (all free, no API key):
  - OpenStreetMap Overpass : hospitals, shelters, assembly points, police, fire stations
  - OpenStreetMap Nominatim: place name -> coordinates
  - Open-Meteo             : elevation (high ground) and short-term rain/wind
  - USGS                   : earthquakes in the last 24 h nearby

Run:
  python mcp_tools.py                      # stdio (Claude Desktop, MCP clients)
  mcp dev mcp_tools.py                     # browser inspector for testing
  MCP_TRANSPORT=streamable-http python mcp_tools.py   # HTTP on :8001/mcp
"""
from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "edge-resq-safety",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8001")),
)

HTTP_TIMEOUT = float(os.getenv("MCP_HTTP_TIMEOUT", "15"))
USER_AGENT = "edge-resq-mcp/0.1 (disaster safety helper)"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

DISCLAIMER = (
    "Map data is crowdsourced and may be out of date. Official instructions "
    "(police, disaster authority, sirens) always override this guidance."
)

EMERGENCY_NUMBERS: dict[str, dict[str, str]] = {
    "IN": {"All emergencies": "112", "Ambulance": "108", "Fire": "101",
           "Police": "100", "Disaster management (NDMA)": "1078"},
    "US": {"All emergencies": "911"},
    "GB": {"All emergencies": "999 or 112"},
    "AU": {"All emergencies": "000"},
    "NP": {"Police": "100", "Fire": "101", "Ambulance": "102"},
    "EU": {"All emergencies": "112"},
}

# What each disaster needs from a "safe place".
# (label, Overpass tag filter)
PLACE_TYPES: dict[str, tuple[str, str]] = {
    "hospital": ("Hospital", '["amenity"="hospital"]'),
    "clinic": ("Clinic", '["amenity"~"clinic|doctors"]'),
    "shelter": ("Shelter", '["amenity"="shelter"]'),
    "assembly_point": ("Assembly point", '["emergency"="assembly_point"]'),
    "police": ("Police station", '["amenity"="police"]'),
    "fire_station": ("Fire station", '["amenity"="fire_station"]'),
    "community_centre": ("Community centre", '["amenity"="community_centre"]'),
    "school": ("School (often used as relief camp)", '["amenity"~"school|college|university"]'),
    "open_ground": ("Open ground / park", '["leisure"~"park|pitch|stadium"]'),
    "place_of_worship": ("Place of worship", '["amenity"="place_of_worship"]'),
}

DISASTER_PROFILES: dict[str, dict[str, Any]] = {
    "flood": {
        "types": ["assembly_point", "shelter", "hospital", "school", "community_centre", "police"],
        "prefer_high_ground": True,
        "tips": [
            "Move to higher ground; do not walk or drive through moving water.",
            "Take only phone, power bank, medicines and documents.",
            "Turn off electricity at the main switch only if it is dry and safe to reach.",
        ],
    },
    "cyclone": {
        "types": ["shelter", "assembly_point", "hospital", "school", "community_centre"],
        "prefer_high_ground": True,
        "tips": [
            "Go to a strong, solid building; stay away from windows.",
            "Do not go outside during the calm eye of the storm.",
        ],
    },
    "glacial_lake_outburst": {
        "types": ["assembly_point", "shelter", "hospital", "school"],
        "prefer_high_ground": True,
        "tips": [
            "Move away from the river valley and up the slope immediately.",
            "Do not wait to see the water. Move first.",
        ],
    },
    "earthquake": {
        "types": ["open_ground", "assembly_point", "hospital", "school", "shelter"],
        "prefer_high_ground": False,
        "tips": [
            "Drop, cover and hold on while shaking. After it stops, go to open ground.",
            "Stay away from buildings, bridges, power lines and glass. Expect aftershocks.",
            "Do not use lifts. Do not re-enter damaged buildings.",
        ],
    },
    "fire": {
        "types": ["open_ground", "assembly_point", "fire_station", "hospital", "police"],
        "prefer_high_ground": False,
        "tips": [
            "Get out first, call for help from outside. Do not stop for belongings.",
            "Stay low under smoke. Close doors behind you.",
        ],
    },
    "drought": {
        "types": ["shelter", "community_centre", "hospital", "clinic"],
        "prefer_high_ground": False,
        "tips": [
            "Look for water, shade and medical help. Watch for dehydration signs.",
            "Contact the local administration for relief camps or water supply.",
        ],
    },
    "general": {
        "types": ["assembly_point", "shelter", "hospital", "police", "fire_station",
                  "school", "community_centre"],
        "prefer_high_ground": False,
        "tips": ["Move away from the danger to a public, well-lit place with other people."],
    },
}


# ----------------------------------------------------------------- helpers ---
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _compass(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(x, y)) + 360) % 360
    return ["north", "north-east", "east", "south-east",
            "south", "south-west", "west", "north-west"][int((brg + 22.5) // 45) % 8]


def _human_distance(m: float) -> str:
    return f"{int(round(m, -1))} m" if m < 1000 else f"{m / 1000:.1f} km"


def _walk_minutes(m: float) -> int:
    # ~4.5 km/h, with a 1.3x factor because roads are not straight lines
    return max(1, round(m * 1.3 / 75))


def _valid_coords(lat: float, lon: float) -> str | None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "Latitude must be -90..90 and longitude -180..180."
    return None


def _guess_country(lat: float, lon: float) -> str:
    # Rough bounding box only, used to pick emergency numbers by default.
    if 6 <= lat <= 37 and 68 <= lon <= 98:
        return "IN"
    return "EU"


async def _get_json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def _overpass(client: httpx.AsyncClient, query: str) -> list[dict]:
    last: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            r = await client.post(url, data={"data": query})
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as exc:  # try next mirror
            last = exc
    raise RuntimeError(f"Overpass unavailable: {last}")


async def _elevations(client: httpx.AsyncClient, points: list[tuple[float, float]]) -> list[float | None]:
    if not points:
        return []
    try:
        data = await _get_json(
            client, "https://api.open-meteo.com/v1/elevation",
            latitude=",".join(f"{p[0]:.5f}" for p in points[:100]),
            longitude=",".join(f"{p[1]:.5f}" for p in points[:100]),
        )
        return list(data.get("elevation", []))
    except Exception:
        return [None] * len(points)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})


# ------------------------------------------------------------------- tools ---
@mcp.tool()
async def geocode_place(query: str) -> dict:
    """Turn a place name or address into latitude/longitude.

    Use when the person cannot share GPS but can say where they are,
    e.g. "Connaught Place, Delhi".
    """
    async with _client() as client:
        try:
            data = await _get_json(
                client, "https://nominatim.openstreetmap.org/search",
                q=query, format="jsonv2", limit=3,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Geocoding failed: {exc}"}
    if not data:
        return {"ok": False, "error": "No match. Try adding the city or landmark."}
    return {
        "ok": True,
        "results": [
            {"name": d["display_name"], "latitude": float(d["lat"]), "longitude": float(d["lon"])}
            for d in data
        ],
    }


@mcp.tool()
def get_emergency_numbers(country_code: str = "IN") -> dict:
    """Emergency phone numbers for a country (ISO code, e.g. IN, US, GB, AU, NP, EU)."""
    nums = EMERGENCY_NUMBERS.get(country_code.upper())
    if not nums:
        return {"ok": False, "error": f"No numbers stored for '{country_code}'. Try 112 (works in many countries)."}
    return {"ok": True, "country": country_code.upper(), "numbers": nums,
            "tip": "If the call will not connect, send an SMS or ask someone nearby to call."}


@mcp.tool()
async def find_safe_locations(
    latitude: float,
    longitude: float,
    disaster_type: str = "general",
    radius_m: int = 3000,
    limit: int = 5,
) -> dict:
    """Find the nearest safe places (shelters, assembly points, hospitals, open ground...)
    for a disaster, ranked by distance, with direction and walking time.

    disaster_type: flood | cyclone | earthquake | fire | glacial_lake_outburst | drought | general.
    For flood-type disasters, places on higher ground than the person are ranked first.
    radius_m is capped at 10000. If nothing is found, retry with a larger radius.
    """
    err = _valid_coords(latitude, longitude)
    if err:
        return {"ok": False, "error": err}
    profile = DISASTER_PROFILES.get(disaster_type.lower(), DISASTER_PROFILES["general"])
    radius_m = max(200, min(int(radius_m), 10000))
    limit = max(1, min(int(limit), 10))

    parts = []
    for t in profile["types"]:
        _, flt = PLACE_TYPES[t]
        parts.append(f"nwr(around:{radius_m},{latitude},{longitude}){flt};")
    query = f"[out:json][timeout:20];({''.join(parts)});out center tags 80;"

    async with _client() as client:
        try:
            elements = await _overpass(client, query)
        except Exception as exc:
            return {"ok": False, "error": str(exc),
                    "fallback": "Call 112 and describe your surroundings. Move away from the hazard to a crowded, open area."}

        candidates: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for el in elements:
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            tags = el.get("tags", {})
            ptype = next((t for t in profile["types"] if _matches(tags, t)), None)
            if ptype is None:
                continue
            key = (tags.get("name", ""), int(lat * 1e4) * 100000 + int(lon * 1e4))
            if key in seen:
                continue
            seen.add(key)
            dist = _haversine_m(latitude, longitude, lat, lon)
            candidates.append({
                "name": tags.get("name") or PLACE_TYPES[ptype][0],
                "type": PLACE_TYPES[ptype][0],
                "latitude": lat, "longitude": lon,
                "distance_m": round(dist),
                "distance": _human_distance(dist),
                "direction": _compass(latitude, longitude, lat, lon),
                "walk_minutes": _walk_minutes(dist),
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "_rank": profile["types"].index(ptype),
            })

        if not candidates:
            return {"ok": True, "count": 0, "places": [], "disclaimer": DISCLAIMER,
                    "message": f"Nothing mapped within {radius_m} m. Retry with a larger radius_m or call 112."}

        candidates.sort(key=lambda c: c["distance_m"])
        pool = candidates[:25]

        # Elevation lookup for the pool (+ the person's own position).
        elev = await _elevations(client, [(latitude, longitude)] + [(c["latitude"], c["longitude"]) for c in pool])
        my_elev, place_elev = elev[0], elev[1:]
        for c, e in zip(pool, place_elev):
            c["elevation_m"] = e
            c["higher_than_you_m"] = round(e - my_elev, 1) if (e is not None and my_elev is not None) else None

    if profile["prefer_high_ground"] and my_elev is not None:
        higher = [c for c in pool if (c["higher_than_you_m"] or -1) >= 3]
        pool = higher or pool  # fall back to all if nothing is higher
        pool.sort(key=lambda c: (c["distance_m"], c["_rank"]))
    else:
        pool.sort(key=lambda c: (c["distance_m"], c["_rank"]))

    top = pool[:limit]
    for c in top:
        c.pop("_rank", None)
        c["maps_link"] = f"https://www.openstreetmap.org/?mlat={c['latitude']}&mlon={c['longitude']}#map=18/{c['latitude']}/{c['longitude']}"

    return {
        "ok": True,
        "disaster_type": disaster_type.lower(),
        "your_elevation_m": my_elev,
        "high_ground_preferred": profile["prefer_high_ground"],
        "count": len(top),
        "places": top,
        "disclaimer": DISCLAIMER,
    }


def _matches(tags: dict, ptype: str) -> bool:
    """Re-check an Overpass element against a place type (Overpass returns a union)."""
    a, e, l = tags.get("amenity"), tags.get("emergency"), tags.get("leisure")
    return {
        "hospital": a == "hospital",
        "clinic": a in ("clinic", "doctors"),
        "shelter": a == "shelter",
        "assembly_point": e == "assembly_point",
        "police": a == "police",
        "fire_station": a == "fire_station",
        "community_centre": a == "community_centre",
        "school": a in ("school", "college", "university"),
        "open_ground": l in ("park", "pitch", "stadium"),
        "place_of_worship": a == "place_of_worship",
    }[ptype]


@mcp.tool()
async def check_nearby_hazards(latitude: float, longitude: float, radius_km: int = 300) -> dict:
    """Check live hazard signals near a point: earthquakes (USGS, last 24 h)
    and rain / wind over the next 6 hours (Open-Meteo).

    This is context to support a decision, not an official warning.
    """
    err = _valid_coords(latitude, longitude)
    if err:
        return {"ok": False, "error": err}
    radius_km = max(10, min(int(radius_km), 1000))
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

    async with _client() as client:
        quakes_r, weather_r = await asyncio.gather(
            _get_json(client, "https://earthquake.usgs.gov/fdsnws/event/1/query",
                      format="geojson", latitude=latitude, longitude=longitude,
                      maxradiuskm=radius_km, starttime=since, minmagnitude=2.5,
                      orderby="time", limit=5),
            _get_json(client, "https://api.open-meteo.com/v1/forecast",
                      latitude=latitude, longitude=longitude,
                      current="temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m",
                      hourly="precipitation", forecast_hours=6, timezone="auto"),
            return_exceptions=True,
        )

    out: dict[str, Any] = {"ok": True, "disclaimer": DISCLAIMER}

    if isinstance(quakes_r, Exception):
        out["earthquakes"] = {"available": False}
    else:
        events = []
        for f in quakes_r.get("features", []):
            p, g = f["properties"], f["geometry"]["coordinates"]
            events.append({
                "magnitude": p.get("mag"), "place": p.get("place"),
                "time_utc": datetime.fromtimestamp(p["time"] / 1000, timezone.utc).isoformat(timespec="minutes"),
                "distance_km": round(_haversine_m(latitude, longitude, g[1], g[0]) / 1000),
            })
        out["earthquakes"] = {"available": True, "last_24h": events,
                              "note": "Aftershocks are likely after a large quake." if events else "None reported nearby."}

    if isinstance(weather_r, Exception):
        out["weather"] = {"available": False}
    else:
        cur = weather_r.get("current", {})
        rain_next6 = round(sum(weather_r.get("hourly", {}).get("precipitation", []) or [0]), 1)
        gust = cur.get("wind_gusts_10m") or 0
        flags = []
        if rain_next6 >= 30:
            flags.append("Heavy rain expected: flooding risk, avoid low ground.")
        if gust >= 60:
            flags.append("Strong gusts: stay away from trees, hoardings and windows.")
        out["weather"] = {
            "available": True,
            "temperature_c": cur.get("temperature_2m"),
            "wind_gust_kmh": gust,
            "rain_next_6h_mm": rain_next6,
            "flags": flags,
        }
    return out


@mcp.tool()
async def calm_safety_plan(
    latitude: float,
    longitude: float,
    disaster_type: str = "general",
    country_code: str | None = None,
) -> dict:
    """One-call, calm decision aid for someone in a disaster.

    Returns: a short breathing cue, the single best place to go with direction and walking
    time, one backup, what to do on the way, hazards nearby, and who to call.
    Present `steps` to the person one at a time, in order, in a calm tone.
    """
    err = _valid_coords(latitude, longitude)
    if err:
        return {"ok": False, "error": err}
    dtype = disaster_type.lower() if disaster_type.lower() in DISASTER_PROFILES else "general"
    profile = DISASTER_PROFILES[dtype]
    cc = (country_code or _guess_country(latitude, longitude)).upper()
    numbers = EMERGENCY_NUMBERS.get(cc, EMERGENCY_NUMBERS["EU"])

    places, hazards = await asyncio.gather(
        find_safe_locations(latitude, longitude, dtype, 3000, 3),
        check_nearby_hazards(latitude, longitude),
    )
    if not places.get("places") and places.get("ok"):  # widen once
        places = await find_safe_locations(latitude, longitude, dtype, 8000, 3)

    steps = [
        "Pause. Take one slow breath in for 4 seconds, out for 6. You have a plan below.",
        f"If anyone is in immediate danger, call {numbers.get('All emergencies', '112')} now.",
    ]
    options = places.get("places") or []
    if options:
        best = options[0]
        high = best.get("higher_than_you_m")
        extra = f", about {high:g} m higher than you" if high and high > 0 else ""
        steps.append(
            f"Go to {best['name']} ({best['type']}): {best['distance']} to the {best['direction']}, "
            f"roughly {best['walk_minutes']} min on foot{extra}."
        )
        if len(options) > 1:
            b = options[1]
            steps.append(f"Backup if that route is blocked: {b['name']}, {b['distance']} to the {b['direction']}.")
    else:
        steps.append("No mapped safe place found nearby. Move away from the hazard to open, public ground "
                     "and call for help.")
    steps.extend(profile["tips"])
    flags = (hazards.get("weather") or {}).get("flags", [])
    steps.extend(flags)
    steps.append("Tell someone where you are going. Keep your phone charged and stay with other people.")

    return {
        "ok": True,
        "disaster_type": dtype,
        "steps": steps,
        "best_place": options[0] if options else None,
        "backup_places": options[1:],
        "hazards": hazards,
        "emergency_numbers": numbers,
        "disclaimer": DISCLAIMER,
    }


if __name__ == "__main__":
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))