"""Start the local RESQ server or run the scripted demo."""

import sys


if sys.version_info < (3, 10):
    print("RESQ needs Python 3.10 or newer, but this is Python "
          + ".".join(str(part) for part in sys.version_info[:3]) + ".")
    print("Try running:  python3.13 main.py")
    sys.exit(1)


import argparse
import copy
from pathlib import Path

import scorecard
import simulation
import web
from application import ActionError, DispatchApp, build_review
from storage import StorageError

HERE = Path(__file__).resolve().parent
TOWN_FILE = HERE / "data" / "town.json"
SAVES_DIR = HERE / "saves"
STATIC_DIR = HERE / "Frontend" / "static"


def _say(minute: int, text: str) -> None:
    """Print a message with its simulated minute."""
    print(f"[minute {minute:>2}] {text}")


def run_demo(town_path: Path = TOWN_FILE) -> int:
    """Run the scripted dispatch and road-closure demonstration."""
    app = DispatchApp(town_path, saves_dir=None)

    def state():
        return app.snapshot().state

    def revision():
        return app.snapshot().revision

    print("RESQ deterministic demo - fictional Harbour District, simulated minutes\n")

    _say(0, app.receive_call(revision()))
    incident = state().incidents[1]
    _say(0, f'Caller says: "{incident.caller_message}"')

    recommendation = simulation.recommend(state())
    plan = recommendation.plan
    _say(0, f"Review (read-only): send {plan.ambulance_id} via "
            f"{simulation.route_text(state(), plan.route_to_scene)}, then "
            f"{simulation.route_text(state(), plan.route_to_hospital)}.")
    for reason in plan.reasons:
        print(f"            - {reason}")
    options = simulation.dispatch_options(state(), state().incidents[plan.incident_id])
    if len(options.ambulances) > 1:
        alternative = simulation.plan_with_choice(state(), state().incidents[plan.incident_id],
                                                  options.ambulances[1][1].id, plan.hospital_id)
        _say(0, f"Choose differently? Sending {alternative.ambulance_id} instead: {alternative.reasons[2]}")
    _say(0, "The demo keeps RESQ's pick.")

    fields = app.plan_form_fields(plan, revision())
    _say(0, app.confirm_dispatch(fields))
    try:
        app.confirm_dispatch(fields)
    except ActionError as error:
        _say(0, f"Duplicate confirm refused (HTTP {error.status}); nothing extra was reserved.")

    for _ in range(2):
        _say(state().minute + 1, app.advance(revision(), "1"))
    ambulance = state().ambulances["A1"]
    _say(state().minute, f"A1 is between {state().place_name(ambulance.location)} and "
                         f"{state().place_name(ambulance.edge_to)}.")

    _say(state().minute, app.set_road(revision(), "R6", "close"))
    for event in state().events:
        if event.kind == "reroute" and event.minute == state().minute:
            print(f"            - {event.text}")


    print()
    _say(state().minute, app.receive_call(revision()))
    priya_plan = simulation.recommend(state()).plan
    warning = [reason for reason in priya_plan.reasons if reason.startswith("Heads-up")]
    _say(state().minute, f"RESQ's pick: {priya_plan.ambulance_id} to "
                         f"{state().hospitals[priya_plan.hospital_id].name}. {' '.join(warning)}")
    blind = copy.deepcopy(state())
    simulation.commit_plan(blind, simulation.recommend(blind).plan)
    simulation.report_incident(blind, category="cardiac", priority="critical", location="old_town", patients=1,
                               caller_name="Tomas", caller_message="", source="call")
    held = simulation.recommend(blind).held
    if held:
        _say(state().minute, f"(If we followed it, the next call - a heart attack - would be held: {held[0].reasons[0]})")
    review = build_review(state(), priya_plan.ambulance_id, "H3")
    _say(state().minute, f"Your choice: Eastgate General instead. {review.plan.reasons[2]}")
    _say(state().minute, app.confirm_dispatch(app.plan_form_fields(review.plan, revision())))
    _say(state().minute, app.receive_call(revision()))
    tomas_plan = simulation.recommend(state()).plan
    _say(state().minute, app.confirm_dispatch(app.plan_form_fields(tomas_plan, revision())))
    print()

    for _ in range(2):
        _say(state().minute + 5, app.advance(revision(), "5"))

    final = state()
    incident = final.incidents[1]
    hospital = final.hospitals["H1"]
    print()
    _say(final.minute, f"Incident #1 is {incident.status}: picked up at minute {incident.pickup_minute}, "
                       f"at hospital minute {incident.arrival_minute}, handover complete minute "
                       f"{incident.completed_minute}.")
    _say(final.minute, f"{hospital.name}: {hospital.occupied} occupied, {hospital.reserved} reserved, "
                       f"{hospital.free} free.")
    print("\nShift scorecard:")
    for line in scorecard.summary_lines(final):
        print(f"  {line}")
    problems = simulation.check_invariants(final)
    print("\nInvariant check:", "all rules hold." if not problems else "; ".join(problems))
    card = scorecard.build_scorecard(final)
    ok = not problems and incident.completed_minute == 12 and card.targets_met == card.targets_judged == 3
    return 0 if ok else 1


def main(argv=None) -> int:
    """Parse options and start the server or demo."""
    parser = argparse.ArgumentParser(description="RESQ - local emergency-response coordination prototype (simulated town)")
    parser.add_argument("--demo", action="store_true", help="run the scripted demo in the terminal and exit")
    parser.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="local port (default 8765)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        if args.demo:
            return run_demo()
        if not 1024 <= args.port <= 65535:
            print("Choose a port between 1024 and 65535.")
            return 2
        app = DispatchApp(TOWN_FILE, SAVES_DIR)
    except StorageError as error:
        print(f"RESQ could not load its town data: {error}")
        return 1
    return web.run_server(app, STATIC_DIR, args.port)


if __name__ == "__main__":
    sys.exit(main())
