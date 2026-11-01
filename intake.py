"""Prepare incoming calls and validate incident details."""

import unicodedata
from dataclasses import dataclass

from models import CATEGORIES, MAX_PATIENTS, MIN_PATIENTS, PRIORITIES, readable

MAX_NAME_LENGTH = 60
MAX_MESSAGE_LENGTH = 280

CATEGORY_LABELS = {
    "medical": "Medical",
    "traffic_accident": "Traffic accident",
    "cardiac": "Cardiac",
}
PRIORITY_LABELS = {"critical": "Critical", "high": "High", "moderate": "Moderate"}
FACILITY_LABELS = {"general": "General ward", "trauma": "Trauma unit", "cardiac": "Cardiac unit"}
AMBULANCE_STATUS_LABELS = {
    "available": "Available",
    "to_scene": "Driving to scene",
    "to_hospital": "Transporting",
    "handover": "Handing over",
}
INCIDENT_STATUS_LABELS = {"waiting": "Waiting", "assigned": "Crew assigned", "completed": "Completed"}
SOURCE_LABELS = {"call": "Scripted call", "manual": "Manual report"}


def label(mapping: dict[str, str], value: str) -> str:
    """Friendly label for a code, falling back to a readable version of it."""
    return mapping.get(value, readable(value).capitalize())


@dataclass(frozen=True)
class CallScenario:
    """One scripted caller: who rings, what they say, and the preset details."""

    caller_name: str
    message: str
    category: str
    priority: str
    location: str
    patients: int


class CallDeck:
    """A fixed, repeating sequence of scripted calls."""

    def __init__(self, scenarios: tuple[CallScenario, ...], next_index: int = 0):
        if not scenarios:
            raise ValueError("The call deck needs at least one scenario.")
        if not 0 <= next_index < len(scenarios):
            raise ValueError("The call deck position is out of range.")
        self.scenarios = tuple(scenarios)
        self.next_index = next_index

    def __len__(self) -> int:
        return len(self.scenarios)

    def peek(self) -> CallScenario:
        """The caller who will be answered next (does not advance the deck)."""
        return self.scenarios[self.next_index]

    def take(self) -> CallScenario:
        """Answer the next caller and move round the deck."""
        call = self.scenarios[self.next_index]
        self.next_index = (self.next_index + 1) % len(self.scenarios)
        return call


def clean_text(value: object) -> str:
    """Collapse whitespace and drop control characters from typed text."""
    if not isinstance(value, str):
        return ""
    kept = []
    for character in value:
        if character in "\r\n\t":
            kept.append(" ")
        elif unicodedata.category(character) != "Cc":
            kept.append(character)
    return " ".join("".join(kept).split())


def validate_report_form(fields: dict[str, str], location_ids) -> tuple[dict, dict[str, str]]:
    """Check the manual report form."""
    errors: dict[str, str] = {}

    category = fields.get("category", "")
    if category not in CATEGORIES:
        errors["category"] = "Choose a category from the list."
    priority = fields.get("priority", "")
    if priority not in PRIORITIES:
        errors["priority"] = "Choose a priority from the list."
    location = fields.get("location", "")
    if location not in location_ids:
        errors["location"] = "Choose a place in the Harbour District."

    patients_text = fields.get("patients", "").strip()
    patients = 0
    if patients_text.isdigit() and patients_text.isascii():
        patients = int(patients_text)
    if not MIN_PATIENTS <= patients <= MAX_PATIENTS:
        errors["patients"] = f"Patients must be a whole number from {MIN_PATIENTS} to {MAX_PATIENTS}."

    caller_name = clean_text(fields.get("caller_name", "")) or "Unknown caller"
    if len(caller_name) > MAX_NAME_LENGTH:
        errors["caller_name"] = f"Keep the caller name to {MAX_NAME_LENGTH} characters or fewer."
    caller_message = clean_text(fields.get("caller_message", ""))
    if len(caller_message) > MAX_MESSAGE_LENGTH:
        errors["caller_message"] = f"Keep the message to {MAX_MESSAGE_LENGTH} characters or fewer."

    clean = {
        "category": category,
        "priority": priority,
        "location": location,
        "patients": patients,
        "caller_name": caller_name,
        "caller_message": caller_message,
    }
    return clean, errors
