"""Draw the town map, routes and ambulance positions."""

import simulation
from .html_tools import Markup, tag
from intake import AMBULANCE_STATUS_LABELS, PRIORITY_LABELS, label
from models import ASSIGNED, TO_SCENE, WAITING, Ambulance, DispatchPlan, SimulationState

WIDTH, HEIGHT = 960, 560


HARBOUR_SHAPE = "M0,505 C70,492 150,515 205,560 L0,560 Z"
RIVER_PATH = "M455,0 C430,90 470,170 440,240 C410,310 400,380 380,440 C365,490 330,530 300,560"


def _fmt(value: float) -> str:
    """Short coordinate text: 120.0 -> '120', 120.25 -> '120.2'."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _point(state: SimulationState, node: str) -> tuple[float, float]:
    location = state.locations[node]
    return float(location.x), float(location.y)


def vehicle_position(state: SimulationState, ambulance: Ambulance) -> tuple[float, float]:
    """Where to draw an ambulance: interpolated along its road if driving."""
    start = _point(state, ambulance.location)
    if not ambulance.on_road:
        return start
    end = _point(state, ambulance.edge_to)
    fraction = ambulance.edge_elapsed / state.roads[ambulance.edge_road].minutes
    return start[0] + (end[0] - start[0]) * fraction, start[1] + (end[1] - start[1]) * fraction


def _polyline(points, css_class: str) -> Markup:
    text = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)
    return tag("polyline", points=text, class_=css_class)


def _label_position(location, line: int = 0):
    """Text anchor point for a place label (line 1 is a second, smaller line)."""
    x, y, side = location.x, location.y, location.label_side
    if side == "above":
        return x, y - 20 - 15 * line, "middle"
    if side == "left":
        return x - 16, y + 5 + 15 * line, "end"
    if side == "right":
        return x + 16, y + 5 + 15 * line, "start"
    return x, y + 30 + 15 * line, "middle"


def _roads(state: SimulationState) -> list[Markup]:
    lines = []
    for road in state.sorted_roads():
        (ax, ay), (bx, by) = _point(state, road.a), _point(state, road.b)
        css = "road" if road.open else "road road-closed"
        lines.append(tag("line", x1=_fmt(ax), y1=_fmt(ay), x2=_fmt(bx), y2=_fmt(by), class_=css))
    return lines


def _road_costs(state: SimulationState) -> list[Markup]:
    """Minute labels at each road's midpoint (drawn above the route lines)."""
    labels = []
    for road in state.sorted_roads():
        (ax, ay), (bx, by) = _point(state, road.a), _point(state, road.b)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        text = f"{road.minutes}" if road.open else f"✕ {road.minutes}"
        width = 26 if road.open else 40
        labels.append(tag(
            "g",
            tag("title", f"{state.road_label(road)}: {road.minutes} min" + ("" if road.open else " (closed)")),
            tag("rect", x=_fmt(mx - width / 2), y=_fmt(my - 10), width=width, height=20, rx=10, class_="cost-pill"),
            tag("text", text, x=_fmt(mx), y=_fmt(my + 4), class_="cost-text"),
            class_="cost" if road.open else "cost cost-closed",
        ))
    return labels


def _preview(state: SimulationState, plan: DispatchPlan | None) -> list[Markup]:
    if plan is None:
        return []
    scene = [_point(state, node) for node in plan.route_to_scene]
    hospital = [_point(state, node) for node in plan.route_to_hospital]
    parts = []
    if len(scene) > 1:
        parts.append(_polyline(scene, "route route-preview-scene"))
    if len(hospital) > 1:
        parts.append(_polyline(hospital, "route route-preview-hospital"))
    return parts


def _active_routes(state: SimulationState) -> list[Markup]:
    parts = []
    for ambulance in state.sorted_ambulances():
        path = simulation.remaining_route(state, ambulance)
        if path is None:
            continue
        points = [_point(state, node) for node in path]
        if ambulance.on_road:
            points.insert(0, vehicle_position(state, ambulance))
        if ambulance.status == TO_SCENE:
            incident = state.incidents[ambulance.incident_id]
            onward = simulation.route_tree(state, state.hospitals[ambulance.hospital_id].location).path_from(incident.location)
            if onward and len(onward) > 1:
                parts.append(_polyline([_point(state, node) for node in onward], "route route-next"))
            css = "route route-scene"
        else:
            css = "route route-hospital"
        if len(points) > 1:
            parts.append(_polyline(points, css))
    return parts


def _places(state: SimulationState) -> list[Markup]:
    parts = []
    for location in state.locations.values():
        hospital = state.hospital_at(location.id)
        x, y, anchor = _label_position(location)
        if hospital is None:
            marker = tag("circle", cx=location.x, cy=location.y, r=8, class_="place")
            extra = None
        else:
            marker = tag(
                "g",
                tag("rect", x=location.x - 13, y=location.y - 13, width=26, height=26, rx=6, class_="hospital"),
                tag("text", "H", x=location.x, y=location.y + 5, class_="hospital-mark"),
            )
            sx, sy, s_anchor = _label_position(location, 1)
            extra = tag("text", f"{hospital.id} · {hospital.free} free", x=sx, y=sy, text_anchor=s_anchor,
                        class_="place-sub")
        parts.append(tag(
            "g",
            marker,
            tag("text", location.name, x=x, y=y, text_anchor=anchor, class_="place-label"),
            extra,
            class_="place-group",
        ))
    return parts


def _incidents(state: SimulationState) -> list[Markup]:
    """Markers for calls not yet picked up, offset to the upper left of the place."""
    parts, per_place = [], {}
    for incident in sorted(state.incidents.values(), key=lambda i: i.id):
        if incident.pickup_minute is not None or incident.status not in (WAITING, ASSIGNED):
            continue
        slot = per_place.get(incident.location, 0)
        per_place[incident.location] = slot + 1
        place = state.locations[incident.location]
        if place.label_side == "left":
            x, y = place.x + 34 + 28 * slot, place.y + 16
        elif place.label_side == "above":
            x, y = place.x - 34 - 28 * slot, place.y + 16
        else:
            x, y = place.x - 34 - 28 * slot, place.y - 16
        css = f"incident incident-{incident.priority}" + (" incident-assigned" if incident.status != WAITING else "")
        parts.append(tag(
            "g",
            tag("title", f"Incident #{incident.id}: {label(PRIORITY_LABELS, incident.priority)}"),
            tag("circle", cx=x, cy=y, r=12, class_="incident-ring"),
            tag("text", f"#{incident.id}", x=x, y=y + 4, class_="incident-text"),
            class_=css,
        ))
    return parts


def _ambulances(state: SimulationState) -> list[Markup]:
    parts, stacked = [], {}
    for ambulance in state.sorted_ambulances():
        x, y = vehicle_position(state, ambulance)
        spot = (round(x), round(y))
        slot = stacked.get(spot, 0)
        stacked[spot] = slot + 1
        at_place = not ambulance.on_road
        below = at_place and state.locations[ambulance.location].label_side == "above"
        gap = 30 if at_place and state.hospital_at(ambulance.location) else 24
        dy = (gap + 22 * slot) if below else -(gap + 22 * slot)
        status = "blocked" if ambulance.is_blocked else ambulance.status
        status_text = "Blocked" if ambulance.is_blocked else label(AMBULANCE_STATUS_LABELS, ambulance.status)
        parts.append(tag(
            "g",
            tag("title", f"{ambulance.id} {ambulance.name}: {status_text}"),
            tag("line", x1=_fmt(x), y1=_fmt(y), x2=_fmt(x), y2=_fmt(y + dy * 0.55), class_="vehicle-stem"),
            tag("rect", x=_fmt(x - 18), y=_fmt(y + dy - 10), width=36, height=20, rx=5, class_="vehicle-body"),
            tag("text", ambulance.id, x=_fmt(x), y=_fmt(y + dy + 4), class_="vehicle-text"),
            class_=f"vehicle vehicle-{status}",
        ))
    return parts


def describe(state: SimulationState) -> str:
    """A plain-English summary of the map for screen readers."""
    sentences = [f"Harbour District map at minute {state.minute}."]
    closed = [state.road_label(road) for road in state.sorted_roads() if not road.open]
    sentences.append("Closed roads: " + ", ".join(closed) + "." if closed else "All roads are open.")
    for ambulance in state.sorted_ambulances():
        status = "blocked" if ambulance.is_blocked else label(AMBULANCE_STATUS_LABELS, ambulance.status).lower()
        sentences.append(f"{ambulance.id} is {status} near {state.place_name(ambulance.location)}.")
    return " ".join(sentences)


def render_map(state: SimulationState, preview: DispatchPlan | None = None) -> Markup:
    """The whole map as one inline <svg> element."""
    return tag(
        "svg",
        tag("title", "Harbour District road network", id="map-title"),
        tag("desc", describe(state), id="map-desc"),
        tag("rect", x=0, y=0, width=WIDTH, height=HEIGHT, class_="map-ground"),
        tag("path", d=HARBOUR_SHAPE, class_="harbour"),
        tag("path", d=RIVER_PATH, class_="river"),
        tag("text", "Harbour", x=22, y=545, class_="scenery-label"),
        tag("text", "River Wend", x=468, y=28, class_="scenery-label"),
        tag("g", _roads(state), class_="layer-roads"),
        tag("g", _preview(state, preview), class_="layer-preview"),
        tag("g", _active_routes(state), class_="layer-routes"),
        tag("g", _road_costs(state), class_="layer-costs"),
        tag("g", _places(state), class_="layer-places"),
        tag("g", _incidents(state), class_="layer-incidents"),
        tag("g", _ambulances(state), class_="layer-vehicles"),
        xmlns="http://www.w3.org/2000/svg",
        viewBox=f"0 0 {WIDTH} {HEIGHT}",
        role="img",
        aria_labelledby="map-title map-desc",
        class_="town-map",
    )
