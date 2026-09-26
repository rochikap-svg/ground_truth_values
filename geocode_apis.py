"""
Country-aware geocoding across several free, no-API-key services.

Design: pick the best free API for the record's country first, then fall
back to universal services if that call fails or returns nothing. Every
result is cached to disk (by raw address + country) so re-runs and repeated
addresses across S1/S2/S3 never re-hit the network.

    France  -> api-adresse.data.gouv.fr   (official govt API, best for FR)
    US      -> Census Bureau geocoder     (official govt API, best for US)
    other   -> Photon (OSM)               -> Nominatim (OSM) as last resort
"""

import json
import math
import os
import time
import requests

from . import config


class GeocodeCache:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                try:
                    self._data = json.load(f)
                except json.JSONDecodeError:
                    self._data = {}
        else:
            self._data = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f)


_last_nominatim_call = [0.0]


def _throttle_nominatim():
    elapsed = time.time() - _last_nominatim_call[0]
    if elapsed < config.NOMINATIM_MIN_INTERVAL_SECS:
        time.sleep(config.NOMINATIM_MIN_INTERVAL_SECS - elapsed)
    _last_nominatim_call[0] = time.time()


def _get_json(url, params, headers=None):
    for attempt in range(config.GEOCODE_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, headers=headers, timeout=config.GEOCODE_TIMEOUT_SECS
            )
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException:
            pass
        time.sleep(0.5 * (attempt + 1))
    return None


def _geocode_france(address):
    data = _get_json(config.FRANCE_API_URL, {"q": address, "limit": 1})
    if not data or not data.get("features"):
        return None
    coords = data["features"][0]["geometry"]["coordinates"]  # [lon, lat]
    return {"lat": coords[1], "lon": coords[0]}


def _geocode_us(address):
    data = _get_json(
        config.US_CENSUS_API_URL,
        {"address": address, "benchmark": "Public_AR_Current", "format": "json"},
    )
    if not data:
        return None
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    coords = matches[0]["coordinates"]  # {'x': lon, 'y': lat}
    return {"lat": coords["y"], "lon": coords["x"]}


def _geocode_photon(address):
    data = _get_json(config.PHOTON_API_URL, {"q": address, "limit": 1})
    if not data or not data.get("features"):
        return None
    coords = data["features"][0]["geometry"]["coordinates"]  # [lon, lat]
    return {"lat": coords[1], "lon": coords[0]}


def _geocode_nominatim(address):
    _throttle_nominatim()
    data = _get_json(
        config.NOMINATIM_API_URL,
        {"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": config.NOMINATIM_USER_AGENT},
    )
    if not data:
        return None
    return {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"])}


def geocode(address, country, cache: GeocodeCache, enabled: bool = True):
    """Return {'lat':.., 'lon':..} or None. Cached and country-routed."""
    if not address or not enabled:
        return None

    key = f"{(country or '').strip().lower()}|{address.strip().lower()}"
    cached = cache.get(key)
    if cached is not None:
        return cached if cached != "NONE" else None

    country_l = (country or "").strip().lower()
    result = None

    if country_l in ("france", "fr"):
        result = _geocode_france(address) or _geocode_photon(address) or _geocode_nominatim(address)
    elif country_l in ("us", "usa", "united states", "united states of america"):
        result = _geocode_us(address) or _geocode_photon(address) or _geocode_nominatim(address)
    else:
        # India and everything else: Photon first (fast, no strict limit),
        # Nominatim as a slower but reliable backstop.
        result = _geocode_photon(address) or _geocode_nominatim(address)

    cache.set(key, result if result is not None else "NONE")
    return result


def haversine_km(p1, p2):
    if p1 is None or p2 is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, [p1["lat"], p1["lon"], p2["lat"], p2["lon"]])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    r_km = 6371.0
    return c * r_km
