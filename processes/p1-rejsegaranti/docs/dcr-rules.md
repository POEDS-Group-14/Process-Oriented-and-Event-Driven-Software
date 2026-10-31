# Process 1 – Rejsegaranti DCR compliance rules (issue #10)

Author: Liam Palmqvist · Files: `models/rejsegaranti-rules.dcr.xml` (dcrgraphs.net XML), `models/rejsegaranti-rules.pdf`/`.svg` (drawing)

The graph is generated from `service/rejsegaranti/dcr.py` (`RULES`) with `uv run rejsegaranti build-dcr`. The event labels are the activity names in the TO-BE claim process, so the Camunda event log can be checked against the graph without renaming anything.

All events start included, none executed or pending.

| Rule | DCR relations | Meaning |
|---|---|---|
| R1 No payout without a receipt | *Receipt verified* →• *Pay out claim*; *Receipt rejected* →% *Pay out claim* | Payout needs a verified receipt, and a rejected receipt rules payout out |
| R2 No payout for a change announced by the day before | *Planned change announced by the day before* →% *Pay out claim* | |
| R3 No payout under force majeure or without a verified delay | *Force majeure* →% *Pay out*; *No verified delay* →% *Pay out*; *Delay verified* →• *Pay out* | |
| R4 250 m rule | *Trip within 250 m* →• *Pay out*; *Trip outside 250 m* →% *Pay out* | |
| R5 Cap calculated first | *Calculate payout* →• *Pay out claim* | The 300 kr cap is applied in this step. The amount itself is data, so it is checked in the unit tests, not in DCR |
| R6 At most one payout per claim | *Pay out claim* →% *Pay out claim*; *Pay out* →% *Reject claim*; *Reject claim* →% *Pay out* | One decision, taken once |
| R7 Every claim gets a reported outcome | *Claim submitted* •→ *Report claim outcome to CEP*; *Pay out* →• *Report*; *Reject* →• *Report*; *Report* →% *Report* | The response must be fulfilled before the case ends. The outcome follows whichever decision was taken (the other decision is excluded, so its condition no longer applies) |
| R8 An eligible claim is paid | *Trip within 250 m* •→ *Pay out claim*; *Trip within 250 m* →% *Reject claim* | |

The remaining conditions (for example *Check receipt* →• *Receipt verified*) only fix the order of the checks.

Legend: →• condition, •→ response, →% exclude.

## Checking locally

`uv run rejsegaranti check <log.xes> [-v]` replays each trace with standard DCR semantics:

- an event is enabled if it is included, and every included condition has been executed
- executing an event marks it executed, clears its pending response, makes its responses pending, then applies excludes and includes
- at the end of a trace, no included event may be pending

This is a pre-check only. The official check for the hand-in is processdoctor.dtu.dk (see `conformance.md`).

## Opening it in the DCR portal

Import `rejsegaranti-rules.dcr.xml` at dcrgraphs.net. The generated layout is a plain grid, so tidy it there before taking the screenshot for the report. If the portal or processdoctor rejects the file, recreate the graph from the table above. There are 17 events and 28 relations.
