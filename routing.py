"""Calculate shortest travel-time routes on open roads."""

import heapq
from collections.abc import Iterable
from dataclasses import dataclass

from models import Road, natural_key


@dataclass(frozen=True)
class RouteTree:
    """Shortest travel times from every reachable place to one root place."""

    root: str
    minutes: dict[str, int]
    next_hop: dict[str, str | None]

    def time_from(self, node: str) -> int | None:
        """Minutes from `node` to the root, or None if it is unreachable."""
        return self.minutes.get(node)

    def path_from(self, node: str) -> list[str] | None:
        """Reconstruct the path node -> ..."""
        if node not in self.minutes:
            return None
        path = [node]
        while path[-1] != self.root:
            path.append(self.next_hop[path[-1]])
        return path


def open_neighbours(nodes: Iterable[str], roads: Iterable[Road]) -> dict[str, list[tuple[str, int]]]:
    """Adjacency list of OPEN roads: place -> [(neighbour, minutes), ...]."""
    adjacency: dict[str, list[tuple[str, int]]] = {node: [] for node in nodes}
    for road in roads:
        if not road.open:
            continue
        if road.a not in adjacency or road.b not in adjacency:
            raise ValueError(f"Road {road.id} uses an unknown place.")
        adjacency[road.a].append((road.b, road.minutes))
        adjacency[road.b].append((road.a, road.minutes))
    for neighbours in adjacency.values():
        neighbours.sort(key=lambda pair: natural_key(pair[0]))
    return adjacency


def dijkstra(nodes: Iterable[str], roads: Iterable[Road], root: str) -> RouteTree:
    """Dijkstra's algorithm, rooted at `root`."""
    adjacency = open_neighbours(nodes, roads)
    if root not in adjacency:
        raise ValueError(f"Unknown place: {root}")

    minutes: dict[str, int] = {root: 0}
    next_hop: dict[str, str | None] = {root: None}
    frontier = [(0, natural_key(root), root)]
    settled: set[str] = set()

    while frontier:
        time_so_far, _, node = heapq.heappop(frontier)
        if node in settled:
            continue
        settled.add(node)
        for neighbour, cost in adjacency[node]:
            candidate = time_so_far + cost
            if neighbour not in minutes or candidate < minutes[neighbour]:
                minutes[neighbour] = candidate
                next_hop[neighbour] = node
                heapq.heappush(frontier, (candidate, natural_key(neighbour), neighbour))

    return RouteTree(root, minutes, next_hop)


def find_road(roads: Iterable[Road], first: str, second: str, include_closed: bool = False) -> Road | None:
    """The road joining two places, or None."""
    for road in roads:
        if road.joins(first, second) and (road.open or include_closed):
            return road
    return None


def path_minutes(roads: Iterable[Road], path: list[str] | tuple[str, ...]) -> int | None:
    """Total minutes along a path, or None if any step lacks an open road."""
    road_list = list(roads)
    total = 0
    for here, there in zip(path, path[1:]):
        road = find_road(road_list, here, there)
        if road is None:
            return None
        total += road.minutes
    return total
