"""Load town data and save or restore simulation checkpoints."""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import simulation
from intake import CallDeck, CallScenario
from models import (
    AMBULANCE_STATUSES,
    CATEGORIES,
    DECISION_SOURCES,
    FACILITIES,
    INCIDENT_SOURCES,
    INCIDENT_STATUSES,
    LABEL_SIDES,
    MAX_PATIENTS,
    MIN_PATIENTS,
    PRIORITIES,
    Ambulance,
    Event,
    Hospital,
    Incident,
    Location,
    Road,
    SimulationState,
)

TOWN_FORMAT = "resq-town"
SHIFT_FORMAT = "resq-shift"
FORMAT_VERSION = 1

MAX_JSON_BYTES = 1_000_000
MAX_ITEMS = 100
MAX_INCIDENTS = 5_000
MAX_EVENTS = 20_000
MAX_MINUTE = 1_000_000
MAX_LABEL_LENGTH = 40

ID_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,40}$")
CHECKPOINT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}\.json$")


class StorageError(Exception):
    """A file could not be read, parsed or trusted."""


def read_json(path: Path, max_bytes: int = MAX_JSON_BYTES):
    """Read and parse one JSON file, turning every failure into StorageError."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise StorageError(f"{path.name} was not found.") from None
    except OSError as error:
        raise StorageError(f"{path.name} could not be opened ({error.strerror}).") from None
    if size > max_bytes:
        raise StorageError(f"{path.name} is too large to be a RESQ file ({size} bytes).")
    try:
        with path.open("r", encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeDecodeError:
        raise StorageError(f"{path.name} is not UTF-8 text.") from None
    except OSError as error:
        raise StorageError(f"{path.name} could not be read ({error.strerror}).") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise StorageError(f"{path.name} is not valid JSON (line {error.lineno}, column {error.colno}).") from None
    except RecursionError:
        raise StorageError(f"{path.name} is nested too deeply.") from None


def atomic_write_json(path: Path, data) -> None:
    """Write JSON so the file is either fully old or fully new, never half-written."""
    path = Path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class _Fields:
    """Reads one JSON object and reports precise errors such as 'roads[3].minutes'."""

    def __init__(self, data, where: str):
        if not isinstance(data, dict):
            raise StorageError(f"{where} should be a JSON object.")
        self.data = data
        self.where = where

    def _get(self, key: str):
        if key not in self.data:
            raise StorageError(f"{self.where}.{key} is missing.")
        return self.data[key]

    def text(self, key, max_length=80, pattern=None, allow_empty=False, default=None) -> str:
        if default is not None and key not in self.data:
            return default
        value = self._get(key)
        if not isinstance(value, str):
            raise StorageError(f"{self.where}.{key} should be text.")
        if (not value and not allow_empty) or len(value) > max_length:
            raise StorageError(f"{self.where}.{key} should be 1-{max_length} characters.")
        if pattern is not None and not pattern.match(value):
            raise StorageError(f"{self.where}.{key} contains characters that are not allowed.")
        return value

    def optional_text(self, key, max_length=600, pattern=None) -> str | None:
        if self.data.get(key) is None:
            return None
        return self.text(key, max_length, pattern)

    def integer(self, key, minimum, maximum, default=None) -> int:
        if default is not None and key not in self.data:
            return default
        value = self._get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise StorageError(f"{self.where}.{key} should be a whole number.")
        if not minimum <= value <= maximum:
            raise StorageError(f"{self.where}.{key} should be between {minimum} and {maximum}.")
        return value

    def optional_integer(self, key, minimum, maximum) -> int | None:
        if self.data.get(key) is None:
            return None
        return self.integer(key, minimum, maximum)

    def optional_choice(self, key, options) -> str | None:
        if self.data.get(key) is None:
            return None
        return self.choice(key, options)

    def boolean(self, key, default=None) -> bool:
        if default is not None and key not in self.data:
            return default
        value = self._get(key)
        if not isinstance(value, bool):
            raise StorageError(f"{self.where}.{key} should be true or false.")
        return value

    def choice(self, key, options, default=None) -> str:
        if default is not None and key not in self.data:
            return default
        value = self._get(key)
        if value not in options:
            raise StorageError(f"{self.where}.{key} should be one of: {', '.join(options)}.")
        return value

    def items(self, key, max_items=MAX_ITEMS, allow_empty=False) -> list:
        value = self._get(key)
        if not isinstance(value, list):
            raise StorageError(f"{self.where}.{key} should be a list.")
        if (not value and not allow_empty) or len(value) > max_items:
            raise StorageError(f"{self.where}.{key} should hold 1-{max_items} entries.")
        return value

    def child(self, key) -> "_Fields":
        return _Fields(self._get(key), f"{self.where}.{key}")


def _check_header(top: _Fields, expected_format: str) -> None:
    if top.data.get("format") != expected_format:
        raise StorageError(f"This is not a {expected_format} file.")
    version = top.data.get("version")
    if version != FORMAT_VERSION:
        raise StorageError(f"Unsupported file version {version!r}; this RESQ reads version {FORMAT_VERSION}.")


def _unique(items: dict, key, where: str) -> None:
    if key in items:
        raise StorageError(f"{where} repeats the identifier {key!r}.")


def _parse_locations(top: _Fields) -> dict[str, Location]:
    locations: dict[str, Location] = {}
    for index, item in enumerate(top.items("locations")):
        f = _Fields(item, f"locations[{index}]")
        location = Location(
            id=f.text("id", 40, ID_PATTERN),
            name=f.text("name", 40),
            x=f.integer("x", 0, 960),
            y=f.integer("y", 0, 560),
            label_side=f.choice("label_side", LABEL_SIDES, default="below"),
        )
        _unique(locations, location.id, "locations")
        locations[location.id] = location
    return locations


def _parse_roads(top: _Fields, locations, dynamic: bool) -> dict[str, Road]:
    roads: dict[str, Road] = {}
    for index, item in enumerate(top.items("roads")):
        f = _Fields(item, f"roads[{index}]")
        road = Road(
            id=f.text("id", 40, ID_PATTERN),
            a=f.choice("a", tuple(locations)),
            b=f.choice("b", tuple(locations)),
            minutes=f.integer("minutes", 1, 120),
            open=f.boolean("open") if dynamic else f.boolean("open", default=True),
        )
        _unique(roads, road.id, "roads")
        roads[road.id] = road
    return roads


def _parse_ambulances(top: _Fields, locations, roads, dynamic: bool) -> dict[str, Ambulance]:
    ambulances: dict[str, Ambulance] = {}
    for index, item in enumerate(top.items("ambulances")):
        f = _Fields(item, f"ambulances[{index}]")
        base = f.choice("base", tuple(locations))
        ambulance = Ambulance(
            id=f.text("id", 40, ID_PATTERN),
            name=f.text("name", 40),
            base=base,
            location=base,
            capacity=f.integer("capacity", 1, 12, default=4),
        )
        if dynamic:
            ambulance.location = f.choice("location", tuple(locations))
            ambulance.status = f.choice("status", AMBULANCE_STATUSES)
            ambulance.incident_id = f.optional_integer("incident_id", 1, MAX_INCIDENTS * 10)
            ambulance.hospital_id = f.optional_text("hospital_id", 40, ID_PATTERN)
            ambulance.edge_to = f.optional_text("edge_to", 40, ID_PATTERN)
            if ambulance.edge_to is not None and ambulance.edge_to not in locations:
                raise StorageError(f"ambulances[{index}].edge_to is an unknown place.")
            ambulance.edge_road = f.optional_text("edge_road", 40, ID_PATTERN)
            if ambulance.edge_road is not None and ambulance.edge_road not in roads:
                raise StorageError(f"ambulances[{index}].edge_road is an unknown road.")
            ambulance.edge_elapsed = f.integer("edge_elapsed", 0, 120)
            ambulance.blocked_reason = f.optional_text("blocked_reason", 600)
        _unique(ambulances, ambulance.id, "ambulances")
        ambulances[ambulance.id] = ambulance
    return ambulances


def _parse_hospitals(top: _Fields, locations, dynamic: bool) -> dict[str, Hospital]:
    hospitals: dict[str, Hospital] = {}
    for index, item in enumerate(top.items("hospitals")):
        f = _Fields(item, f"hospitals[{index}]")
        facilities = f.items("facilities", max_items=len(FACILITIES))
        if any(facility not in FACILITIES for facility in facilities) or len(set(facilities)) != len(facilities):
            raise StorageError(f"hospitals[{index}].facilities must list distinct values from: {', '.join(FACILITIES)}.")
        capacity = f.integer("capacity", 1, 500)
        hospital = Hospital(
            id=f.text("id", 40, ID_PATTERN),
            name=f.text("name", 60),
            location=f.choice("location", tuple(locations)),
            facilities=tuple(facilities),
            capacity=capacity,
            occupied=f.integer("occupied", 0, capacity),
            reserved=f.integer("reserved", 0, capacity) if dynamic else 0,
        )
        _unique(hospitals, hospital.id, "hospitals")
        hospitals[hospital.id] = hospital
    return hospitals


def _parse_incidents(top: _Fields, locations) -> dict[int, Incident]:
    incidents: dict[int, Incident] = {}
    for index, item in enumerate(top.items("incidents", MAX_INCIDENTS, allow_empty=True)):
        f = _Fields(item, f"incidents[{index}]")
        incident = Incident(
            id=f.integer("id", 1, MAX_INCIDENTS * 10),
            category=f.choice("category", CATEGORIES),
            priority=f.choice("priority", PRIORITIES),
            location=f.choice("location", tuple(locations)),
            patients=f.integer("patients", MIN_PATIENTS, MAX_PATIENTS),
            reported_minute=f.integer("reported_minute", 0, MAX_MINUTE),
            caller_name=f.text("caller_name", 60),
            caller_message=f.text("caller_message", 280, allow_empty=True),
            source=f.choice("source", INCIDENT_SOURCES),
            status=f.choice("status", INCIDENT_STATUSES),
            ambulance_id=f.optional_text("ambulance_id", 40, ID_PATTERN),
            hospital_id=f.optional_text("hospital_id", 40, ID_PATTERN),
            dispatched_minute=f.optional_integer("dispatched_minute", 0, MAX_MINUTE),
            pickup_minute=f.optional_integer("pickup_minute", 0, MAX_MINUTE),
            arrival_minute=f.optional_integer("arrival_minute", 0, MAX_MINUTE),
            completed_minute=f.optional_integer("completed_minute", 0, MAX_MINUTE),

            chosen_by=f.optional_choice("chosen_by", DECISION_SOURCES),
            recommended_ambulance_id=f.optional_text("recommended_ambulance_id", 40, ID_PATTERN),
            recommended_hospital_id=f.optional_text("recommended_hospital_id", 40, ID_PATTERN),
            recommended_minutes=f.optional_integer("recommended_minutes", 0, MAX_MINUTE),
            planned_minutes=f.optional_integer("planned_minutes", 0, MAX_MINUTE),
        )
        _unique(incidents, incident.id, "incidents")
        incidents[incident.id] = incident
    return incidents


def _parse_events(top: _Fields, incidents) -> list[Event]:
    events = []
    for index, item in enumerate(top.items("events", MAX_EVENTS, allow_empty=True)):
        f = _Fields(item, f"events[{index}]")
        event = Event(
            minute=f.integer("minute", 0, MAX_MINUTE),
            kind=f.text("kind", 20, ID_PATTERN),
            text=f.text("text", 600),
            incident_id=f.optional_integer("incident_id", 1, MAX_INCIDENTS * 10),
        )
        if event.incident_id is not None and event.incident_id not in incidents:
            raise StorageError(f"events[{index}] refers to an unknown incident.")
        events.append(event)
    return events


def _parse_calls(top: _Fields, locations, next_index: int = 0) -> CallDeck:
    scenarios = []
    for index, item in enumerate(top.items("calls", max_items=20)):
        f = _Fields(item, f"calls[{index}]")
        scenarios.append(CallScenario(
            caller_name=f.text("caller_name", 60),
            message=f.text("message", 280),
            category=f.choice("category", CATEGORIES),
            priority=f.choice("priority", PRIORITIES),
            location=f.choice("location", tuple(locations)),
            patients=f.integer("patients", MIN_PATIENTS, MAX_PATIENTS),
        ))
    if not 0 <= next_index < len(scenarios):
        raise StorageError("The call-deck position is out of range.")
    return CallDeck(tuple(scenarios), next_index)


def _verified(state: SimulationState, what: str) -> SimulationState:
    problems = simulation.check_invariants(state)
    if problems:
        raise StorageError(f"{what} failed a consistency check: {problems[0]}")
    return state


def town_from_document(document) -> tuple[SimulationState, CallDeck]:
    """Build a fresh shift from a parsed town document."""
    top = _Fields(document, "town")
    _check_header(top, TOWN_FORMAT)
    locations = _parse_locations(top)
    roads = _parse_roads(top, locations, dynamic=False)
    state = SimulationState(
        locations=locations,
        roads=roads,
        ambulances=_parse_ambulances(top, locations, roads, dynamic=False),
        hospitals=_parse_hospitals(top, locations, dynamic=False),
    )
    deck = _parse_calls(top, locations)
    return _verified(state, "The town data"), deck


def load_town(path: Path) -> tuple[SimulationState, CallDeck]:
    return town_from_document(read_json(path))


def state_to_document(state: SimulationState) -> dict:
    """Everything needed to rebuild the shift exactly, as plain JSON values."""
    return {
        "minute": state.minute,
        "next_incident_id": state.next_incident_id,
        "locations": [asdict(location) for location in state.locations.values()],
        "roads": [asdict(road) for road in state.sorted_roads()],
        "ambulances": [asdict(ambulance) for ambulance in state.sorted_ambulances()],
        "hospitals": [
            {**asdict(hospital), "facilities": list(hospital.facilities)} for hospital in state.sorted_hospitals()
        ],
        "incidents": [asdict(state.incidents[key]) for key in sorted(state.incidents)],
        "events": [asdict(event) for event in state.events],
    }


def deck_to_document(deck: CallDeck) -> dict:
    return {"next_index": deck.next_index, "calls": [asdict(call) for call in deck.scenarios]}


def shift_document(state: SimulationState, deck: CallDeck, label: str) -> dict:
    return {
        "format": SHIFT_FORMAT,
        "version": FORMAT_VERSION,
        "label": label,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "call_deck": deck_to_document(deck),
        "simulation": state_to_document(state),
    }


def shift_from_document(document) -> tuple[SimulationState, CallDeck, str]:
    """Rebuild and verify a saved shift."""
    top = _Fields(document, "shift")
    _check_header(top, SHIFT_FORMAT)
    label = top.text("label", MAX_LABEL_LENGTH)
    sim = top.child("simulation")
    locations = _parse_locations(sim)
    roads = _parse_roads(sim, locations, dynamic=True)
    incidents = _parse_incidents(sim, locations)
    state = SimulationState(
        locations=locations,
        roads=roads,
        ambulances=_parse_ambulances(sim, locations, roads, dynamic=True),
        hospitals=_parse_hospitals(sim, locations, dynamic=True),
        incidents=incidents,
        events=_parse_events(sim, incidents),
        minute=sim.integer("minute", 0, MAX_MINUTE),
        next_incident_id=sim.integer("next_incident_id", 1, MAX_INCIDENTS * 10 + 1),
    )
    deck_fields = top.child("call_deck")
    deck = _parse_calls(deck_fields, locations, deck_fields.integer("next_index", 0, 19))
    return _verified(state, "The saved shift"), deck, label


@dataclass(frozen=True)
class CheckpointInfo:
    """One row in the Saved shifts list."""

    name: str
    label: str = ""
    saved_at: str = ""
    minute: int | None = None
    completed: int = 0
    waiting: int = 0
    problem: str | None = None


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].strip("-") or "shift"


def checkpoint_filename(label: str, minute: int, existing: set[str]) -> str:
    """A new, unique file name such as 'demo-run-minute-12.json' (or '-2', '-3')."""
    base = f"{_slug(label)}-minute-{minute}"
    name = f"{base}.json"
    number = 2
    while name in existing:
        name = f"{base}-{number}.json"
        number += 1
    return name


def resolve_checkpoint(folder: Path, name: str) -> Path:
    """Turn a submitted checkpoint name into a path INSIDE the saves folder only."""
    if not isinstance(name, str) or not CHECKPOINT_PATTERN.match(name):
        raise StorageError("That checkpoint name is not valid.")
    folder = Path(folder).resolve()
    path = (folder / name).resolve()
    if path.parent != folder:
        raise StorageError("That checkpoint name is not valid.")
    if not path.is_file():
        raise StorageError(f"{name} no longer exists in the saves folder.")
    return path


def save_checkpoint(folder: Path, state: SimulationState, deck: CallDeck, label: str) -> str:
    """Save a new checkpoint (never overwriting another) and return its file name."""
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        existing = {entry.name for entry in folder.iterdir()}
        name = checkpoint_filename(label, state.minute, existing)
        atomic_write_json(folder / name, shift_document(state, deck, label))
    except OSError as error:
        raise StorageError(f"The shift could not be saved ({error.strerror or error}).") from None
    return name


def load_checkpoint(folder: Path, name: str) -> tuple[SimulationState, CallDeck, str]:
    return shift_from_document(read_json(resolve_checkpoint(folder, name)))


def list_checkpoints(folder: Path) -> list[CheckpointInfo]:
    """Summaries of saved shifts, oldest save first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    candidates = []
    for path in folder.iterdir():
        if CHECKPOINT_PATTERN.match(path.name) and path.is_file():
            candidates.append((path.stat().st_mtime_ns, len(path.name), path.name, path))
    rows = []
    for *_, path in sorted(candidates):
        try:
            raw = read_json(path)
            state, _, label = shift_from_document(raw)
        except StorageError as error:
            rows.append(CheckpointInfo(name=path.name, problem=str(error)))
            continue
        saved_at = raw["saved_at"][:25] if isinstance(raw.get("saved_at"), str) else ""
        rows.append(CheckpointInfo(
            name=path.name,
            label=label,
            saved_at=saved_at,
            minute=state.minute,
            completed=len(simulation.completed_incidents(state)),
            waiting=len(simulation.waiting_queue(state)),
        ))
    return rows
