"""Handle user actions and validate state changes."""

import copy
import hashlib
import hmac
import json
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import simulation
import storage
from intake import CallDeck, clean_text, validate_report_form
from models import DISPATCHER_CHOICE, DispatchOptions, DispatchPlan, Recommendation, SimulationState

ADVANCE_CHOICES = (1, 5)
PLAN_FIELDS = ("revision", "incident", "ambulance", "hospital", "scene_route", "hospital_route")
MAX_NOTICES = 64

STALE_MESSAGE = (
    "That page was out of date - another action or tab changed the shift first. "
    "Nothing was changed. The latest state is shown below."
)


class ActionError(Exception):
    """An action was refused."""

    def __init__(self, message: str, status: int = 400, field_errors=None, values=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.field_errors = field_errors or {}
        self.values = values or {}


@dataclass(frozen=True)
class Review:
    """Everything the review panel needs, worked out without changing anything."""

    recommendation: Recommendation
    options: DispatchOptions | None
    plan: DispatchPlan | None
    problem: str | None = None


def build_review(state: SimulationState, ambulance_id: str | None = None, hospital_id: str | None = None) -> Review:
    """Prepare the review of the next dispatch, optionally with the dispatcher's own choice of crew and/or hospital."""
    recommendation = simulation.recommend(state)
    recommended = recommendation.plan
    if recommended is None:
        return Review(recommendation, None, None)
    incident = state.incidents[recommended.incident_id]
    options = simulation.dispatch_options(state, incident)
    if not ambulance_id and not hospital_id:
        return Review(recommendation, options, recommended)
    choice = simulation.plan_with_choice(state, incident, ambulance_id or recommended.ambulance_id,
                                         hospital_id or recommended.hospital_id)
    if isinstance(choice, DispatchPlan):
        return Review(recommendation, options, choice)
    return Review(recommendation, options, recommended, "That choice isn't possible: " + " ".join(choice.reasons))


@dataclass(frozen=True)
class Snapshot:
    """A consistent view of the shift for drawing one page."""

    state: SimulationState
    deck: CallDeck
    revision: int


class DispatchApp:
    """One local shift, shared by every browser tab that talks to this server."""

    def __init__(self, town_path, saves_dir=None, secret: bytes | None = None):
        self.town_path = Path(town_path)
        self.saves_dir = Path(saves_dir) if saves_dir is not None else None
        self._lock = threading.RLock()
        self._secret = secret or secrets.token_bytes(32)
        self.form_token = secrets.token_urlsafe(24)
        self.state, self.deck = storage.load_town(self.town_path)
        self.revision = 1
        self._notices: OrderedDict[str, tuple[str, str]] = OrderedDict()


    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self.state, self.deck, self.revision)

    def checkpoints(self) -> list[storage.CheckpointInfo]:
        if self.saves_dir is None:
            return []
        with self._lock:
            return storage.list_checkpoints(self.saves_dir)


    def add_notice(self, text: str, kind: str = "success") -> str:
        """Store a message to show after a redirect; returns its lookup key."""
        with self._lock:
            key = secrets.token_urlsafe(8)
            self._notices[key] = (kind, text)
            while len(self._notices) > MAX_NOTICES:
                self._notices.popitem(last=False)
            return key

    def notice(self, key: str | None) -> tuple[str, str] | None:
        with self._lock:
            return self._notices.get(key) if key else None


    def _check_revision(self, submitted) -> None:
        try:
            value = int(submitted)
        except (TypeError, ValueError):
            raise ActionError("The form was missing its page revision. Reload the page and try again.") from None
        if value != self.revision:
            raise ActionError(STALE_MESSAGE, status=409)

    def _transaction(self, submitted_revision, change) -> str:
        """Run `change(state, deck)` on copies; keep the result only if it is valid."""
        with self._lock:
            self._check_revision(submitted_revision)
            state = copy.deepcopy(self.state)
            deck = copy.deepcopy(self.deck)
            try:
                message = change(state, deck)
            except simulation.PlanRejected as error:
                raise ActionError(str(error), status=409) from None
            except simulation.SimulationError as error:
                raise ActionError(str(error), status=400) from None
            problems = simulation.check_invariants(state)
            if problems:
                raise ActionError("A safety check failed, so the change was rolled back: " + problems[0], status=500)
            self.state, self.deck = state, deck
            self.revision += 1
            return message

    def _replace_shift(self, state: SimulationState, deck: CallDeck) -> None:
        self.state, self.deck = state, deck
        self.revision += 1


    def _sign(self, fields: dict) -> str:
        payload = json.dumps([str(fields.get(name, "")) for name in PLAN_FIELDS], separators=(",", ":"))
        return hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def plan_form_fields(self, plan: DispatchPlan, revision: int) -> dict[str, str]:
        """Hidden fields for the confirm form, including the signature."""
        fields = {
            "revision": str(revision),
            "incident": str(plan.incident_id),
            "ambulance": plan.ambulance_id,
            "hospital": plan.hospital_id,
            "scene_route": ">".join(plan.route_to_scene),
            "hospital_route": ">".join(plan.route_to_hospital),
        }
        fields["signature"] = self._sign(fields)
        return fields


    def receive_call(self, revision) -> str:
        def change(state, deck):
            call = deck.take()
            incident = simulation.report_incident(
                state,
                category=call.category,
                priority=call.priority,
                location=call.location,
                patients=call.patients,
                caller_name=call.caller_name,
                caller_message=call.message,
                source="call",
            )
            return f"Call answered: {call.caller_name} reported incident #{incident.id} at {state.place_name(call.location)}."

        return self._transaction(revision, change)

    def report_incident(self, fields: dict[str, str]) -> str:
        def change(state, deck):
            clean, errors = validate_report_form(fields, state.locations)
            if errors:
                raise ActionError("Please fix the highlighted fields. Nothing was added.", 400, errors, fields)
            incident = simulation.report_incident(state, source="manual", **clean)
            return f"Manual report added as incident #{incident.id}."

        return self._transaction(fields.get("revision"), change)

    def confirm_dispatch(self, fields: dict[str, str]) -> str:
        """Send the reviewed plan: RESQ's pick, or the dispatcher's own choice."""
        def change(state, deck):
            if not hmac.compare_digest(self._sign(fields).encode(), str(fields.get("signature", "")).encode()):
                raise ActionError("The dispatch form did not match the reviewed plan, so nothing was sent.", 400)
            submitted = (
                int(fields["incident"]),
                fields["ambulance"],
                fields["hospital"],
                tuple(fields["scene_route"].split(">")),
                tuple(fields["hospital_route"].split(">")),
            )
            review = build_review(state, fields["ambulance"], fields["hospital"])
            plan = review.plan
            if plan is None or review.problem or plan.key() != submitted:
                raise ActionError("The situation changed after review, so nothing was sent. Review again.", 409)
            incident = simulation.commit_plan(state, plan)
            hospital = state.hospitals[plan.hospital_id]
            whose = "Your choice" if plan.chosen_by == DISPATCHER_CHOICE else "RESQ's pick"
            return (f"{whose}: {plan.ambulance_id} is on its way to incident #{incident.id}. "
                    f"{incident.patients} bed(s) reserved at {hospital.name}.")

        return self._transaction(fields.get("revision"), change)

    def advance(self, revision, minutes_text) -> str:
        minutes = int(minutes_text) if str(minutes_text).isdigit() else 0
        if minutes not in ADVANCE_CHOICES:
            raise ActionError("Time can be advanced by 1 or 5 minutes.")

        def change(state, deck):
            simulation.advance_time(state, minutes)
            return f"Clock advanced {minutes} minute(s) to minute {state.minute}."

        return self._transaction(revision, change)

    def set_road(self, revision, road_id: str, action: str) -> str:
        if action not in ("close", "reopen"):
            raise ActionError("Unknown road action.")

        def change(state, deck):
            if action == "close":
                simulation.close_road(state, road_id)
            else:
                simulation.reopen_road(state, road_id)
            return f"Road {'closed' if action == 'close' else 'reopened'}: {state.road_label(state.roads[road_id])}."

        return self._transaction(revision, change)

    def discharge(self, revision, hospital_id: str, count_text) -> str:
        count = int(count_text) if str(count_text).isdigit() else 0

        def change(state, deck):
            simulation.discharge_patients(state, hospital_id, count)
            hospital = state.hospitals[hospital_id]
            return f"{hospital.name} discharged {count} patient(s); {hospital.free} bed(s) free."

        return self._transaction(revision, change)

    def save_shift(self, revision, label_text) -> str:
        if self.saves_dir is None:
            raise ActionError("Saving is switched off in this mode.")
        label = clean_text(label_text) or "Shift"
        if len(label) > storage.MAX_LABEL_LENGTH:
            raise ActionError(f"Keep the checkpoint label to {storage.MAX_LABEL_LENGTH} characters or fewer.")
        with self._lock:
            self._check_revision(revision)
            try:
                name = storage.save_checkpoint(self.saves_dir, self.state, self.deck, label)
            except storage.StorageError as error:
                raise ActionError(str(error), status=500) from None
            return f"Saved minute {self.state.minute} as {name}."

    def restore_shift(self, revision, name: str, confirmed: str) -> str:
        if self.saves_dir is None:
            raise ActionError("Saving is switched off in this mode.")
        with self._lock:
            self._check_revision(revision)
            if confirmed != "yes":
                raise ActionError("Tick 'Replace the current shift' to confirm the restore. Nothing was changed.")
            try:
                state, deck, label = storage.load_checkpoint(self.saves_dir, name)
            except storage.StorageError as error:
                raise ActionError(f"Could not restore {name}: {error} Your current shift was kept.") from None
            self._replace_shift(state, deck)
            return f"Restored '{label}' at minute {state.minute}."

    def new_shift(self, revision, confirmed: str) -> str:
        with self._lock:
            self._check_revision(revision)
            if confirmed != "yes":
                raise ActionError("Tick 'Replace the current shift' to start a new one. Nothing was changed.")
            try:
                state, deck = storage.load_town(self.town_path)
            except storage.StorageError as error:
                raise ActionError(f"Could not start a new shift: {error} Your current shift was kept.", 500) from None
            self._replace_shift(state, deck)
            return "A new shift has started at minute 0."
