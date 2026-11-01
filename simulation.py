"""Manage dispatch, travel, road closures and hospital capacity."""

import routing
from models import (
    AMBULANCE_STATUSES,
    ASSIGNED,
    AVAILABLE,
    CATEGORIES,
    COMPLETED,
    DECISION_SOURCES,
    DISPATCHER_CHOICE,
    FACILITIES,
    HANDOVER,
    INCIDENT_SOURCES,
    INCIDENT_STATUSES,
    MAX_PATIENTS,
    MIN_PATIENTS,
    MOVING_STATUSES,
    PRIORITIES,
    PRIORITY_RANK,
    REQUIRED_FACILITY,
    RESQ_PICK,
    TO_HOSPITAL,
    TO_SCENE,
    WAITING,
    Ambulance,
    DispatchOptions,
    DispatchPlan,
    Event,
    Hospital,
    Incident,
    Infeasibility,
    Recommendation,
    SimulationState,
    natural_key,
    readable,
)

MAX_ADVANCE_MINUTES = 60
MAX_CALLER_NAME = 60
MAX_CALLER_MESSAGE = 280


class SimulationError(Exception):
    """An action was refused."""


class InvalidAction(SimulationError):
    """The request itself was not valid (unknown road, bad number, ...)."""


class PlanRejected(SimulationError):
    """A reviewed plan no longer matches the current situation."""


def log_event(state: SimulationState, kind: str, text: str, incident_id: int | None = None) -> None:
    """Append one timestamped line to the shift log."""
    state.events.append(Event(state.minute, kind, text, incident_id))


def route_tree(state: SimulationState, root: str) -> routing.RouteTree:
    """Shortest times from every place to `root` using currently open roads."""
    return routing.dijkstra(state.locations.keys(), state.roads.values(), root)


def route_text(state: SimulationState, path) -> str:
    """Human-readable path such as "Harbour Station → Market Square"."""
    return " → ".join(state.place_name(node) for node in path)


def beds(count: int) -> str:
    return f"{count} bed" if count == 1 else f"{count} beds"


def patients_text(count: int) -> str:
    return f"{count} patient" if count == 1 else f"{count} patients"


def waiting_queue(state: SimulationState) -> list[Incident]:
    """Waiting incidents in dispatch order."""
    waiting = [incident for incident in state.incidents.values() if incident.status == WAITING]
    return sorted(waiting, key=lambda i: (PRIORITY_RANK[i.priority], i.reported_minute, i.id))


def active_incidents(state: SimulationState) -> list[Incident]:
    return sorted((i for i in state.incidents.values() if i.status == ASSIGNED), key=lambda i: i.id)


def completed_incidents(state: SimulationState) -> list[Incident]:
    return sorted((i for i in state.incidents.values() if i.status == COMPLETED), key=lambda i: i.id)


def report_incident(
    state: SimulationState,
    *,
    category: str,
    priority: str,
    location: str,
    patients: int,
    caller_name: str,
    caller_message: str,
    source: str,
) -> Incident:
    """Add a new waiting incident."""
    problems = []
    if category not in CATEGORIES:
        problems.append("unknown category")
    if priority not in PRIORITIES:
        problems.append("unknown priority")
    if location not in state.locations:
        problems.append("unknown location")
    if not isinstance(patients, int) or isinstance(patients, bool) or not MIN_PATIENTS <= patients <= MAX_PATIENTS:
        problems.append(f"patients must be {MIN_PATIENTS}-{MAX_PATIENTS}")
    if source not in INCIDENT_SOURCES:
        problems.append("unknown source")
    if not isinstance(caller_name, str) or not 1 <= len(caller_name) <= MAX_CALLER_NAME:
        problems.append("caller name must be 1-60 characters")
    if not isinstance(caller_message, str) or len(caller_message) > MAX_CALLER_MESSAGE:
        problems.append("caller message is too long")
    if problems:
        raise InvalidAction("Incident not added: " + "; ".join(problems) + ".")

    incident = Incident(
        id=state.next_incident_id,
        category=category,
        priority=priority,
        location=location,
        patients=patients,
        reported_minute=state.minute,
        caller_name=caller_name,
        caller_message=caller_message,
        source=source,
    )
    state.incidents[incident.id] = incident
    state.next_incident_id += 1
    log_event(
        state,
        "report",
        f"Incident #{incident.id} reported by {caller_name}: {priority} {readable(category)} "
        f"at {state.place_name(location)}, {patients_text(patients)}.",
        incident.id,
    )
    return incident


def _ambulance_choices(state, incident, scene_tree):
    """Available, big-enough, reachable ambulances sorted by (minutes, ID)."""
    choices, excluded = [], []
    for ambulance in state.sorted_ambulances():
        if ambulance.status != AVAILABLE:
            excluded.append(f"{ambulance.id} is busy ({readable(ambulance.status)}).")
            continue
        if ambulance.capacity < incident.patients:
            excluded.append(f"{ambulance.id} carries at most {patients_text(ambulance.capacity)}.")
            continue
        minutes = scene_tree.time_from(ambulance.location)
        if minutes is None:
            excluded.append(f"{ambulance.id} has no open road to the scene.")
            continue
        choices.append((minutes, natural_key(ambulance.id), ambulance))
    choices.sort(key=lambda choice: (choice[0], choice[1]))
    return [(minutes, ambulance) for minutes, _, ambulance in choices], excluded


def _hospital_choices(state, facility, patients, origin_tree, exclude_id=None):
    """Suitable, reachable hospitals with room, sorted by (minutes, ID)."""
    choices, excluded = [], []
    for hospital in state.sorted_hospitals():
        if hospital.id == exclude_id:
            continue
        if not hospital.offers(facility):
            excluded.append(f"{hospital.name} has no {facility} unit.")
            continue
        if hospital.free < patients:
            excluded.append(f"{hospital.name} has only {beds(hospital.free)} free.")
            continue
        minutes = origin_tree.time_from(hospital.location)
        if minutes is None:
            excluded.append(f"{hospital.name} cannot be reached on open roads.")
            continue
        choices.append((minutes, natural_key(hospital.id), hospital))
    choices.sort(key=lambda choice: (choice[0], choice[1]))
    return [(minutes, hospital) for minutes, _, hospital in choices], excluded


def dispatch_options(state: SimulationState, incident: Incident) -> DispatchOptions:
    """Every crew and hospital that could serve this incident now, best first."""
    facility = REQUIRED_FACILITY[incident.category]
    scene_tree = route_tree(state, incident.location)
    ambulances, ambulance_excluded = _ambulance_choices(state, incident, scene_tree)
    hospitals, hospital_excluded = _hospital_choices(state, facility, incident.patients, scene_tree)
    return DispatchOptions(incident.id, facility, tuple(ambulances), tuple(hospitals),
                           tuple(ambulance_excluded), tuple(hospital_excluded))


def _scarcity_notes(state: SimulationState, hospital: Hospital, incident: Incident) -> list[str]:
    """Explain what a booking that fills a hospital means for later calls."""
    if hospital.free - incident.patients > 0:
        return []
    needed = REQUIRED_FACILITY[incident.category]
    others = [other for other in state.hospitals.values() if other.id != hospital.id]
    unique = [facility for facility in hospital.facilities if not any(other.offers(facility) for other in others)]
    if unique and needed not in unique:
        return [f"Heads-up: this booking fills {hospital.name}, the only hospital with a {unique[0]} unit, "
                f"with a patient who doesn't need one. The next {unique[0]} patient will have to wait for a discharge."]
    if unique:
        return [f"Note: this takes the last bed at {hospital.name}, the only {unique[0]} unit. "
                f"Another {unique[0]} patient would have to wait for a discharge."]
    return [f"Note: this booking fills {hospital.name} (no free beds afterwards)."]


def _build_plan(state, incident, ambulance, ambulance_minutes, hospital, hospital_minutes, reasons, chosen_by):
    """Turn a chosen crew and hospital into a frozen plan, checking BOTH legs."""
    route_to_scene = route_tree(state, incident.location).path_from(ambulance.location)
    route_to_hospital = route_tree(state, hospital.location).path_from(incident.location)
    if (
        route_to_scene is None
        or route_to_hospital is None
        or routing.path_minutes(state.roads.values(), route_to_scene) != ambulance_minutes
        or routing.path_minutes(state.roads.values(), route_to_hospital) != hospital_minutes
    ):
        return Infeasibility(incident.id, ("The route check failed, so no plan is offered.",))
    return DispatchPlan(
        incident_id=incident.id,
        ambulance_id=ambulance.id,
        hospital_id=hospital.id,
        route_to_scene=tuple(route_to_scene),
        route_to_hospital=tuple(route_to_hospital),
        minutes_to_scene=ambulance_minutes,
        minutes_to_hospital=hospital_minutes,
        planned_at_minute=state.minute,
        reasons=tuple(reasons),
        chosen_by=chosen_by,
    )


def plan_for_incident(state: SimulationState, incident: Incident) -> DispatchPlan | Infeasibility:
    """RESQ's pick for ONE incident, or why it must wait."""
    if incident.status != WAITING:
        return Infeasibility(incident.id, ("This incident is no longer waiting.",))

    options = dispatch_options(state, incident)
    scene_name = state.place_name(incident.location)
    reasons = []
    if not options.ambulances:
        reasons.append(f"No available ambulance with room for {patients_text(incident.patients)} can reach {scene_name}.")
        reasons.extend(options.ambulance_excluded)
    if not options.hospitals:
        reasons.append(f"No reachable hospital with a {options.facility} unit has {beds(incident.patients)} free.")
        reasons.extend(options.hospital_excluded)
    if reasons:
        return Infeasibility(incident.id, tuple(reasons))

    ambulance_minutes, ambulance = options.ambulances[0]
    hospital_minutes, hospital = options.hospitals[0]
    if len(options.ambulances) > 1:
        runner_up = f" Next closest: {options.ambulances[1][1].id} at {options.ambulances[1][0]} min."
    else:
        runner_up = " No other ambulance is free."
    if ambulance_minutes == 0:
        ambulance_reason = (f"{ambulance.id} ({ambulance.name}) is already at {scene_name} and has room for "
                            f"{patients_text(incident.patients)}.")
    else:
        ambulance_reason = (
            f"{ambulance.id} ({ambulance.name}) is the nearest free crew with room for "
            f"{patients_text(incident.patients)}: {ambulance_minutes} min from {state.place_name(ambulance.location)}."
        )
    hospital_reason = (
        f"{hospital.name} is the closest hospital with a {options.facility} unit and {beds(incident.patients)} free: "
        f"{hospital_minutes} min from the scene ({hospital.free} free before this booking)."
    )
    if len(options.hospitals) > 1:
        hospital_reason += f" Next: {options.hospitals[1][1].name} at {options.hospitals[1][0]} min."
    plan_reasons = [ambulance_reason + runner_up, hospital_reason]
    if options.hospital_excluded:
        plan_reasons.append("Ruled out: " + " ".join(options.hospital_excluded))
    plan_reasons.append(
        f"Both legs checked on open roads: {ambulance_minutes} min to the scene, then "
        f"{hospital_minutes} min to hospital, then a 1-minute handover."
    )
    plan_reasons.extend(_scarcity_notes(state, hospital, incident))
    return _build_plan(state, incident, ambulance, ambulance_minutes, hospital, hospital_minutes,
                       plan_reasons, RESQ_PICK)


def _extra(minutes: int) -> str:
    return "no extra time" if minutes == 0 else f"+{minutes} min"


def plan_with_choice(state: SimulationState, incident: Incident, ambulance_id: str,
                     hospital_id: str) -> DispatchPlan | Infeasibility:
    """The dispatcher's own choice of crew and hospital, with its cost compared with RESQ's pick."""
    recommended = plan_for_incident(state, incident)
    if not isinstance(recommended, DispatchPlan):
        return recommended
    if (ambulance_id, hospital_id) == (recommended.ambulance_id, recommended.hospital_id):
        return recommended

    options = dispatch_options(state, incident)
    ambulance_entry = next(((m, a) for m, a in options.ambulances if a.id == ambulance_id), None)
    hospital_entry = next(((m, h) for m, h in options.hospitals if h.id == hospital_id), None)
    problems = []
    if ambulance_entry is None:
        why = [note for note in options.ambulance_excluded if note.startswith(f"{ambulance_id} ")]
        problems.append(why[0] if why else "That ambulance does not exist.")
    if hospital_entry is None:
        hospital = state.hospitals.get(hospital_id)
        why = [note for note in options.hospital_excluded if hospital is not None and note.startswith(hospital.name)]
        problems.append(why[0] if why else "That hospital does not exist.")
    if problems:
        return Infeasibility(incident.id, tuple(problems))

    ambulance_minutes, ambulance = ambulance_entry
    hospital_minutes, hospital = hospital_entry
    scene_extra = ambulance_minutes - recommended.minutes_to_scene
    hospital_extra = hospital_minutes - recommended.minutes_to_hospital
    total_extra = scene_extra + hospital_extra
    handover = state.minute + ambulance_minutes + hospital_minutes + 1
    recommended_handover = state.minute + recommended.total_minutes + 1
    recommended_hospital = state.hospitals[recommended.hospital_id]
    if ambulance.id == recommended.ambulance_id:
        crew = f"Your crew: {ambulance.id} ({ambulance.name}), the same as RESQ's pick."
    else:
        where = ("already at the scene" if ambulance_minutes == 0
                 else f"{ambulance_minutes} min away at {state.place_name(ambulance.location)}")
        crew = (f"Your crew: {ambulance.id} ({ambulance.name}), {where}. RESQ's pick, {recommended.ambulance_id}, "
                f"is {recommended.minutes_to_scene} min away: {_extra(scene_extra)} to reach the patients.")
    if hospital.id == recommended.hospital_id:
        destination = f"Your hospital: {hospital.name}, the same as RESQ's pick."
    else:
        destination = (f"Your hospital: {hospital.name}, {hospital_minutes} min from the scene ({hospital.free} free "
                       f"before this booking). RESQ's pick, {recommended_hospital.name}, is "
                       f"{recommended.minutes_to_hospital} min away: {_extra(hospital_extra)} on the way to hospital.")
    reasons = [
        crew,
        destination,
        (f"Overall: {_extra(total_extra)}. Expected handover at minute {handover} instead of {recommended_handover}."
         if total_extra else f"Overall: no extra time. Expected handover at minute {handover}, the same as RESQ's pick."),
        f"Both legs checked on open roads: {ambulance_minutes} min to the scene, then {hospital_minutes} min "
        f"to hospital, then a 1-minute handover.",
    ]
    reasons.extend(_scarcity_notes(state, hospital, incident))
    return _build_plan(state, incident, ambulance, ambulance_minutes, hospital, hospital_minutes,
                       reasons, DISPATCHER_CHOICE)


def recommend(state: SimulationState) -> Recommendation:
    """The highest-priority incident that CAN be served now, plus any higher-priority incidents that must keep waiting (with reasons)."""
    held = []
    for incident in waiting_queue(state):
        result = plan_for_incident(state, incident)
        if isinstance(result, DispatchPlan):
            return Recommendation(result, tuple(held))
        held.append(result)
    return Recommendation(None, tuple(held))


def commit_plan(state: SimulationState, plan: DispatchPlan) -> Incident:
    """Send the crew: re-check the plan, then reserve the ambulance and beds."""
    incident = state.incidents.get(plan.incident_id)
    if incident is None:
        raise PlanRejected("That incident does not exist.")
    if incident.status != WAITING:
        raise PlanRejected(f"Incident #{incident.id} has already been dispatched.")
    recommended = recommend(state).plan
    if recommended is None or recommended.incident_id != incident.id:
        raise PlanRejected("The situation changed after review, so nothing was sent. Review the next dispatch again.")
    if plan.chosen_by == DISPATCHER_CHOICE:
        fresh = plan_with_choice(state, incident, plan.ambulance_id, plan.hospital_id)
    else:
        fresh = recommended
    if not isinstance(fresh, DispatchPlan) or fresh.key() != plan.key():
        raise PlanRejected("The situation changed after review, so nothing was sent. Review the next dispatch again.")
    plan = fresh

    ambulance = state.ambulances[plan.ambulance_id]
    hospital = state.hospitals[plan.hospital_id]

    ambulance.status = TO_SCENE
    ambulance.incident_id = incident.id
    ambulance.hospital_id = hospital.id
    ambulance.blocked_reason = None
    hospital.reserved += incident.patients
    incident.status = ASSIGNED
    incident.ambulance_id = ambulance.id
    incident.hospital_id = hospital.id
    incident.dispatched_minute = state.minute
    incident.chosen_by = plan.chosen_by
    incident.recommended_ambulance_id = recommended.ambulance_id
    incident.recommended_hospital_id = recommended.hospital_id
    incident.recommended_minutes = recommended.total_minutes
    incident.planned_minutes = plan.total_minutes

    who = "Dispatcher confirmed RESQ's pick" if plan.chosen_by == RESQ_PICK else "Dispatcher chose their own plan"
    log_event(
        state,
        "dispatch",
        f"{who}: {ambulance.id} to {state.place_name(incident.location)}, then {hospital.name}. "
        f"{beds(incident.patients).capitalize()} reserved at {hospital.name}.",
        incident.id,
    )
    if plan.chosen_by == DISPATCHER_CHOICE:
        log_event(
            state,
            "override",
            f"RESQ would have sent {recommended.ambulance_id} to {state.hospitals[recommended.hospital_id].name}. "
            f"This choice costs {_extra(plan.total_minutes - recommended.total_minutes)} of driving.",
            incident.id,
        )
    for reason in plan.reasons:
        log_event(state, "reason", reason, incident.id)

    if ambulance.location == incident.location:
        _arrive_at_scene(state, ambulance)
    return incident


def advance_time(state: SimulationState, minutes: int) -> None:
    """Advance the clock one minute at a time."""
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_ADVANCE_MINUTES:
        raise InvalidAction(f"Time can advance by 1 to {MAX_ADVANCE_MINUTES} minutes.")
    for _ in range(minutes):
        _tick(state)


def _tick(state: SimulationState) -> None:
    state.minute += 1
    for ambulance in state.sorted_ambulances():
        if ambulance.status == HANDOVER:
            _complete_handover(state, ambulance)
        elif ambulance.status in MOVING_STATUSES:
            _move_one_minute(state, ambulance)


def target_node(state: SimulationState, ambulance: Ambulance) -> str | None:
    """Where the ambulance is heading for its current phase."""
    if ambulance.status == TO_SCENE:
        return state.incidents[ambulance.incident_id].location
    if ambulance.status == TO_HOSPITAL:
        return state.hospitals[ambulance.hospital_id].location
    return None


def _move_one_minute(state: SimulationState, ambulance: Ambulance) -> None:
    if not ambulance.on_road:
        if ambulance.is_blocked:
            return
        path = route_tree(state, target_node(state, ambulance)).path_from(ambulance.location)
        if path is None:
            _refresh_at_node(state, ambulance)
            return
        if len(path) == 1:
            _arrive_at_node(state, ambulance)
            return
        road = routing.find_road(state.roads.values(), path[0], path[1])
        ambulance.edge_to = path[1]
        ambulance.edge_road = road.id
        ambulance.edge_elapsed = 0

    ambulance.edge_elapsed += 1
    road = state.roads[ambulance.edge_road]
    if ambulance.edge_elapsed >= road.minutes:

        ambulance.location = ambulance.edge_to
        ambulance.edge_to = None
        ambulance.edge_road = None
        ambulance.edge_elapsed = 0
        _arrive_at_node(state, ambulance)


def _arrive_at_node(state: SimulationState, ambulance: Ambulance) -> None:
    if ambulance.location != target_node(state, ambulance):
        _refresh_at_node(state, ambulance)
    elif ambulance.status == TO_SCENE:
        _arrive_at_scene(state, ambulance)
    else:
        _arrive_at_hospital(state, ambulance)


def _arrive_at_scene(state: SimulationState, ambulance: Ambulance) -> None:
    incident = state.incidents[ambulance.incident_id]
    incident.pickup_minute = state.minute
    ambulance.status = TO_HOSPITAL
    log_event(
        state,
        "pickup",
        f"{ambulance.id} picked up {patients_text(incident.patients)} at {state.place_name(incident.location)}.",
        incident.id,
    )
    if ambulance.location == state.hospitals[ambulance.hospital_id].location:
        _arrive_at_hospital(state, ambulance)
    else:
        _refresh_at_node(state, ambulance)


def _arrive_at_hospital(state: SimulationState, ambulance: Ambulance) -> None:
    incident = state.incidents[ambulance.incident_id]
    hospital = state.hospitals[ambulance.hospital_id]
    incident.arrival_minute = state.minute
    ambulance.status = HANDOVER
    log_event(state, "arrival", f"{ambulance.id} arrived at {hospital.name}; handover takes 1 minute.", incident.id)


def _complete_handover(state: SimulationState, ambulance: Ambulance) -> None:
    incident = state.incidents[ambulance.incident_id]
    hospital = state.hospitals[ambulance.hospital_id]
    hospital.reserved -= incident.patients
    hospital.occupied += incident.patients
    incident.status = COMPLETED
    incident.completed_minute = state.minute
    ambulance.status = AVAILABLE
    ambulance.incident_id = None
    ambulance.hospital_id = None
    ambulance.blocked_reason = None
    log_event(
        state,
        "handover",
        f"Handover complete at {hospital.name}: reserved beds became occupied. "
        f"{ambulance.id} is available at {state.place_name(ambulance.location)}.",
        incident.id,
    )


def _refresh_at_node(state: SimulationState, ambulance: Ambulance) -> None:
    """Re-check a moving crew that is standing at a junction."""
    if ambulance.status not in MOVING_STATUSES or ambulance.on_road:
        return
    incident = state.incidents[ambulance.incident_id]
    here = state.place_name(ambulance.location)
    target = target_node(state, ambulance)

    if route_tree(state, target).time_from(ambulance.location) is not None:
        if ambulance.is_blocked:
            ambulance.blocked_reason = None
            log_event(state, "recovered", f"{ambulance.id} has an open route again and continues to {state.place_name(target)}.", incident.id)
        return

    if ambulance.status == TO_HOSPITAL:
        old = state.hospitals[ambulance.hospital_id]
        facility = REQUIRED_FACILITY[incident.category]
        here_tree = route_tree(state, ambulance.location)
        choices, _ = _hospital_choices(state, facility, incident.patients, here_tree, exclude_id=old.id)
        if choices:
            minutes, new = choices[0]
            old.reserved -= incident.patients
            new.reserved += incident.patients
            ambulance.hospital_id = new.id
            incident.hospital_id = new.id
            ambulance.blocked_reason = None
            log_event(
                state,
                "transfer",
                f"{old.name} can no longer be reached from {here}. The {beds(incident.patients)} reservation "
                f"moved to {new.name} ({minutes} min away).",
                incident.id,
            )
            if ambulance.location == new.location:
                _arrive_at_hospital(state, ambulance)
            return
        reason = (
            f"No open road from {here} to {old.name}, and no other hospital with a {facility} unit and "
            f"{beds(incident.patients)} free can be reached. The reservation at {old.name} is kept."
        )
    else:
        reason = f"No open road from {here} to the scene at {state.place_name(target)}."

    if ambulance.blocked_reason != reason:
        ambulance.blocked_reason = reason
        log_event(state, "blocked", f"{ambulance.id} is held at {here}. {reason}", incident.id)


def _refresh_stationary_crews(state: SimulationState) -> None:
    for ambulance in state.sorted_ambulances():
        if ambulance.status in MOVING_STATUSES and not ambulance.on_road:
            _refresh_at_node(state, ambulance)


def remaining_route(state: SimulationState, ambulance: Ambulance) -> list[str] | None:
    """Places still to visit, starting from the next junction (read-only)."""
    target = target_node(state, ambulance)
    if target is None or ambulance.is_blocked:
        return None
    start = ambulance.edge_to if ambulance.on_road else ambulance.location
    return route_tree(state, target).path_from(start)


def minutes_remaining(state: SimulationState, ambulance: Ambulance) -> int | None:
    """Estimated minutes until the crew reaches its current target."""
    path = remaining_route(state, ambulance)
    if path is None:
        return None
    total = routing.path_minutes(state.roads.values(), path) or 0
    if ambulance.on_road:
        total += state.roads[ambulance.edge_road].minutes - ambulance.edge_elapsed
    return total


def _uses_road(path, road) -> bool:
    return any(road.joins(here, there) for here, there in zip(path, path[1:]))


def close_road(state: SimulationState, road_id: str) -> None:
    """Close a road."""
    road = state.roads.get(road_id)
    if road is None:
        raise InvalidAction("Unknown road.")
    if not road.open:
        raise InvalidAction(f"{state.road_label(road)} is already closed.")
    moving = [a for a in state.sorted_ambulances() if a.status in MOVING_STATUSES]
    before = {ambulance.id: remaining_route(state, ambulance) for ambulance in moving}

    road.open = False
    log_event(state, "road", f"Road closed: {state.road_label(road)}.")
    _refresh_stationary_crews(state)

    for ambulance in moving:
        old_route = before[ambulance.id]
        affected = (old_route is not None and _uses_road(old_route, road)) or ambulance.edge_road == road.id
        if not affected or ambulance.status not in MOVING_STATUSES:
            continue
        if ambulance.on_road:
            current = state.roads[ambulance.edge_road]
            note = (f"{ambulance.id} is already on {state.road_label(current)}; it will finish that road "
                    f"and recalculate at {state.place_name(ambulance.edge_to)}.")
        else:
            note = f"{ambulance.id} recalculated at {state.place_name(ambulance.location)}."
        new_route = remaining_route(state, ambulance)
        if new_route is None:
            note += " There is no open route onwards yet."
        else:
            minutes = routing.path_minutes(state.roads.values(), new_route)
            note += f" New route from there: {route_text(state, new_route)} ({minutes} min)."
        log_event(state, "reroute", note, ambulance.incident_id)


def reopen_road(state: SimulationState, road_id: str) -> None:
    road = state.roads.get(road_id)
    if road is None:
        raise InvalidAction("Unknown road.")
    if road.open:
        raise InvalidAction(f"{state.road_label(road)} is already open.")
    road.open = True
    log_event(state, "road", f"Road reopened: {state.road_label(road)}.")
    _refresh_stationary_crews(state)


def discharge_patients(state: SimulationState, hospital_id: str, count: int) -> None:
    """Free occupied beds."""
    hospital: Hospital | None = state.hospitals.get(hospital_id)
    if hospital is None:
        raise InvalidAction("Unknown hospital.")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise InvalidAction("Discharge at least one patient.")
    if count > hospital.occupied:
        raise InvalidAction(
            f"{hospital.name} has only {beds(hospital.occupied)} occupied. "
            "Reserved beds belong to crews on their way and cannot be discharged."
        )
    hospital.occupied -= count
    log_event(state, "discharge", f"{hospital.name} discharged {patients_text(count)}; {beds(hospital.free)} now free.")
    _refresh_stationary_crews(state)


def check_invariants(state: SimulationState) -> list[str]:
    """Return a list of broken rules (empty means the state is consistent)."""
    problems: list[str] = []
    add = problems.append

    if not isinstance(state.minute, int) or state.minute < 0:
        add("The clock must be a non-negative whole number.")

    seen_pairs = set()
    for road in state.roads.values():
        if road.a not in state.locations or road.b not in state.locations:
            add(f"Road {road.id} uses an unknown place.")
        if road.a == road.b:
            add(f"Road {road.id} starts and ends at the same place.")
        if not isinstance(road.minutes, int) or isinstance(road.minutes, bool) or road.minutes < 1:
            add(f"Road {road.id} must take a positive whole number of minutes.")
        pair = frozenset((road.a, road.b))
        if pair in seen_pairs:
            add(f"Road {road.id} duplicates another road.")
        seen_pairs.add(pair)

    expected_reserved = {hospital_id: 0 for hospital_id in state.hospitals}
    serving: dict[int, str] = {}
    for ambulance in state.sorted_ambulances():
        if ambulance.location not in state.locations:
            add(f"{ambulance.id} is at an unknown place.")
        if ambulance.base not in state.locations:
            add(f"{ambulance.id} has an unknown base.")
        if not isinstance(ambulance.capacity, int) or ambulance.capacity < 1:
            add(f"{ambulance.id} needs a capacity of at least 1.")
        if ambulance.status not in AMBULANCE_STATUSES:
            add(f"{ambulance.id} has an unknown status.")
            continue
        if ambulance.status == AVAILABLE:
            if (ambulance.incident_id, ambulance.hospital_id, ambulance.edge_to, ambulance.edge_road,
                    ambulance.blocked_reason) != (None, None, None, None, None) or ambulance.edge_elapsed != 0:
                add(f"{ambulance.id} is available but still holds mission details.")
            continue

        incident = state.incidents.get(ambulance.incident_id)
        hospital = state.hospitals.get(ambulance.hospital_id)
        if incident is None or hospital is None:
            add(f"{ambulance.id} refers to a missing incident or hospital.")
            continue
        if incident.id in serving:
            add(f"Incident #{incident.id} is assigned to two ambulances.")
        serving[incident.id] = ambulance.id
        if incident.status != ASSIGNED or incident.ambulance_id != ambulance.id or incident.hospital_id != hospital.id:
            add(f"{ambulance.id} and incident #{incident.id} disagree about the mission.")
        if incident.patients > ambulance.capacity:
            add(f"{ambulance.id} is carrying more patients than it can hold.")
        if REQUIRED_FACILITY.get(incident.category) not in hospital.facilities:
            add(f"Incident #{incident.id} is going to a hospital without the right facility.")
        expected_reserved[hospital.id] += incident.patients

        if ambulance.on_road:
            road = state.roads.get(ambulance.edge_road)
            if road is None or not road.joins(ambulance.location, ambulance.edge_to):
                add(f"{ambulance.id} is on a road that does not match its position.")
            elif not 1 <= ambulance.edge_elapsed < road.minutes:
                add(f"{ambulance.id} has an impossible amount of progress on its road.")
            if ambulance.is_blocked:
                add(f"{ambulance.id} cannot be blocked while driving along a road.")
        elif ambulance.edge_road is not None or ambulance.edge_elapsed != 0:
            add(f"{ambulance.id} has road progress but is not on a road.")

        if ambulance.status == TO_SCENE and incident.pickup_minute is not None:
            add(f"{ambulance.id} is still driving to a scene it has already left.")
        if ambulance.status in (TO_HOSPITAL, HANDOVER) and incident.pickup_minute is None:
            add(f"{ambulance.id} is carrying patients that were never picked up.")
        if ambulance.status in (TO_SCENE, TO_HOSPITAL) and incident.arrival_minute is not None:
            add(f"{ambulance.id} is still on its way, but incident #{incident.id} says it reached the hospital.")
        if ambulance.status == HANDOVER and (
            ambulance.on_road or ambulance.location != hospital.location or incident.arrival_minute is None
        ):
            add(f"{ambulance.id} is handing over away from its hospital.")

    for incident in state.incidents.values():
        tag = f"Incident #{incident.id}"
        if not isinstance(incident.id, int) or incident.id < 1 or incident.id >= state.next_incident_id:
            add(f"{tag} has an invalid number.")
        if incident.category not in CATEGORIES or incident.priority not in PRIORITIES:
            add(f"{tag} has an unknown category or priority.")
        if incident.location not in state.locations:
            add(f"{tag} is at an unknown place.")
        if not isinstance(incident.patients, int) or not MIN_PATIENTS <= incident.patients <= MAX_PATIENTS:
            add(f"{tag} must have {MIN_PATIENTS}-{MAX_PATIENTS} patients.")
        if incident.status not in INCIDENT_STATUSES or incident.source not in INCIDENT_SOURCES:
            add(f"{tag} has an unknown status or source.")
            continue
        if not 0 <= incident.reported_minute <= state.minute:
            add(f"{tag} was reported at an impossible time.")
        milestones = [incident.reported_minute, incident.dispatched_minute, incident.pickup_minute,
                      incident.arrival_minute, incident.completed_minute]
        reached = [value for value in milestones if value is not None]
        if reached != sorted(reached) or any(value > state.minute for value in reached):
            add(f"{tag} has a timeline that goes backwards.")
        if incident.chosen_by is not None and incident.chosen_by not in DECISION_SOURCES:
            add(f"{tag} has an unknown decision source.")
        if incident.status == WAITING:
            if incident.ambulance_id is not None or incident.hospital_id is not None or incident.dispatched_minute is not None:
                add(f"{tag} is waiting but already has a crew.")
            if incident.chosen_by is not None or incident.planned_minutes is not None:
                add(f"{tag} is waiting but already has a decision recorded.")

            if any(value is not None for value in milestones[2:]):
                add(f"{tag} is waiting but already has a pickup, arrival or completion time.")
        elif incident.status == ASSIGNED:
            if incident.id not in serving:
                add(f"{tag} is assigned but no ambulance is serving it.")
            if incident.dispatched_minute is None or incident.completed_minute is not None:
                add(f"{tag} is assigned but its dispatch or completion time does not fit.")
        else:
            if incident.id in serving or any(value is None for value in milestones):
                add(f"{tag} is completed but its record is incomplete.")
            if incident.hospital_id not in state.hospitals or incident.ambulance_id not in state.ambulances:
                add(f"{tag} refers to a missing ambulance or hospital.")

    for hospital in state.hospitals.values():
        if hospital.location not in state.locations:
            add(f"{hospital.name} is at an unknown place.")
        if not hospital.facilities or any(facility not in FACILITIES for facility in hospital.facilities):
            add(f"{hospital.name} has unknown facilities.")
        if hospital.occupied < 0 or hospital.reserved < 0:
            add(f"{hospital.name} has a negative bed count.")
        if hospital.occupied + hospital.reserved > hospital.capacity:
            add(f"{hospital.name} has more beds in use than it has.")
        if hospital.reserved != expected_reserved.get(hospital.id, 0):
            add(f"{hospital.name} reservations do not match the crews heading there.")

    for event in state.events:
        if not 0 <= event.minute <= state.minute:
            add("The shift log contains an entry from an impossible time.")
            break
    return problems
