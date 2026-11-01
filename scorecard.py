"""Summarise incident response times and dispatch decisions."""

from dataclasses import dataclass

from models import ASSIGNED, COMPLETED, DISPATCHER_CHOICE, PRIORITIES, WAITING, SimulationState


RESPONSE_TARGETS = {"critical": 8, "high": 12, "moderate": 20}


@dataclass(frozen=True)
class CaseScore:
    """One incident's row on the scorecard."""

    incident_id: int
    priority: str
    category: str
    place: str
    status: str
    target: int
    waited: int
    reached: int | None
    handed_over: int | None
    met_target: bool | None
    decision: str | None
    extra_minutes: int | None


@dataclass(frozen=True)
class Scorecard:
    minute: int
    cases: tuple[CaseScore, ...]
    targets_met: int
    targets_judged: int
    average_reach: float | None
    longest_wait: CaseScore | None
    resq_picks: int
    own_choices: int
    extra_minutes: int
    reroutes: int
    holds: int
    transfers: int

    @property
    def waiting_past_target(self) -> list[CaseScore]:
        return [case for case in self.cases if case.status == WAITING and case.met_target is False]

    def headline(self) -> str:
        """One plain sentence summing up the shift so far."""
        if not self.cases:
            return "No calls yet. Answer a call to start the shift."
        parts = []
        if self.targets_judged:
            parts.append(f"{self.targets_met} of {self.targets_judged} incidents reached within the fictional response target")
        else:
            parts.append("No incidents reached yet")
        late = len(self.waiting_past_target)
        if late:
            parts.append(f"{late} waiting call{'s' if late != 1 else ''} already past target")
        if self.own_choices:
            parts.append(f"{self.own_choices} choice{'s' if self.own_choices != 1 else ''} of your own "
                         f"({'no extra time' if self.extra_minutes == 0 else f'+{self.extra_minutes} min driving'})")
        return "; ".join(parts) + "."


def _case_score(state: SimulationState, incident) -> CaseScore:
    target = RESPONSE_TARGETS[incident.priority]
    if incident.dispatched_minute is not None:
        waited = incident.dispatched_minute - incident.reported_minute
    else:
        waited = state.minute - incident.reported_minute
    reached = None if incident.pickup_minute is None else incident.pickup_minute - incident.reported_minute
    handed_over = None if incident.completed_minute is None else incident.completed_minute - incident.reported_minute
    if reached is not None:
        met = reached <= target
    elif state.minute - incident.reported_minute > target:
        met = False
    else:
        met = None
    extra = None
    if incident.planned_minutes is not None and incident.recommended_minutes is not None:
        extra = incident.planned_minutes - incident.recommended_minutes
    return CaseScore(
        incident_id=incident.id,
        priority=incident.priority,
        category=incident.category,
        place=state.place_name(incident.location),
        status=incident.status,
        target=target,
        waited=waited,
        reached=reached,
        handed_over=handed_over,
        met_target=met,
        decision=incident.chosen_by,
        extra_minutes=extra,
    )


def build_scorecard(state: SimulationState) -> Scorecard:
    """Summarise the shift so far, most urgent cases first."""
    order = {priority: rank for rank, priority in enumerate(PRIORITIES)}
    incidents = sorted(state.incidents.values(), key=lambda i: (order[i.priority], i.reported_minute, i.id))
    cases = tuple(_case_score(state, incident) for incident in incidents)

    judged = [case for case in cases if case.met_target is not None]
    reached = [case.reached for case in cases if case.reached is not None]
    dispatched = [case for case in cases if case.status in (ASSIGNED, COMPLETED) and case.decision is not None]
    own = [case for case in dispatched if case.decision == DISPATCHER_CHOICE]
    kinds = [event.kind for event in state.events]
    return Scorecard(
        minute=state.minute,
        cases=cases,
        targets_met=sum(1 for case in judged if case.met_target),
        targets_judged=len(judged),
        average_reach=round(sum(reached) / len(reached), 1) if reached else None,
        longest_wait=max(cases, key=lambda case: case.waited) if cases else None,
        resq_picks=len(dispatched) - len(own),
        own_choices=len(own),
        extra_minutes=sum(case.extra_minutes or 0 for case in own),
        reroutes=kinds.count("reroute"),
        holds=kinds.count("blocked"),
        transfers=kinds.count("transfer"),
    )


def summary_lines(state: SimulationState) -> list[str]:
    """The scorecard as short text lines (used by the --demo mode)."""
    card = build_scorecard(state)
    lines = [card.headline()]
    if card.average_reach is not None:
        lines.append(f"Average time from call to pickup: {card.average_reach} min per incident.")
    lines.append(f"Decisions: {card.resq_picks} RESQ pick(s), {card.own_choices} of your own.")
    lines.append(f"Disruptions handled: {card.reroutes} reroute(s), {card.holds} crew(s) held, "
                 f"{card.transfers} booking(s) moved.")
    for case in card.cases:
        reached = "not yet reached" if case.reached is None else f"reached after {case.reached} min"
        verdict = {True: "within target", False: "missed target", None: "pending"}[case.met_target]
        lines.append(f"#{case.incident_id} {case.priority}: {reached} (target {case.target}), {verdict}.")
    return lines
