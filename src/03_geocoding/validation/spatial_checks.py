"""Index GeoJSON polygons and test point membership"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _inside_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[0], point[1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            boundary_lon = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < boundary_lon:
                inside = not inside
        j = i
    return inside


def _inside_polygon(lon: float, lat: float, polygon: list[list[list[float]]]) -> bool:
    if not polygon or not _inside_ring(lon, lat, polygon[0]):
        return False
    return not any(_inside_ring(lon, lat, hole) for hole in polygon[1:])


@dataclass(frozen=True)
class Feature:
    code: str
    polygons: list[list[list[list[float]]]]
    bbox: tuple[float, float, float, float]

    def contains(self, lon: float, lat: float) -> bool:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            return False
        return any(_inside_polygon(lon, lat, polygon) for polygon in self.polygons)


# Index regional polygons for repeated coordinate-agreement checks
class TL3Index:
    def __init__(self, features: list[Feature]) -> None:
        self.features = features
        self.codes = frozenset(feature.code for feature in features)

    @classmethod
    def from_geojson(cls, path: Path, code_property: str) -> TL3Index:
        payload = json.loads(path.read_text(encoding="utf-8"))
        declared_crs = payload.get("crs")
        if declared_crs:
            name = str((declared_crs.get("properties") or {}).get("name", "")).upper()
            if name not in {"EPSG:4326", "URN:OGC:DEF:CRS:EPSG::4326", "OGC:CRS84",
                            "URN:OGC:DEF:CRS:OGC:1.3:CRS84"}:
                raise ValueError("Boundary GeoJSON must use WGS84 longitude/latitude")
        features: list[Feature] = []
        for raw in payload.get("features", []):
            properties: dict[str, Any] = raw.get("properties") or {}
            code = str(properties.get(code_property, "")).strip()
            geometry = raw.get("geometry") or {}
            kind = geometry.get("type")
            coordinates = geometry.get("coordinates") or []
            if not code or kind not in {"Polygon", "MultiPolygon"}:
                continue
            polygons = [coordinates] if kind == "Polygon" else coordinates
            points = [
                point
                for polygon in polygons
                for ring in polygon
                for point in ring
                if len(point) >= 2
            ]
            if not points:
                continue
            lons = [float(point[0]) for point in points]
            lats = [float(point[1]) for point in points]
            if any(not math.isfinite(x) or not -180 <= x <= 180 for x in lons) or any(
                    not math.isfinite(y) or not -90 <= y <= 90 for y in lats):
                raise ValueError("Boundary coordinates are outside WGS84 longitude/latitude bounds")
            features.append(
                Feature(
                    code=code,
                    polygons=polygons,
                    bbox=(min(lons), min(lats), max(lons), max(lats)),
                )
            )
        if not features:
            raise ValueError(f"No polygon features contain property {code_property!r}")
        return cls(features)

    def locate(self, lon: float, lat: float) -> list[str]:
        return sorted({feature.code for feature in self.features if feature.contains(lon, lat)})
