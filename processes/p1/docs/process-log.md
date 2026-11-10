# How process 1 (rejsegaranti) was built

Work log for issues #7-#11. The work was done by Claude (Anthropic's AI assistant, in Cowork) on Liam Palmqvist's instructions, on 31 October 2026. Use it as input for the AI-use declaration in the report (#42, contract section 4.6). Liam reviews and owns the result.

## 1. Brief

Liam's instruction: do the items assigned to him on the project board and put the result in the local `Project` folder.

## 2. Finding the work

- The board is private, and the session's GitHub token was invalid. The board was therefore read through the built-in browser after Liam signed in to GitHub himself.
- Filtering by `assignee:@me` gave seven items:
  - #1 Process 1: Rejsegaranti claim, the parent issue
  - its sub-issues #7 AS-IS, #8 TO-BE, #9 Camunda, #10 DCR and #11 event log
  - #41 final presentation, a group task
- All 45 issues were read for context: processes 2-6, CEP (#37), integration (#38), quality measures (#39) and hand-in (#42/#43). This showed how process 1 connects to the rest:
  - late notices come from process 6
  - delay and weather situations come from the Siddhi CEP
  - outcomes must go back to the CEP
- The GitHub repository was still empty (0 commits), so there was no existing code or structure to follow.
- #41 was left out because it is a group task.

## 3. Checking the rules

The rejsegaranti terms were read on dinletbane.dk. The rule texts are hidden in collapsed FAQ sections, which were opened in the browser, after declining the non-essential cookies. Confirmed rules:

- the delay must be 30 minutes or more
- the payout is capped at 300 kr
- the trip must start and end within 250 m of a station
- the receipt must show start, end, distance, time, minutes and amount
- the transport provider must be a CVR-registered company
- exclusions: planned changes announced at the latest the day before, and force majeure

Background for the force-majeure thresholds:

- DMI warning levels and the cloudburst definition (15 mm in 30 minutes), from a news explainer
- the 29 Line L stations, from Wikipedia

## 4. Constraints found along the way

| Constraint | Effect on the design |
|---|---|
| Rejseplanen API 2.0 is available on request only (raised by Liam) | No real delay data. Delays are simulated, and the code reads a delay CSV so recorded data can replace the simulation later |
| Maven Central and Docker Hub were blocked from both the cloud workspace and Liam's machine | No Java/Spring project could be built. A Camunda 7 Run container with Python external-task workers was chosen instead, using only the standard library |
| No live Camunda engine available | Everything was checked without an engine (section 7). A live run is still to do |
| An OpenStreetMap query for station coordinates was stopped by Liam | Coordinates were entered by hand, marked approximate (±100 m) and flagged for checking |

The plan, including these trade-offs, was presented to Liam and approved before building started.

## 5. Design decisions

- **Two TO-BE processes instead of one.** #9 asks that instances start from the CEP's delay situation, but claims belong to individual passengers.
  - A single process with an event subprocess per claim would make claims overwrite each other's variables.
  - The solution: a *delay case* process, started by the CEP, decides once per delay whether the guarantee applies. A *claim* process, one instance per passenger, reuses that decision.
- **Milestone events** (*Receipt verified*, *Force majeure* and others) make each decision visible in the event log, so the DCR rules can be checked against it.
- **Shared activity names.** The names are defined once (`activities.py`) and used by the BPMN, the event log and the DCR graph. Tests check that they agree.
- **Force majeure.** A DMI warning of level 2 or higher counts first. OpenWeatherMap thresholds are used only when there is no DMI warning; they are group proposals to be justified in the report.
- **Notice rule.** The notice's first-seen date must be before the date the change takes effect. This matches the example in issue #6.
- **Intake validation.** An incomplete receipt is refused at the claim form. This replaces the AS-IS "ask for missing details" loop (contact-reduction heuristic).
- **Every TO-BE change** is linked to a redesign heuristic and a proposed quality measure (`docs/to-be-redesign.md`), because #39 is still open.

## 6. Building

1. **Python service** (`service/rejsegaranti/`):
   - business rules
   - Camunda REST client
   - HTTP bridge for CEP situations and claims
   - external-task workers
   - outbound CEP events
   - simulator
   - XES export
   - DCR generator and checker
2. **BPMN models**, generated from code (`tools/build_bpmn.py`) with full diagram layout.
   - They were rendered with bpmn-js and inspected visually.
   - The layout was revised three times to fix overlapping labels and crossing lines.
3. **DCR graph**, generated in dcrgraphs.net XML, plus a Graphviz drawing.
4. **Docs**: AS-IS, TO-BE rationale, DCR rules, conformance, CEP interface and README.

## 7. Verification

- **BPMN:** both files parsed with bpmn-moddle and the Camunda moddle (0 warnings) and imported into bpmn-js (0 warnings).
- **Unit tests:** 19 tests, passing on Python 3.13 in the workspace and Python 3.10 on Liam's machine. They cover:
  - each rule, including the issue #6 notice example
  - the worker retry and incident paths
  - the bridge over HTTP
  - consistency between the BPMN and the code
  - that simulated traces only follow paths in the BPMN
  - that the DCR graph accepts a clean log and catches each kind of violation
- **Tests that changed the design:**
  - the first DCR version did not enforce "an eligible claim must be paid" (R8), because a pending event that is excluded does not block the end of a trace. An exclude relation was added.
  - the offline simulation did not reproduce the intake check, so it was aligned with the bridge.
- **Dry run on simulated data:**
  - the clean log fully conforms (110 traces, fitness 1.000)
  - all 7 deliberately injected violations were detected (fitness 0.936)
  - these figures show that the pipeline works. They are not findings about Line L.

## 8. Delivery

- The files were copied into `Project/processes/p1-rejsegaranti/`, and a root `README.md` was added.
- Git was initialised with the group repository as `origin`, and one local commit was made. Its author is Liam Palmqvist <liam.palmqvist@icloud.com>, corrected on Liam's request.
- Nothing was pushed, and no issue statuses were changed.
- The connected folder does not allow deletions, so git's lock files were renamed (`.git/stale*`). They can be deleted by hand.

## 9. Open items

- [ ] Request Rejseplanen API 2.0 access, then rerun with recorded delays
- [x] First live run on Camunda (`docker compose up`, then the steps in the README). See section 10
- [ ] Check the station coordinates against OpenStreetMap or Rejseplanen
- [ ] Upload the DCR XML and the exported XES log to processdoctor.dtu.dk, and write the deviation analysis
- [ ] Agree the quality measures with the group (#39)
- [ ] Confirm the AS-IS assumptions (order of the checks, the missing-details loop) with the TAs or the operator
- [ ] Review and push the commit
- [x] Enough real delay cases to analyse: run on every Copenhagen line, not only 150S. See section 11

## 10. First live run on Camunda

Done by Claude (Anthropic's AI assistant, in Claude Code in Liam's terminal) on Liam's instructions, on 31 October 2026, against Camunda 7 Run 7.24 in Docker. What broke and how it was fixed:

- **Deploy rejected:** the generator wrote `<bpmn:documentation>` directly under `<bpmn:definitions>`, which the BPMN schema does not allow. It now goes inside the collaboration (`tools/bpmn_layout.py`), and both models were regenerated.
- **Payout type:** a capped payout came back as the integer 300, so Camunda stored it as Long while other amounts were Double. The cap is now `300.0`, and a test checks the type.
- **Duplicate receipts raised incidents:** `simulate --post` sent all claims within seconds, so a resubmitted receipt raced the first claim to "Pay out claim", and the payout guard left an incident. In the scenario, a resubmission comes days later. The simulator now waits for the first claim's outcome before resubmitting its receipt, which matches the dry run. The guard itself is unchanged.
- **Demo process in the counts:** Camunda Run deploys an "invoice" demo with running instances, so the instance count could never reach 0. It is switched off in `docker-compose.yml`.
- **Local only:** in an iCloud-synced folder, iCloud hides everything in `.venv`, and Python then skips the editable install. The workaround is in the README.

Result on simulated data: 111 claim traces, 0 violating, 0 incidents. All 110 simulated claims got the same outcome as in the dry run (`docs/conformance.md`).

## 11. Process evolution: from bus 150S to every Copenhagen bus and S-tog line

Decided by Liam on 10 November 2026; implemented by Claude (Anthropic's AI assistant, in Claude Code).

**What changed.** The process now runs on every Movia bus and DSB S-tog line with a stop in Copenhagen (79 lines, 4,764 stop points, 2,808 stations), instead of on bus 150S alone. The rules are unchanged (30 minutes, 300 kr, 250 m, notices, force majeure). Only the set of lines a delay case can start on, and the stations a claim is measured against, grew.

**Why.** In the first 2.6 days on the server (7 November 22:20 to 10 November 13:05 UTC) not one 150S journey ran 30+ minutes late; the worst was 25 minutes. So the bridge received no delay, Camunda started no case, the feed simulated no claim, and there was no event log to analyse. In the same period the recorder saw 42 journeys run 30+ minutes late on 22 of the Copenhagen lines (5 of them on the S-tog). With every Copenhagen line, about 16 delay cases a day would have started, each with its simulated claims. 150S was chosen as a stand-in for Line L because it shares Line L's DTU and Lundtofte stops (section "Why bus 150S" in `deploy/README.md`); a stand-in that never qualifies tells us nothing about the process.

**How.**

- `tools/build_stations.py` writes `data/stations.csv` (with a `line` column) for every Movia and DSB S-tog line with a stop inside a box around Copenhagen and Frederiksberg (55.60-55.76 N, 12.40-12.65 E). All of a line's stops are kept, also those outside the box. It replaces `data/stations-150S.csv`.
- `REJSEGARANTI_LINES` (was `REJSEGARANTI_LINE`) selects the lines: `L` (the default, Line L's hand-entered stations), `*` (every line in `data/stations.csv`, set on the server) or a comma-separated list.
- Station names are only unique within a line, so every lookup now takes the line. A claim is matched to a delay by the nearest station of *that delay's* line, and both 250 m checks measure to stations of the delayed line. A notice applies to the lines it lists; one without lines applies to every line.
- The recorder sends every line's delays (`BRIDGE_LINES=*`). The bridge still decides which qualify, and ignores lines it has no stations for (HTTP 202).
- When several lines are configured, a delay situation must name its line (HTTP 400 otherwise).

**Consequences for the analysis.** Delay cases now mix lines with very different frequencies and stop spacing, so per-line results are not comparable without normalising by journeys. The BPMN, DCR and activity names still say Line L, as before. The claims are still simulated. Delays recorded before the change were not replayed into the bridge, so cases start from the deployment onwards.
