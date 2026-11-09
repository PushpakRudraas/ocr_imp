"""Geo resolution from entities + dateline: primary location, admin level, approximate coordinates, disambiguation of
names shared with another country (Punjab), and H3 cell if the optional `h3` package is installed."""
from __future__ import annotations

from . import lexicons as L
from .entities import resolve_country

try:                                  # optional
    import h3 as _h3
except Exception:                     # pragma: no cover
    _h3 = None


def _h3_cell(lat: float, lon: float, res: int = 5) -> str | None:
    if _h3 is None:
        return None
    fn = getattr(_h3, "latlng_to_cell", None) or getattr(_h3, "geo_to_h3", None)
    return fn(lat, lon, res) if fn else None


def _lookup(name: str) -> dict | None:
    low = name.lower()
    if low in L.INDIA_STATES and low not in ("chandigarh", "delhi"):
        lat, lon = L.INDIA_STATES[low]
        return {"name": name.title() if low != "jammu and kashmir" else "Jammu and Kashmir", "level": "state",
                "lat": lat, "lon": lon, "iso": "IN", "state": low}
    if low in L.INDIA_CITIES:
        lat, lon, state = L.INDIA_CITIES[low]
        return {"name": name.title(), "level": "city", "lat": lat, "lon": lon, "iso": "IN", "state": state}
    iso = resolve_country(name)
    if iso:
        lat, lon = L.COUNTRY_CENTROIDS.get(iso, (None, None))
        return {"name": name, "level": "country", "lat": lat, "lon": lon, "iso": iso, "state": None}
    return None


def resolve_geo(headline: str, text: str, entities: list[dict], dateline_city: str | None,
                institution: str | None, default_country: str = "IN") -> dict:
    scores: dict[str, float] = {}
    for e in entities:
        if e["type"] == "GPE":
            scores[e["text"]] = scores.get(e["text"], 0) + e.get("mentions", 1)
    if dateline_city:
        scores[dateline_city] = scores.get(dateline_city, 0) + 1
    if institution:
        low = institution.lower()
        for name in list(L.INDIA_STATES) + list(scores):
            if low.startswith(name.lower() + " "):
                key = next((k for k in scores if k.lower() == name.lower()), name.title())
                scores[key] = scores.get(key, 0) + 4   # "Punjab Assembly" is a Punjab story, wherever the reporter sat

    empty = {"primary_location": None, "country_iso": default_country, "admin_level": None, "lat": None, "lon": None,
             "coordinates_note": "no place name found in the article", "disambiguation": None, "h3_res5": None}
    if not scores:
        return empty

    ranked = sorted(scores, key=lambda n: -scores[n])
    info = None
    for name in ranked:
        info = _lookup(name)
        if info:
            break
    if not info:
        return {**empty, "primary_location": ranked[0], "coordinates_note": "place not in the gazetteer"}

    # a city whose state is also in play (or implied by the institution) is reported at state level
    if info["level"] == "city" and info["state"]:
        st = info["state"]
        state_in_play = any(n.lower() == st for n in scores) or (institution and institution.lower().startswith(st))
        if state_in_play and st not in ("chandigarh", "delhi"):
            info = _lookup(st) or info

    low = info["name"].lower()
    disamb = None
    if low in L.AMBIGUOUS_PLACES:
        other, cues = L.AMBIGUOUS_PLACES[low]
        doc = f"{headline} {text}".lower()
        hit = [c for c in sorted(cues) if c in doc][:3]
        disamb = (f"India, not {other}, because the text mentions {', '.join(hit)}" if hit
                  else f"ambiguous with {other}; India assumed from the publication's market")
    cell = _h3_cell(info["lat"], info["lon"]) if info["lat"] is not None else None
    note = {"state": "approximate state centroid, not a precise event location",
            "city": "approximate city centroid", "country": "approximate country centroid"}[info["level"]]
    return {"primary_location": info["name"], "country_iso": info["iso"], "admin_level": info["level"],
            "lat": info["lat"], "lon": info["lon"], "coordinates_note": note, "disambiguation": disamb,
            "h3_res5": cell}


def geo_scope(geo: dict, text: str) -> str:
    level = geo.get("admin_level")
    if level == "city":
        return "local"
    if level == "state":
        return "state"
    if level == "country":
        return "national" if geo.get("country_iso") == "IN" else "international"
    return "national" if any(w in text.lower() for w in ("centre", "union government", "parliament", "supreme court")) else "unknown"
