"""Build the application pages from simulation state."""

from dataclasses import dataclass

import simulation
from .html_tools import Markup, document, tag
from intake import (
    AMBULANCE_STATUS_LABELS,
    CATEGORY_LABELS,
    FACILITY_LABELS,
    INCIDENT_STATUS_LABELS,
    MAX_MESSAGE_LENGTH,
    MAX_NAME_LENGTH,
    PRIORITY_LABELS,
    SOURCE_LABELS,
    CallDeck,
    label,
)
from application import Review
from .map_view import render_map
from scorecard import build_scorecard
from models import (
    ASSIGNED,
    CATEGORIES,
    DISPATCHER_CHOICE,
    HANDOVER,
    MAX_PATIENTS,
    MIN_PATIENTS,
    PRIORITIES,
    TO_SCENE,
    Ambulance,
    DispatchPlan,
    Incident,
    Infeasibility,
    Recommendation,
    SimulationState,
)
from storage import MAX_LABEL_LENGTH, CheckpointInfo

NAV = (
    ("desk", "/desk", "Dispatch desk"),
    ("resources", "/resources", "Resources"),
    ("archive", "/archive", "Case archive"),
    ("shifts", "/shifts", "Saved shifts"),
)

TIMELINE_STEPS = (
    ("Reported", "reported_minute"),
    ("Crew confirmed", "dispatched_minute"),
    ("Patients picked up", "pickup_minute"),
    ("Arrived at hospital", "arrival_minute"),
    ("Handover complete", "completed_minute"),
)


@dataclass(frozen=True)
class ViewContext:
    """What every page needs: the shift snapshot, form token and any notice."""

    state: SimulationState
    deck: CallDeck
    revision: int
    token: str
    notice: tuple[str, str] | None = None


def _hidden(name: str, value) -> Markup:
    return tag("input", type="hidden", name=name, value=value)


def _form(ctx: ViewContext, url: str, /, *content, css: str = "inline-form", **fields) -> Markup:
    """A POST form carrying the security token, the page revision and extra hidden fields (positional-only `url`, so a field may be calle."""
    hidden = [_hidden("token", ctx.token), _hidden("revision", ctx.revision)]
    hidden += [_hidden(name, value) for name, value in fields.items()]
    return tag("form", hidden, *content, method="post", action=url, class_=css)


def _button(text: str, css: str = "btn") -> Markup:
    return tag("button", text, type="submit", class_=css)


def _panel(title: str, *content, panel_id: str, css: str = "", subtitle: str | None = None) -> Markup:
    heading = f"{panel_id}-title"
    head = tag("div", tag("h2", title, id=heading), tag("p", subtitle, class_="panel-sub") if subtitle else None,
               class_="panel-head")
    return tag("section", head, *content, class_=f"panel {css}".strip(), id=panel_id, aria_labelledby=heading)


def _badge(text: str, kind: str) -> Markup:
    return tag("span", text, class_=f"badge badge-{kind}")


def _priority(priority: str) -> Markup:
    return _badge(label(PRIORITY_LABELS, priority), priority)


def _fact(name: str, value) -> Markup:
    return tag("div", tag("dt", name), tag("dd", value), class_="fact")


def _table(caption: str, headers, rows) -> Markup:
    return tag(
        "div",
        tag("table", tag("caption", caption), tag("thead", tag("tr", [tag("th", h, scope="col") for h in headers])),
            tag("tbody", rows)),
        class_="table-wrap",
    )


def _patients(count: int) -> str:
    return simulation.patients_text(count)


def position_text(state: SimulationState, ambulance: Ambulance) -> str:
    """'at Dockyards' or 'between Market Square and Civic Junction (1 of 2 min)'."""
    if ambulance.on_road:
        road = state.roads[ambulance.edge_road]
        return (f"between {state.place_name(ambulance.location)} and {state.place_name(ambulance.edge_to)} "
                f"({ambulance.edge_elapsed} of {road.minutes} min)")
    return f"at {state.place_name(ambulance.location)}"


def _timeline(incident: Incident) -> Markup:
    steps = []
    for text, attribute in TIMELINE_STEPS:
        minute = getattr(incident, attribute)
        steps.append(tag(
            "li",
            tag("span", text, class_="step-name"),
            tag("span", f"min {minute}" if minute is not None else "pending", class_="step-time"),
            class_="step step-done" if minute is not None else "step",
        ))
    return tag("ol", steps, class_="timeline")


def _log_list(events) -> Markup:
    return tag("ol", [
        tag("li", tag("span", f"min {event.minute}", class_="log-time"), tag("span", event.text, class_="log-text"),
            class_=f"log-entry log-{event.kind}")
        for event in events
    ], class_="log")


def _stat(name: str, value, css: str = "") -> Markup:
    return tag("div", tag("dt", name), tag("dd", value), class_=f"stat {css}".strip())


def _notice(notice: tuple[str, str] | None) -> Markup | None:
    if not notice:
        return None
    kind, text = notice
    return tag("div", tag("p", text), class_=f"notice notice-{kind}", role="alert" if kind == "error" else "status")


def _layout(ctx: ViewContext, active: str, title: str, *content) -> Markup:
    state = ctx.state
    header = tag(
        "header",
        tag("a", tag("span", "RESQ", class_="brand-mark"),
            tag("span", "Harbour District dispatch simulator", class_="brand-sub"), href="/desk", class_="brand"),
        tag("dl",
            _stat("Clock", f"min {state.minute}", "stat-clock"),
            _stat("Waiting", len(simulation.waiting_queue(state))),
            _stat("Active", len(simulation.active_incidents(state))),
            _stat("Completed", len(simulation.completed_incidents(state))),
            class_="stats"),
        class_="topbar",
    )
    nav = tag("nav", tag("ul", [
        tag("li", tag("a", text, href=href, class_="nav-link", aria_current="page" if key == active else None))
        for key, href, text in NAV
    ]), class_="nav", aria_label="Main sections")
    footer = tag(
        "footer",
        tag("p", "RESQ is a local emergency-response coordination prototype using a simulated environment. It is not emergency-service software and gives no "
                 "medical advice. In a real emergency in Australia, call 000."),
        tag("p", "This shift lives in the running Python program: refreshing or switching pages keeps it, "
                 "but stopping Python discards anything not saved under Saved shifts."),
        class_="footer",
    )
    return document(
        f"{title} · RESQ",
        tag("a", "Skip to main content", href="#main", class_="skip-link"),
        header,
        nav,
        tag("main", _notice(ctx.notice), tag("h1", title, class_="page-title"), *content, id="main"),
        footer,
    )


def _last_call(state: SimulationState) -> Incident | None:
    calls = [incident for incident in state.incidents.values() if incident.source == "call"]
    return max(calls, key=lambda incident: incident.id) if calls else None


def _call_card(state: SimulationState, incident: Incident) -> Markup:
    return tag(
        "article",
        tag("p", f"Incident #{incident.id} · {label(INCIDENT_STATUS_LABELS, incident.status)}", class_="call-meta"),
        tag("blockquote",
            tag("p", incident.caller_message or "No message recorded."),
            tag("footer", "— ", incident.caller_name, f", minute {incident.reported_minute}")),
        tag("ul",
            tag("li", _priority(incident.priority)),
            tag("li", label(CATEGORY_LABELS, incident.category)),
            tag("li", _patients(incident.patients)),
            tag("li", state.place_name(incident.location)),
            class_="chips"),
        class_="call-card",
        aria_label="Last answered call",
    )


def _call_panel(ctx: ViewContext) -> Markup:
    upcoming = ctx.deck.peek()
    last = _last_call(ctx.state)
    return _panel(
        "Incoming call",
        tag("p", "Next on the line: ", tag("strong", upcoming.caller_name),
            tag("span", f" · call {ctx.deck.next_index + 1} of {len(ctx.deck)}", class_="muted"), class_="next-caller"),
        _form(ctx, "/call/receive", _button("Answer call", "btn btn-primary")),
        _call_card(ctx.state, last) if last else None,
        panel_id="call",
        css="call-panel",
        subtitle="Scripted, fictional callers",
    )


def _manual_panel(ctx: ViewContext, errors: dict, values: dict) -> Markup:
    state = ctx.state

    def error(name):
        return tag("p", errors[name], class_="field-error", id=f"error-{name}") if name in errors else None

    def input_state(name):
        return {"aria_invalid": "true" if name in errors else None,
                "aria_describedby": f"error-{name}" if name in errors else None}

    def select(name, text, options, chosen):
        choices = [tag("option", option_text, value=value, selected=(value == chosen)) for value, option_text in options]
        return tag("div", tag("label", text, for_=f"field-{name}"),
                   tag("select", choices, name=name, id=f"field-{name}", **input_state(name)), error(name),
                   class_="field")

    places = sorted(state.locations.values(), key=lambda place: place.name)
    form = _form(
        ctx,
        "/incident/report",
        tag("div",
            select("category", "Category", [(c, label(CATEGORY_LABELS, c)) for c in CATEGORIES],
                   values.get("category", "medical")),
            select("priority", "Priority", [(p, label(PRIORITY_LABELS, p)) for p in PRIORITIES],
                   values.get("priority", "moderate")),
            select("location", "Location", [(place.id, place.name) for place in places],
                   values.get("location", places[0].id)),
            select("patients", "Patients", [(str(n), str(n)) for n in range(MIN_PATIENTS, MAX_PATIENTS + 1)],
                   values.get("patients", "1")),
            class_="field-grid"),
        tag("div", tag("label", "Caller name", for_="field-caller_name"),
            tag("input", type="text", name="caller_name", id="field-caller_name", maxlength=MAX_NAME_LENGTH,
                value=values.get("caller_name", ""), autocomplete="off", **input_state("caller_name")),
            error("caller_name"), class_="field"),
        tag("div", tag("label", "What the caller said", for_="field-caller_message"),
            tag("textarea", values.get("caller_message", ""), name="caller_message", id="field-caller_message",
                maxlength=MAX_MESSAGE_LENGTH, rows=3, **input_state("caller_message")),
            error("caller_message"), class_="field"),
        tag("p", "You choose the category and priority. RESQ never diagnoses.", class_="hint"),
        _button("Add to queue"),
        css="manual-form",
    )
    return tag("details", tag("summary", "Report an incident manually"), form,
               class_="panel manual-panel", open=bool(errors))


def _queue_panel(ctx: ViewContext, recommendation: Recommendation) -> Markup:
    state = ctx.state
    queue = simulation.waiting_queue(state)
    next_id = recommendation.plan.incident_id if recommendation.plan else None
    if not queue:
        body = tag("p", "No calls are waiting. Answer the next call or report one manually.", class_="empty")
    else:
        items = []
        for position, incident in enumerate(queue, start=1):
            result = simulation.plan_for_incident(state, incident)
            held = result.reasons[0] if isinstance(result, Infeasibility) else None
            items.append(tag(
                "li",
                tag("div",
                    tag("span", position, class_="queue-rank"),
                    _priority(incident.priority),
                    tag("strong", f"#{incident.id} {label(CATEGORY_LABELS, incident.category)}"),
                    _badge("Next to dispatch", "next") if incident.id == next_id else None,
                    class_="queue-line"),
                tag("p", f"{state.place_name(incident.location)} · {_patients(incident.patients)} · "
                         f"reported min {incident.reported_minute}", class_="queue-detail"),
                tag("p", tag("strong", "Held: "), held, class_="queue-held") if held else None,
                class_=f"queue-item queue-{incident.priority}",
            ))
        body = tag("ol", items, class_="queue")
    return _panel(
        "Priority queue",
        body,
        tag("p", "Order: Critical → High → Moderate, then earliest report, then incident number.", class_="hint"),
        panel_id="queue",
        subtitle=f"{len(queue)} waiting",
    )


def _extra_text(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    return "no extra time" if minutes == 0 else f"+{minutes} min"


def _plan_card(ctx: ViewContext, plan: DispatchPlan, fields: dict, recommended: DispatchPlan) -> Markup:
    state = ctx.state
    incident = state.incidents[plan.incident_id]
    ambulance = state.ambulances[plan.ambulance_id]
    hospital = state.hospitals[plan.hospital_id]
    scene = state.place_name(incident.location)
    own = plan.chosen_by == DISPATCHER_CHOICE
    facts = tag(
        "dl",
        _fact("Incident", tag("span", _priority(incident.priority),
                              f" #{incident.id} {label(CATEGORY_LABELS, incident.category)} at {scene}, "
                              f"{_patients(incident.patients)}")),
        _fact("Ambulance", f"{ambulance.id} · {ambulance.name} (room for {ambulance.capacity})"),
        _fact("Hospital", f"{hospital.name} · " + ", ".join(label(FACILITY_LABELS, f) for f in hospital.facilities)),
        _fact("To scene", f"{plan.minutes_to_scene} min · {simulation.route_text(state, plan.route_to_scene)}"),
        _fact("To hospital", f"{plan.minutes_to_hospital} min · {simulation.route_text(state, plan.route_to_hospital)}"),
        _fact("Beds", f"{hospital.free} free now → {hospital.free - incident.patients} after reserving "
                      f"{incident.patients}"),
        _fact("Handover", f"expected at minute {state.minute + plan.total_minutes + 1} if no road changes"),
        _fact("Versus RESQ", f"{_extra_text(plan.total_minutes - recommended.total_minutes)} compared with "
                             f"{recommended.ambulance_id} + {state.hospitals[recommended.hospital_id].name}")
        if own else None,
        class_="facts",
    )
    confirm = tag(
        "form",
        _hidden("token", ctx.token),
        [_hidden(name, value) for name, value in fields.items()],
        _button("Confirm my choice & send crew" if own else "Confirm & send crew", "btn btn-confirm"),
        tag("a", "Back to RESQ's pick", href="/desk?review=1#review", class_="back-link") if own else None,
        method="post",
        action="/dispatch/confirm",
        class_="confirm-form",
    )
    reasons = [tag("li", reason, class_="reason-warn" if reason.startswith("Heads-up") else None)
               for reason in plan.reasons]
    return tag(
        "article",
        tag("p", _badge("Your choice", "choice") if own else _badge("RESQ's pick", "next"), " Send ",
            tag("strong", ambulance.id), " to ", tag("strong", scene), ", then ",
            tag("strong", hospital.name), ".", class_="plan-headline"),
        facts,
        tag("h3", "What your choice means" if own else "Why this plan"),
        tag("ol", reasons, class_="reasons"),
        confirm,
        class_="plan-card plan-own" if own else "plan-card",
    )


def _choice_form(ctx: ViewContext, review: Review) -> Markup | None:
    """A read-only GET form: pick any possible crew and hospital, then preview."""
    options, plan = review.options, review.plan
    recommended = review.recommendation.plan
    if options is None or plan is None or recommended is None:
        return None
    if len(options.ambulances) == 1 and len(options.hospitals) == 1:
        return tag("p", "There is no other possible crew or hospital for this call right now.", class_="hint")

    def choices(name, entries, describe, picked, resq_pick):
        items = []
        for minutes, item in entries:
            radio_id = f"pick-{name}-{item.id}"
            items.append(tag(
                "div",
                tag("input", type="radio", name=name, value=item.id, id=radio_id, checked=(item.id == picked)),
                tag("label", describe(minutes, item), " ",
                    _badge("RESQ's pick", "next") if item.id == resq_pick else None, for_=radio_id),
                class_="choice",
            ))
        return items

    def not_possible(notes):
        return tag("p", "Not possible: " + " ".join(notes), class_="choice-excluded") if notes else None

    return tag(
        "form",
        tag("input", type="hidden", name="review", value="1"),
        tag("h3", "Choose differently"),
        tag("p", "You decide how to serve this call: any free crew with room, and any reachable hospital with the "
                 "right unit and free beds. Queue order still decides which call goes next. Previewing changes "
                 "nothing.", class_="hint"),
        tag("div",
            tag("fieldset", tag("legend", "Ambulance"),
                choices("ambulance", options.ambulances,
                        lambda m, a: [tag("strong", a.id), f" · {a.name} · {m} min to the scene"],
                        plan.ambulance_id, recommended.ambulance_id),
                not_possible(options.ambulance_excluded)),
            tag("fieldset", tag("legend", "Hospital"),
                choices("hospital", options.hospitals,
                        lambda m, h: [tag("strong", h.name), f" · {m} min · {h.free} free"],
                        plan.hospital_id, recommended.hospital_id),
                not_possible(options.hospital_excluded)),
            class_="choice-grid"),
        _button("Preview this choice"),
        method="get",
        action="/desk#review",
        class_="choice-form",
    )


def _review_panel(ctx: ViewContext, review: Review, reviewing: bool, fields: dict | None) -> Markup:
    queue_empty = not simulation.waiting_queue(ctx.state)
    if not reviewing:
        action = (tag("p", "No calls are waiting, so there is nothing to review.", class_="empty") if queue_empty
                  else tag("a", "Review next dispatch", href="/desk?review=1#review", class_="btn btn-primary"))
        return _panel(
            "Next dispatch",
            tag("p", "Reviewing shows RESQ's pick, its reasons and every other option. Nothing changes until you "
                     "confirm.", class_="hint"),
            action,
            panel_id="review",
            css="review-panel",
        )
    recommendation = review.recommendation
    content = []
    if review.problem:
        content.append(tag("p", review.problem, class_="choice-problem", role="alert"))
    if review.plan is not None and fields is not None:
        content.append(_plan_card(ctx, review.plan, fields, recommendation.plan))
        content.append(_choice_form(ctx, review))
    else:
        content.append(tag("p", "No calls are waiting." if queue_empty else "Nothing can be dispatched right now.",
                           class_="empty"))
    if recommendation.held:
        content.append(tag("h3", "Held back: higher priority, but cannot be served yet"))
        content.append(tag("ul", [
            tag("li", tag("strong", f"#{held.incident_id}: "), " ".join(held.reasons)) for held in recommendation.held
        ], class_="held-list"))
    content.append(tag("p", tag("a", "Close review", href="/desk#review"), class_="close-review"))
    return _panel("Next dispatch: review", *content, panel_id="review", css="review-panel reviewing")


def _key(css: str, text: str) -> Markup:
    return tag("li", tag("span", class_=f"swatch {css}", aria_hidden="true"), text)


def _map_panel(ctx: ViewContext, preview: DispatchPlan | None) -> Markup:
    legend = tag(
        "ul",
        _key("key-road", "Road (minutes)"),
        _key("key-closed", "Closed road"),
        _key("key-scene", "Crew to scene"),
        _key("key-hospital", "Crew to hospital"),
        _key("key-next", "Next leg"),
        _key("key-preview", "Proposed in review"),
        class_="map-key",
    )
    return _panel("Harbour District", tag("div", render_map(ctx.state, preview), class_="map-frame"), legend,
                  panel_id="map", css="map-panel", subtitle="Fictional town · simulated minutes")


def _clock_panel(ctx: ViewContext) -> Markup:
    return _panel(
        "Simulation clock",
        tag("p", f"Minute {ctx.state.minute}", class_="clock-big"),
        tag("div",
            _form(ctx, "/time/advance", _button("Next minute"), minutes="1"),
            _form(ctx, "/time/advance", _button("+5 minutes"), minutes="5"),
            class_="button-row"),
        tag("p", "Advancing time only moves crews you have confirmed. It never dispatches a waiting call.",
            class_="hint"),
        tag("p", tag("strong", "Scorecard: "), build_scorecard(ctx.state).headline(), " ",
            tag("a", "Open", href="/archive#scorecard"), class_="score-teaser"),
        panel_id="clock",
    )


def _mission_card(state: SimulationState, incident: Incident) -> Markup:
    ambulance = state.ambulances.get(incident.ambulance_id)
    hospital = state.hospitals.get(incident.hospital_id)
    status = None
    if incident.status == ASSIGNED and ambulance is not None:
        if ambulance.is_blocked:
            status = tag("p", tag("strong", "Blocked. "), ambulance.blocked_reason, class_="mission-status is-blocked")
        elif ambulance.status == HANDOVER:
            status = tag("p", f"{ambulance.id} is handing over at {hospital.name}.", class_="mission-status")
        else:
            remaining = simulation.minutes_remaining(state, ambulance)
            goal = "scene" if ambulance.status == TO_SCENE else "hospital"
            if remaining is None:
                status = tag("p", f"{ambulance.id} is {position_text(state, ambulance)}. No open route onwards yet; "
                                  f"it will recalculate at {state.place_name(ambulance.edge_to)}.",
                             class_="mission-status is-blocked")
            else:
                status = tag("p", f"{ambulance.id} is {position_text(state, ambulance)}. About {remaining} min to "
                                  f"the {goal} (minute {state.minute + remaining}).", class_="mission-status")
    return tag(
        "article",
        tag("h3", f"#{incident.id} · {incident.ambulance_id} → {hospital.name if hospital else '?'} ",
            _badge(label(INCIDENT_STATUS_LABELS, incident.status), incident.status), " ",
            _badge("Your choice", "choice") if incident.chosen_by == DISPATCHER_CHOICE else None),
        status,
        _timeline(incident),
        class_="mission",
    )


def _timeline_panel(ctx: ViewContext) -> Markup:
    state = ctx.state
    cases = simulation.active_incidents(state) + simulation.completed_incidents(state)[-2:][::-1]
    body = ([_mission_card(state, incident) for incident in cases] if cases
            else [tag("p", "Confirmed missions appear here with each milestone.", class_="empty")])
    return _panel("Response timeline", *body, panel_id="timeline")


def _log_panel(ctx: ViewContext) -> Markup:
    events = [event for event in ctx.state.events if event.kind != "reason"][-12:][::-1]
    body = _log_list(events) if events else tag("p", "Nothing has happened yet this shift.", class_="empty")
    return _panel("Shift log", body, panel_id="log", subtitle="Newest first")


def desk_page(ctx: ViewContext, review: Review, reviewing: bool = False, plan_fields=None,
              form_errors=None, form_values=None) -> Markup:
    side = tag("div", _call_panel(ctx), _manual_panel(ctx, form_errors or {}, form_values or {}),
               _queue_panel(ctx, review.recommendation), class_="desk-side")
    main = tag("div", _review_panel(ctx, review, reviewing, plan_fields),
               _map_panel(ctx, review.plan if reviewing else None),
               tag("div", _clock_panel(ctx), _timeline_panel(ctx), class_="desk-pair"),
               _log_panel(ctx), class_="desk-main")
    return _layout(ctx, "desk", "Dispatch desk", tag("div", side, main, class_="desk-grid"))


def _ambulance_panel(ctx: ViewContext) -> Markup:
    state = ctx.state
    rows = []
    for ambulance in state.sorted_ambulances():
        if ambulance.is_blocked:
            status = [_badge("Blocked", "blocked"), tag("p", ambulance.blocked_reason, class_="cell-note")]
        else:
            status = _badge(label(AMBULANCE_STATUS_LABELS, ambulance.status), ambulance.status)
        mission = "—"
        if ambulance.incident_id is not None:
            mission = f"#{ambulance.incident_id} → {state.hospitals[ambulance.hospital_id].name}"
        rows.append(tag("tr", tag("th", f"{ambulance.id} · {ambulance.name}", scope="row"), tag("td", status),
                        tag("td", position_text(state, ambulance)), tag("td", mission), tag("td", ambulance.capacity)))
    return _panel("Ambulances", _table("Ambulance crews", ("Unit", "Status", "Position", "Mission", "Room"), rows),
                  panel_id="ambulances")


def _hospital_panel(ctx: ViewContext) -> Markup:
    state = ctx.state
    rows = []
    for hospital in state.sorted_hospitals():
        if hospital.occupied:
            choices = [tag("option", n, value=n) for n in range(1, hospital.occupied + 1)]
            action = _form(ctx, "/hospital/discharge",
                           tag("label", "Patients", tag("select", choices, name="count"), class_="compact-label"),
                           _button("Discharge", "btn btn-small"), css="inline-form discharge-form",
                           hospital=hospital.id)
        else:
            action = tag("span", "No patients to discharge", class_="muted")
        rows.append(tag(
            "tr",
            tag("th", f"{hospital.id} · {hospital.name}", scope="row"),
            tag("td", ", ".join(label(FACILITY_LABELS, f) for f in hospital.facilities)),
            tag("td", hospital.capacity), tag("td", hospital.occupied), tag("td", hospital.reserved),
            tag("td", tag("strong", hospital.free)),
            tag("td", action),
        ))
    return _panel(
        "Hospitals",
        _table("Hospital beds", ("Hospital", "Facilities", "Beds", "Occupied", "Reserved", "Free", "Discharge"), rows),
        tag("p", "Free = beds − occupied − reserved. Beds are reserved when you confirm a crew and become occupied "
                 "at handover. Discharging frees occupied beds only; reserved beds stay held for crews on the way.",
            class_="hint"),
        panel_id="hospitals",
    )


def _road_panel(ctx: ViewContext) -> Markup:
    state = ctx.state
    rows = []
    for road in state.sorted_roads():
        action = "close" if road.open else "reopen"
        rows.append(tag(
            "tr",
            tag("th", f"{road.id} · {state.road_label(road)}", scope="row"),
            tag("td", f"{road.minutes} min"),
            tag("td", _badge("Open", "open") if road.open else _badge("Closed", "closed")),
            tag("td", _form(ctx, "/road/toggle", _button("Close road" if road.open else "Reopen road",
                                                         "btn btn-small" + ("" if road.open else " btn-primary")),
                            road=road.id, action=action)),
        ))
    return _panel(
        "Roads",
        _table("Two-way roads and simulated travel times", ("Road", "Time", "Status", "Change"), rows),
        tag("p", "When a road closes, a crew already on it finishes that road and recalculates at the next junction. "
                 "A reachable hospital is kept; otherwise the booking moves only once another suitable hospital is "
                 "found, or the crew is held until a road reopens.", class_="hint"),
        panel_id="roads",
    )


def resources_page(ctx: ViewContext) -> Markup:
    return _layout(ctx, "resources", "Resources", _map_panel(ctx, None),
                   tag("div", _ambulance_panel(ctx), _hospital_panel(ctx), class_="resource-stack"),
                   _road_panel(ctx))


def _case(state: SimulationState, incident: Incident) -> Markup:
    events = [event for event in state.events if event.incident_id == incident.id]
    ambulance = state.ambulances.get(incident.ambulance_id)
    hospital = state.hospitals.get(incident.hospital_id)
    response = incident.completed_minute - incident.reported_minute
    return tag(
        "details",
        tag("summary",
            tag("span", f"#{incident.id}", class_="case-id"), _priority(incident.priority),
            tag("span", f"{label(CATEGORY_LABELS, incident.category)} at {state.place_name(incident.location)} · "
                        f"{_patients(incident.patients)} · done at minute {incident.completed_minute} "
                        f"({response} min after the call)", class_="case-summary")),
        tag("div",
            tag("blockquote",
                tag("p", incident.caller_message or "No message recorded."),
                tag("footer", "— ", incident.caller_name,
                    f" · {label(SOURCE_LABELS, incident.source)} · minute {incident.reported_minute}")),
            tag("dl",
                _fact("Crew", f"{incident.ambulance_id} · {ambulance.name if ambulance else ''}"),
                _fact("Hospital", hospital.name if hospital else incident.hospital_id),
                _fact("Decision", _decision_text(state, incident)),
                class_="facts"),
            _timeline(incident),
            tag("h3", "Decisions and events"),
            _log_list(events),
            class_="case-body"),
        class_="case",
    )


def _decision_text(state: SimulationState, incident: Incident) -> str:
    if incident.chosen_by is None:
        return "—"
    if incident.chosen_by != DISPATCHER_CHOICE:
        return "RESQ's pick"
    extra = None
    if incident.planned_minutes is not None and incident.recommended_minutes is not None:
        extra = incident.planned_minutes - incident.recommended_minutes
    hospital = state.hospitals.get(incident.recommended_hospital_id)
    return (f"Your choice ({_extra_text(extra)} driving); RESQ suggested {incident.recommended_ambulance_id} + "
            f"{hospital.name if hospital else incident.recommended_hospital_id}")


def _tile(name: str, value: str, note: str) -> Markup:
    return tag("div", tag("p", name, class_="tile-name"), tag("p", value, class_="tile-value"),
               tag("p", note, class_="tile-note"), class_="tile")


def _scorecard_panel(state: SimulationState) -> Markup:
    card = build_scorecard(state)
    tiles = tag(
        "div",
        _tile("Targets met", f"{card.targets_met} / {card.targets_judged}" if card.targets_judged else "—",
              "incidents reached within the fictional target"),
        _tile("Time to patient", f"{card.average_reach} min" if card.average_reach is not None else "—",
              "average, from call to pickup"),
        _tile("Your decisions", f"{card.resq_picks} RESQ · {card.own_choices} yours",
              "own choices: " + _extra_text(card.extra_minutes) + " driving" if card.own_choices
              else "no choices of your own yet"),
        _tile("Disruptions", f"{card.reroutes} · {card.holds} · {card.transfers}",
              "reroutes · crews held · bookings moved"),
        class_="tiles",
    )
    rows = []
    for case in card.cases:
        if case.met_target is True:
            verdict = _badge("Within target", "open")
        elif case.met_target is False:
            verdict = _badge("Missed", "closed")
        else:
            verdict = _badge("Pending", "pending")
        reached = "—" if case.reached is None else f"{case.reached} min"
        decision = {None: "—", DISPATCHER_CHOICE: f"Yours ({_extra_text(case.extra_minutes)})"}.get(
            case.decision, "RESQ's pick")
        rows.append(tag(
            "tr",
            tag("th", f"#{case.incident_id} ", _priority(case.priority), scope="row"),
            tag("td", f"{label(CATEGORY_LABELS, case.category)} · {case.place}"),
            tag("td", label(INCIDENT_STATUS_LABELS, case.status)),
            tag("td", f"{case.waited} min"),
            tag("td", reached, " / ", f"{case.target}", " ", verdict),
            tag("td", "—" if case.handed_over is None else f"{case.handed_over} min"),
            tag("td", decision),
        ))
    table = (_table("Every call this shift, most urgent first. Times are minutes after the call.",
                    ("Call", "What · where", "Status", "Waited for a crew", "Reached / target", "Handed over",
                     "Decision"), rows)
             if rows else tag("p", "No calls yet.", class_="empty"))
    return _panel(
        "Shift scorecard",
        tag("p", card.headline(), class_="score-headline"),
        tiles,
        table,
        tag("p", "Targets are fictional: Critical 8 min, High 12 min, Moderate 20 min from the call until the crew "
                 "reaches the patients. A waiting call that passes its target counts as missed.", class_="hint"),
        panel_id="scorecard",
        subtitle=f"so far · minute {state.minute}",
    )


def archive_page(ctx: ViewContext) -> Markup:
    state = ctx.state
    done = simulation.completed_incidents(state)[::-1]
    if done:
        cases = [tag("p", f"{len(done)} completed. Open a case to read the caller's words, its timeline and "
                          "every timestamped decision.", class_="hint"),
                 [_case(state, incident) for incident in done]]
    else:
        cases = [tag("p", "No cases have been completed yet. A case arrives here after the hospital handover.",
                     class_="empty")]
    open_cases = len(simulation.waiting_queue(state)) + len(simulation.active_incidents(state))
    return _layout(
        ctx, "archive", "Case archive",
        _scorecard_panel(state),
        _panel("Completed cases", *cases, panel_id="cases"),
        tag("p", f"{open_cases} open case(s) are on the Dispatch desk.", class_="hint"),
    )


def _confirm_box(text: str) -> Markup:
    return tag("label", tag("input", type="checkbox", name="confirm", value="yes", required=True), " ", text,
               class_="check")


def shifts_page(ctx: ViewContext, checkpoints: list[CheckpointInfo], saving_enabled: bool = True) -> Markup:
    save = _panel(
        "Save this shift",
        _form(ctx, "/shift/save",
              tag("div", tag("label", "Checkpoint label", for_="field-label"),
                  tag("input", type="text", name="label", id="field-label", maxlength=MAX_LABEL_LENGTH,
                      value=f"Shift at minute {ctx.state.minute}", required=True), class_="field"),
              _button("Save checkpoint", "btn btn-primary"), css="save-form"),
        tag("p", "Each save makes a new file in the saves folder next to main.py. Existing checkpoints are never "
                 "overwritten. A save keeps the clock, closed roads, crews part-way along roads, bed bookings, "
                 "every case, caller notes, the log and the next scripted caller.", class_="hint"),
        panel_id="save",
    )
    if checkpoints:
        rows = []
        for item in reversed(checkpoints):
            if item.problem:
                rows.append(tag("tr", tag("th", item.name, scope="row"),
                                tag("td", tag("span", "Cannot be restored: ", item.problem, class_="cell-note"),
                                    colspan=4)))
                continue
            restore = _form(ctx, "/shift/restore", _confirm_box("Replace the current shift"),
                            _button("Restore", "btn btn-small"), css="inline-form restore-form", name=item.name)
            rows.append(tag("tr", tag("th", item.label, tag("span", item.name, class_="file-name"), scope="row"),
                            tag("td", item.saved_at.replace("T", " ")), tag("td", item.minute),
                            tag("td", f"{item.completed} done · {item.waiting} waiting"), tag("td", restore)))
        listing = _table("Checkpoints in the saves folder", ("Checkpoint", "Saved (UTC)", "Minute", "Cases", "Restore"),
                         rows)
    else:
        listing = tag("p", "No checkpoints yet.", class_="empty")
    restore = _panel(
        "Saved checkpoints",
        listing,
        tag("p", "A checkpoint is fully checked before it replaces anything. If a file is damaged or edited by hand "
                 "so that it no longer makes sense, the restore is refused and your current shift is kept.",
            class_="hint"),
        panel_id="checkpoints",
    )
    fresh = _panel(
        "Start a new shift",
        _form(ctx, "/shift/new", _confirm_box("Replace the current shift"), _button("Start new shift", "btn"),
              css="inline-form"),
        tag("p", "Reloads the Harbour District from data/town.json at minute 0. Save first if you want to keep "
                 "this shift.", class_="hint"),
        panel_id="new-shift",
    )
    note = None if saving_enabled else tag("p", "Saving is switched off in this mode.", class_="notice notice-error")
    return _layout(ctx, "shifts", "Saved shifts", note, save, restore, fresh)


def error_page(title: str, message: str) -> Markup:
    """A standalone page for requests that never reached a RESQ screen."""
    return document(
        f"{title} · RESQ",
        tag("header", tag("a", tag("span", "RESQ", class_="brand-mark"), href="/desk", class_="brand"),
            class_="topbar"),
        tag("main", tag("section", tag("h1", title), tag("p", message),
                        tag("p", tag("a", "Back to the dispatch desk", href="/desk", class_="btn")),
                        class_="panel error-panel"), id="main"),
    )
