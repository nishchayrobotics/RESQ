# RESQ — Emergency Response & Dispatch Simulator

RESQ is a local emergency-response coordination prototype using a simulated environment. It was
built for COMP9001 (University of Sydney). The user acts as the dispatcher: answer scripted calls
or report incidents manually, review RESQ's recommendation and its reasons, then either accept it
or **choose a different crew or hospital** and see what that choice costs. Advance simulated time:
ambulances drive a small road graph, roads can close mid-mission, and hospital beds are reserved,
transferred and filled. A **shift scorecard** shows how quickly each
incident's patients were reached against fictional response targets, and how the dispatcher's
own decisions played out.

The idea in one line: RESQ's rule is simple and explains itself, but it cannot see ahead. In
the scripted shift, following it blindly fills the only cardiac unit just before a heart-attack
call. Overruling it, at no extra cost, keeps that bed free.

It is **not** emergency-service software, a medical tool, a telephone system or a live map.
Every place, caller and rule is invented for this prototype.

Possible future direction (**not built**): the same coordination pattern could serve care
facilities, where a SafeGuard wearable alert is matched to an available caregiver nearby.

## Run it

Requirements: **Python 3.10 or newer** and any web browser. Nothing needs to be installed —
RESQ uses only Python's standard library.

```
python3.13 main.py
```

Then open the printed address, **http://127.0.0.1:8765**, in a browser. Keep the terminal open;
press Ctrl+C to stop. (On Windows use `py main.py`; if port 8765 is busy add `--port 8766`.)

Other commands:

```
python3.13 main.py --demo                              # the presentation sequence in the terminal
python3.13 -m unittest discover -s tests -t . -v       # the test suite
python3.13 tools/language_audit.py                     # refresh LANGUAGE_AUDIT.json
```

If you see "RESQ needs Python 3.10 or newer", you ran an older `python3` (macOS ships 3.9).
Use `python3.13` instead.

## The four screens

1. **Dispatch desk**: incoming call, manual report form, priority queue, review (RESQ's pick,
   or your own choice with its cost) and confirm, map, simulation clock, response timeline and
   shift log.
2. **Resources**: ambulance states, hospital beds (with discharge) and road closures.
3. **Case archive**: the shift scorecard, then completed cases with the caller's words and
   every timestamped decision.
4. **Saved shifts**: save checkpoints, restore one, or start a new shift.

Refreshing or switching pages keeps the shift because it lives in the running Python program.
Stopping Python discards anything that was not saved under **Saved shifts**.

## Project structure

| File | Purpose |
|---|---|
| `main.py` | Entry point: starts the local server, or runs `--demo`. Checks the Python version first. |
| `models.py` | Data classes: Location, Road, Ambulance, Hospital, Incident, frozen DispatchPlan. |
| `routing.py` | Dijkstra's algorithm and path reconstruction. |
| `simulation.py` | The rules: queue order, recommendation, the dispatcher's own choices, confirmation, movement, closures, beds, invariants. |
| `scorecard.py` | Read-only shift summary: response times against fictional targets, decisions, disruptions. |
| `storage.py` | JSON town data and shift checkpoints: validation, atomic saves, safe restore. |
| `intake.py` | Scripted caller deck, manual-form validation and on-screen labels. |
| `application.py` | User actions as transactions, revision checks and signed dispatch plans. |
| `web.py` | Local HTTP server (`http.server`) with an allowlist of routes. |
| `html_tools.py` | Builds escaped HTML/SVG from Python calls. |
| `views.py` | The four screens. |
| `map_view.py` | Draws the road graph, routes and moving ambulances as SVG. |
| `static/style.css` | A small supporting stylesheet. No JavaScript is used anywhere. |
| `data/town.json` | 10 places, 15 roads, 3 ambulances, 3 hospitals, 5 scripted callers. |
| `tests/` | Engine, storage, application and real-HTTP integration tests. |
| `tools/language_audit.py` | Development tool that measures the Python share of the source. |
| `docs/` | Design notes, learning guide, presentation script, Padlet text and image, rubric audit, AI disclosure. |
