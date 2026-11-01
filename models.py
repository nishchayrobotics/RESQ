"""Define the simulation data models."""

import re
from dataclasses import dataclass, field


PRIORITIES = ("critical", "high", "moderate")
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}

CATEGORIES = ("medical", "traffic_accident", "cardiac")
FACILITIES = ("general", "trauma", "cardiac")


REQUIRED_FACILITY = {
    "medical": "general",
    "traffic_accident": "trauma",
    "cardiac": "cardiac",
}

MIN_PATIENTS = 1
MAX_PATIENTS = 4
DEFAULT_AMBULANCE_CAPACITY = 4


AVAILABLE = "available"
TO_SCENE = "to_scene"
TO_HOSPITAL = "to_hospital"
HANDOVER = "handover"
AMBULANCE_STATUSES = (AVAILABLE, TO_SCENE, TO_HOSPITAL, HANDOVER)
MOVING_STATUSES = (TO_SCENE, TO_HOSPITAL)


WAITING = "waiting"
ASSIGNED = "assigned"
COMPLETED = "completed"
INCIDENT_STATUSES = (WAITING, ASSIGNED, COMPLETED)


INCIDENT_SOURCES = ("call", "manual")


RESQ_PICK = "resq"
DISPATCHER_CHOICE = "dispatcher"
DECISION_SOURCES = (RESQ_PICK, DISPATCHER_CHOICE)

LABEL_SIDES = ("above", "below", "left", "right")


def natural_key(identifier: str) -> tuple:
    """Sort key that orders "A2" before "A10" (numbers compared as numbers)."""
    parts = re.split(r"(\d+)", identifier)
    return tuple(int(part) if index % 2 else part for index, part in enumerate(parts))


def readable(value: str) -> str:
    """Turn an internal code such as "traffic_accident" into "traffic accident"."""
    return value.replace("_", " ")


@dataclass
class Location:
    """A named place on the map; x and y are map coordinates for drawing."""

    id: str
    name: str
    x: int
    y: int
    label_side: str = "below"


@dataclass
class Road:
    """A two-way road between two locations with a travel time in minutes."""

    id: str
    a: str
    b: str
    minutes: int
    open: bool = True

    def joins(self, first: str, second: str) -> bool:
        """True if this road connects the two places (in either direction)."""
        return {self.a, self.b} == {first, second}

    def other_end(self, node: str) -> str:
        """The place at the far end of the road from `node`."""
        if node == self.a:
            return self.b
        if node == self.b:
            return self.a
        raise ValueError(f"Road {self.id} does not touch {node}.")


@dataclass
class Ambulance:
    """One crewed ambulance."""

    id: str
    name: str
    base: str
    location: str
    capacity: int = DEFAULT_AMBULANCE_CAPACITY
    status: str = AVAILABLE
    incident_id: int | None = None
    hospital_id: str | None = None
    edge_to: str | None = None
    edge_road: str | None = None
    edge_elapsed: int = 0
    blocked_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.status == AVAILABLE

    @property
    def on_road(self) -> bool:
        return self.edge_to is not None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_reason is not None


@dataclass
class Hospital:
    """A hospital with a fixed number of beds."""

    id: str
    name: str
    location: str
    facilities: tuple[str, ...]
    capacity: int
    occupied: int = 0
    reserved: int = 0

    @property
    def free(self) -> int:
        return self.capacity - self.occupied - self.reserved

    def offers(self, facility: str) -> bool:
        return facility in self.facilities


@dataclass
class Incident:
    """One reported emergency: a group of 1-4 patients at a single place."""

    id: int
    category: str
    priority: str
    location: str
    patients: int
    reported_minute: int
    caller_name: str
    caller_message: str
    source: str
    status: str = WAITING
    ambulance_id: str | None = None
    hospital_id: str | None = None
    dispatched_minute: int | None = None
    pickup_minute: int | None = None
    arrival_minute: int | None = None
    completed_minute: int | None = None
    chosen_by: str | None = None
    recommended_ambulance_id: str | None = None
    recommended_hospital_id: str | None = None
    recommended_minutes: int | None = None
    planned_minutes: int | None = None


@dataclass
class Event:
    """One timestamped line in the shift log (decisions, movements, changes)."""

    minute: int
    kind: str
    text: str
    incident_id: int | None = None


@dataclass(frozen=True)
class DispatchPlan:
    """A proposed assignment: which ambulance, which hospital, which roads."""

    incident_id: int
    ambulance_id: str
    hospital_id: str
    route_to_scene: tuple[str, ...]
    route_to_hospital: tuple[str, ...]
    minutes_to_scene: int
    minutes_to_hospital: int
    planned_at_minute: int
    reasons: tuple[str, ...] = ()
    chosen_by: str = RESQ_PICK

    @property
    def total_minutes(self) -> int:
        return self.minutes_to_scene + self.minutes_to_hospital

    def key(self) -> tuple:
        """The parts of the plan that must still match at confirmation."""
        return (
            self.incident_id,
            self.ambulance_id,
            self.hospital_id,
            self.route_to_scene,
            self.route_to_hospital,
        )


@dataclass(frozen=True)
class DispatchOptions:
    """Every crew and hospital that could serve one incident right now."""

    incident_id: int
    facility: str
    ambulances: tuple
    hospitals: tuple
    ambulance_excluded: tuple[str, ...] = ()
    hospital_excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class Infeasibility:
    """Why an incident cannot be served right now."""

    incident_id: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Recommendation:
    """The next dispatch to review, plus higher-priority calls that must wait."""

    plan: DispatchPlan | None
    held: tuple[Infeasibility, ...] = ()


@dataclass
class SimulationState:
    """Everything needed to continue a shift: the town plus what has happened."""

    locations: dict[str, Location]
    roads: dict[str, Road]
    ambulances: dict[str, Ambulance]
    hospitals: dict[str, Hospital]
    incidents: dict[int, Incident] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    minute: int = 0
    next_incident_id: int = 1

    def place_name(self, node: str) -> str:
        location = self.locations.get(node)
        return location.name if location else node

    def road_label(self, road: Road) -> str:
        return f"{self.place_name(road.a)} – {self.place_name(road.b)}"

    def sorted_ambulances(self) -> list[Ambulance]:
        return [self.ambulances[key] for key in sorted(self.ambulances, key=natural_key)]

    def sorted_hospitals(self) -> list[Hospital]:
        return [self.hospitals[key] for key in sorted(self.hospitals, key=natural_key)]

    def sorted_roads(self) -> list[Road]:
        return [self.roads[key] for key in sorted(self.roads, key=natural_key)]

    def hospital_at(self, node: str) -> Hospital | None:
        for hospital in self.sorted_hospitals():
            if hospital.location == node:
                return hospital
        return None
