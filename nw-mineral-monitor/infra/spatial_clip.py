"""Small stdlib spatial index for repeat point-in-state tests."""
from __future__ import annotations

import math


def _point_in_ring(x, y, ring):
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][:2]
        x2, y2 = ring[i + 1][:2]
        if ((y1 > y) != (y2 > y) and
                x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside


class StateClipIndex:
    """Memoize 0.05-degree cells away from polygon boundaries.

    Cells touched by any boundary segment always receive an exact test. Other
    cells are exact-tested once at their first point and safely reused because
    no polygon boundary crosses the cell.
    """
    def __init__(self, geometry, cell=0.05):
        self.cell = cell
        self.polygons = (geometry['coordinates'] if geometry['type'] == 'MultiPolygon'
                         else [geometry['coordinates']])
        self.boundary = set()
        self.memo = {}
        for polygon in self.polygons:
            for ring in polygon:
                for i in range(len(ring) - 1):
                    x1, y1 = ring[i][:2]
                    x2, y2 = ring[i + 1][:2]
                    gx0, gx1 = sorted((math.floor(x1 / cell), math.floor(x2 / cell)))
                    gy0, gy1 = sorted((math.floor(y1 / cell), math.floor(y2 / cell)))
                    for gx in range(gx0, gx1 + 1):
                        for gy in range(gy0, gy1 + 1):
                            self.boundary.add((gx, gy))

    def _exact(self, x, y):
        return any(sum(_point_in_ring(x, y, ring) for ring in polygon) % 2 == 1
                   for polygon in self.polygons)

    def contains(self, x, y):
        key = (math.floor(x / self.cell), math.floor(y / self.cell))
        if key in self.boundary:
            return self._exact(x, y)
        if key not in self.memo:
            self.memo[key] = self._exact(x, y)
        return self.memo[key]
