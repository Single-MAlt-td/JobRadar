from __future__ import annotations

from dataclasses import dataclass
import math
import os
import threading
import time
from typing import Optional
from urllib.parse import quote

import requests


@dataclass
class GeocodeResult:
    address_text: str
    lat: float
    lon: float
    display_name: str = ""


@dataclass
class RouteResult:
    destination_text: str
    distance_km: float
    duration_min: float
    start: GeocodeResult
    destination: GeocodeResult
    provider: str = "direct"
    quality: str = "estimated"  # estimated | exact


class RateLimiter:
    def __init__(self, min_delay_seconds: float = 1.0) -> None:
        self.min_delay_seconds = max(0.0, float(min_delay_seconds))
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_time = self.min_delay_seconds - (now - self._last_request_at)
            if wait_time > 0:
                time.sleep(wait_time)
            self._last_request_at = time.monotonic()


class BaseRouter:
    provider_name = "base"
    quality = "exact"

    def __init__(self, request_delay_seconds: float = 1.0, geocoding_provider: str = "nominatim", ors_api_key: str = "") -> None:
        self.rate_limiter = RateLimiter(request_delay_seconds)
        self.geocoding_provider = (geocoding_provider or "nominatim").strip().lower()
        self.ors_api_key = ors_api_key.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "JobRadar local personal job-search tool (contact: local-user)"}
        )

    def geocode(self, address_text: str) -> GeocodeResult:
        query = address_text.strip()
        if not query:
            raise ValueError("Empty address")

        # Internal shortcut used by JobRadar for exact BA coordinates.
        # Format: coords:<lat>,<lon>
        if query.lower().startswith("coords:"):
            coord_text = query.split(":", 1)[1].strip()
            lat_text, lon_text = [part.strip() for part in coord_text.split(",", 1)]
            lat = float(lat_text)
            lon = float(lon_text)
            return GeocodeResult(
                address_text=query,
                lat=lat,
                lon=lon,
                display_name=f"{lat:.6f}, {lon:.6f}",
            )

        queries = self._build_geocoding_queries(query)
        if self.geocoding_provider in {"ors", "openrouteservice", "open_route_service"}:
            return self._geocode_ors(address_text.strip(), queries)
        return self._geocode_nominatim(address_text.strip(), queries)

    def _geocode_nominatim(self, original_text: str, queries: list[str]) -> GeocodeResult:
        last_error = None
        for geocode_query in queries:
            self.rate_limiter.wait()
            response = self.session.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": geocode_query,
                    "format": "jsonv2",
                    "limit": 1,
                    "addressdetails": 0,
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            if data:
                first = data[0]
                return GeocodeResult(
                    address_text=original_text,
                    lat=float(first["lat"]),
                    lon=float(first["lon"]),
                    display_name=str(first.get("display_name", "")),
                )
            last_error = f"Address not found: {geocode_query}"
        raise ValueError(last_error or f"Address not found: {original_text}")

    def _geocode_ors(self, original_text: str, queries: list[str]) -> GeocodeResult:
        if not self.ors_api_key:
            raise ValueError("OpenRouteService API key is missing for geocoding")
        last_error = None
        for geocode_query in queries:
            self.rate_limiter.wait()
            response = self.session.get(
                "https://api.heigit.org/openrouteservice/geocode/search",
                params={
                    "api_key": self.ors_api_key,
                    "text": geocode_query,
                    "size": 1,
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            features = data.get("features") or []
            if features:
                first = features[0]
                coords = first.get("geometry", {}).get("coordinates", [])
                props = first.get("properties", {})
                if len(coords) >= 2:
                    return GeocodeResult(
                        address_text=original_text,
                        lat=float(coords[1]),
                        lon=float(coords[0]),
                        display_name=str(props.get("label") or props.get("name") or ""),
                    )
            last_error = f"Address not found: {geocode_query}"
        raise ValueError(last_error or f"Address not found: {original_text}")

    @staticmethod
    def _build_geocoding_queries(address_text: str) -> list[str]:
        query = address_text.strip()
        if not query:
            return []

        # JobRadar now supplies the country explicitly whenever it is known.
        # Never force Germany when the query already ends with a country-like
        # component. Germany remains a conservative fallback for legacy rows.
        parts = [part.strip() for part in query.split(",") if part.strip()]
        lower = query.casefold()
        known_country_tokens = {
            "deutschland", "germany", "spanien", "spain", "österreich", "austria",
            "schweiz", "switzerland", "frankreich", "france", "italien", "italy",
            "niederlande", "netherlands", "belgien", "belgium", "portugal",
            "polen", "poland", "tschechien", "czechia", "dänemark", "denmark",
            "schweden", "sweden", "norwegen", "norway", "finnland", "finland",
            "vereinigtes königreich", "united kingdom", "uk", "irland", "ireland",
            "usa", "united states", "kanada", "canada"
        }
        has_explicit_country = any(token in lower for token in known_country_tokens)
        if len(parts) >= 2 and parts[-1].casefold() in known_country_tokens:
            has_explicit_country = True

        queries: list[str] = []
        if len(parts) >= 2 and parts[0].replace(" ", "").isalnum() and any(ch.isdigit() for ch in parts[0]):
            postal_code = parts[0]
            city = parts[1].split("/", 1)[0].strip()
            suffix = f", {parts[-1]}" if has_explicit_country and len(parts) >= 3 else ""
            queries.append(f"{postal_code} {city}{suffix}")
            queries.append(f"{city}{suffix}")

        queries.append(query)
        if not has_explicit_country:
            queries.append(f"{query}, Germany")

        result: list[str] = []
        seen: set[str] = set()
        for item in queries:
            key = item.casefold()
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    def calculate_route(self, home_address: str, destination_address: str) -> RouteResult:
        start = self.geocode(home_address)
        destination = self.geocode(destination_address)
        return self.route(start, destination)

    def route(self, start: GeocodeResult, destination: GeocodeResult) -> RouteResult:
        raise NotImplementedError


class DirectEstimateRouter(BaseRouter):
    provider_name = "direct"
    quality = "estimated"

    def __init__(self, request_delay_seconds: float = 1.0, road_factor: float = 1.25, average_speed_kmh: float = 75.0, geocoding_provider: str = "nominatim", ors_api_key: str = "") -> None:
        super().__init__(request_delay_seconds=request_delay_seconds, geocoding_provider=geocoding_provider, ors_api_key=ors_api_key)
        self.road_factor = float(road_factor)
        self.average_speed_kmh = float(average_speed_kmh)

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius_km = 6371.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def route(self, start: GeocodeResult, destination: GeocodeResult) -> RouteResult:
        air_km = self._haversine_km(start.lat, start.lon, destination.lat, destination.lon)
        distance_km = round(air_km * self.road_factor, 1)
        duration_min = round((distance_km / max(1.0, self.average_speed_kmh)) * 60.0, 1)
        return RouteResult(
            destination_text=destination.address_text,
            distance_km=distance_km,
            duration_min=duration_min,
            start=start,
            destination=destination,
            provider=self.provider_name,
            quality=self.quality,
        )


class OSRMDemoRouter(BaseRouter):
    provider_name = "osrm_demo"
    quality = "exact"

    def route(self, start: GeocodeResult, destination: GeocodeResult) -> RouteResult:
        coords = f"{start.lon},{start.lat};{destination.lon},{destination.lat}"
        self.rate_limiter.wait()
        response = self.session.get(
            f"https://router.project-osrm.org/route/v1/driving/{quote(coords, safe=';,')}",
            params={"overview": "false", "alternatives": "false", "steps": "false"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        routes = data.get("routes") or []
        if not routes:
            raise ValueError(f"No route found: {destination.address_text}")
        route = routes[0]
        return RouteResult(
            destination_text=destination.address_text,
            distance_km=round(float(route.get("distance", 0.0)) / 1000.0, 1),
            duration_min=round(float(route.get("duration", 0.0)) / 60.0, 1),
            start=start,
            destination=destination,
            provider=self.provider_name,
            quality=self.quality,
        )


class OpenRouteServiceRouter(BaseRouter):
    provider_name = "ors"
    quality = "exact"

    def __init__(self, request_delay_seconds: float = 1.0, api_key: str = "", geocoding_provider: str = "nominatim") -> None:
        super().__init__(request_delay_seconds=request_delay_seconds, geocoding_provider=geocoding_provider, ors_api_key=api_key)
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("OpenRouteService API key is missing")

    def route(self, start: GeocodeResult, destination: GeocodeResult) -> RouteResult:
        self.rate_limiter.wait()
        response = self.session.post(
            "https://api.heigit.org/openrouteservice/v2/directions/driving-car",
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            json={"coordinates": [[start.lon, start.lat], [destination.lon, destination.lat]]},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        routes = data.get("routes") or []
        if not routes:
            raise ValueError(f"No ORS route found: {destination.address_text}")
        summary = routes[0].get("summary") or {}
        return RouteResult(
            destination_text=destination.address_text,
            distance_km=round(float(summary.get("distance", 0.0)) / 1000.0, 1),
            duration_min=round(float(summary.get("duration", 0.0)) / 60.0, 1),
            start=start,
            destination=destination,
            provider=self.provider_name,
            quality=self.quality,
        )


def create_router(
    provider: str,
    request_delay_seconds: float = 1.0,
    ors_env_var: str = "ORS_API_KEY",
    geocoding_provider: str = "nominatim",
) -> BaseRouter:
    provider_key = (provider or "direct").strip().lower()
    env_name = (ors_env_var or "ORS_API_KEY").strip() or "ORS_API_KEY"
    api_key = os.getenv(env_name, "")
    if provider_key in {"direct", "direct_estimate", "estimate"}:
        return DirectEstimateRouter(
            request_delay_seconds=request_delay_seconds,
            geocoding_provider=geocoding_provider,
            ors_api_key=api_key,
        )
    if provider_key in {"osrm", "osrm_demo"}:
        router = OSRMDemoRouter(request_delay_seconds=request_delay_seconds)
        router.geocoding_provider = (geocoding_provider or "nominatim").strip().lower()
        router.ors_api_key = api_key
        return router
    if provider_key in {"ors", "openrouteservice", "open_route_service"}:
        return OpenRouteServiceRouter(
            request_delay_seconds=request_delay_seconds,
            api_key=api_key,
            geocoding_provider=geocoding_provider,
        )
    raise ValueError(f"Unknown routing provider: {provider}")
