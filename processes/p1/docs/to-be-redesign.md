# Process 1 - Movia travel guarantee: TO-BE redesign (issue #8)

Author: Liam Palmqvist · Notation: BPMN 2.0 and DMN 1.3, executable on Camunda 7.24 · Models: `models/to-be/`

The AS-IS (`docs/as-is.md`) is one claim-handling process in which a case worker checks every claim, using
four decision tables. The TO-BE keeps those decisions, makes the engine take them, and splits the work by who
it serves and how often it happens. This document says what changed and why. The earlier Line L TO-BE is in
`models/archive/`; it was replaced because it used Line L's rules (30 min, 250 m, CVR) while the AS-IS is
Movia's, so the two could not be compared on the same claims.

## The models

| # | Model | Kind | Executable | File |
|---|---|---|---|---|
| 1 | Process landscape | Overview: how the processes group and connect | No | `process-landscape.drawio` (+ `.png`, `.svg`) |
| 2 | Collaboration overview | Overview: the pools as black boxes, with the messages between them | No | `collaboration-overview.bpmn` |
| 3 | Passenger journey | Customer-oriented | No | `passenger-journey.bpmn` |
| 4 | Assess disruption | Business-oriented, once per delay | Yes | `assess-disruption.bpmn` |
| 5 | Decide claim and pay out | Business-oriented, once per claim | Yes | `decide-claim.bpmn` |
| 6 | Handle appeal | Business-oriented, once per appeal | Yes | `handle-appeal.bpmn` |
| 7 | TO-BE DRD and six decision tables | Decisions | Yes | `travel-guarantee-to-be.dmn` |

Each `.bpmn` and `.dmn` has a `.png` and `.svg` next to it, and each table a `travel-guarantee-to-be.<decision>.png`.

### Why several models instead of one

The AS-IS decision tree (Tables 1 to 4) stays the core of the TO-BE, and the decisions themselves hardly
change. What changes is the process around them, and that is easier to read in levels:

- **The landscape** shows the split the redesign is built on: what the passenger experiences
  (customer-oriented), what Movia does to decide and pay (business-oriented), what keeps it running (support:
  the recorder and the tables) and what management watches.
- **The collaboration overview** shows only the conversations between the parts, so the detail models can
  each show one part.
- **The detail models** each carry the changes for one part, so every change in the table below points at
  one diagram.

The split into executable processes follows **how often** the work happens, not just who does it. A delay
happens once and can affect many claims, a claim happens once per passenger, and an appeal is rare. Putting
the delay assessment inside the claim process would repeat it for every claim (the AS-IS does this), and
putting the appeal inside it would keep every claim open while the passenger decides whether to appeal.
Paying out stays inside "Decide claim": it is one step, always follows an approval, and a separate process
would only add a message and a waiting state.

Appeals are expected to be rare, because the decision rests on recorded data (the real-time delay, the
receipt, the weather readings). The process is still worth having: the real-time data can miss a delay, and a
passenger needs a way to say so that ends with a person looking at it.

## Changes, heuristics and quality measures

Heuristic names follow Reijers and Limam Mansar's best practices from the course. The quality measures use
the devil's quadrangle (time, cost, quality, flexibility).

| # | Change (AS-IS to TO-BE) | Where | Heuristic | Measure it improves |
|---|---|---|---|---|
| 1 | A case worker looks up the delay; now the delay monitor reports every 20+ minute delay and starts a case | Assess disruption | Task automation; integral technology | **Time:** claim handling time. **Cost:** claims decided without a case worker |
| 2 | Cause and exclusion are decided once per delay and reused by every claim on it, instead of once per claim | Assess disruption, DRD (Exclusion is input data of "Travel guarantee applies") | Task elimination; case-based work | **Cost:** decisions per claim. **Quality:** every claim on one delay gets the same exclusion |
| 3 | Passengers on a qualifying delay are told they may take a taxi and must keep the receipt | Assess disruption ("Notify affected passengers"), Passenger journey | Contact reduction (proactive); customer teams | **Quality:** fewer claims rejected for a missing receipt. **Time:** the passenger knows before paying |
| 4 | Weather and force majeure judged by a person; now by the "Cause of disruption" table on DMI warnings, OpenWeatherMap readings and operator cause codes | DRD | Task automation; standardised decision rules | **Quality:** consistency and traceability (the inputs are stored with the decision) |
| 5 | Limits (20 min, 24 h, 14 days, 300 kr, weather) move into the tables as rules on raw numbers; the AS-IS fed the tables yes/no answers worked out by hand | DRD | Business rules separated from the process | **Flexibility:** a changed term is one table row, not a code change |
| 6 | The 300 kr cap becomes the "Refund amount" table instead of a calculation task | DRD, Decide claim | Same as 5 | **Flexibility** |
| 7 | Claim validity (receipt, 14 days, receipt not used before) is checked first and rejects without looking at the delay | Decide claim | Knock-out: the cheapest check that rejects first | **Cost:** work on claims that are rejected anyway |
| 8 | A receipt used on an earlier claim is a rule in "Claim valid", checked at intake, instead of a payout that fails | DRD, Decide claim | Knock-out; control relocation | **Cost:** no failed payouts. **Quality:** one refund per receipt |
| 9 | Every decision ends with a message to the passenger and an outcome event to the delay monitor | Decide claim, Handle appeal | Integral technology; knowledge transfer back to monitoring | **Flexibility:** payouts per line and per delay can be watched (Management in the landscape) |
| 10 | New: a passenger can appeal, and a case worker reviews it | Handle appeal, Passenger journey | Case worker only for exceptions (triage) | **Quality:** a way to correct the data. **Cost:** a person only sees appealed claims |

### Proposed measures (issue #39)

- **Claim handling time:** claim submitted to decision sent (median and 90th percentile), from the Camunda history.
- **Automation rate:** share of claims decided without a user task or incident.
- **Decision consistency:** share of claims on the same delay with the same exclusion (100% by design).
- **Appeal rate and appeals upheld:** how often the automated decision is challenged, and how often it was wrong.

## Notation choices

- **Executable models keep to the subset processdoctor reads.** processdoctor.compute.dtu.dk turns a BPMN
  model into a DCR graph. Its parser (read from the site's own source, `bpmn2dcr-pycore/bpmn_parser.py`) only
  looks at the first `<bpmn:process>` in a file and only at plain `<bpmn:task>` elements, start and end
  events, exclusive, parallel and inclusive gateways and sequence flows. It also requires one start event,
  exactly one flow in and one out of every task, and every gateway split closed by a join of the same type.
  The old TO-BE broke these rules (two processes in one file, intermediate milestone events, a gateway split
  without a join) and was rejected. So every TO-BE process is block-structured, has one start event, and
  sits in its own file, and each executable file has a `.processdoctor.bpmn` copy with the same ids, names
  and layout in which the typed tasks are plain tasks. The copies and the passenger journey pass
  processdoctor's own parser and translation.
- **Business rule tasks write plain variables.** Each rule task maps the decision's outputs to process
  variables (`claimValid`, `covered`, `exclusion`, `refundDkk`), so the gateways read one named variable and
  the external workers never have to read a serialized result object.
- **No intermediate milestone events.** The old model used throw events only to make decisions visible in the
  event log for the DCR rules. The decision is now the activity itself ("Decide exclusion", "Decide travel
  guarantee"), and the rejection reason is a variable of the instance.
- **Rejections are two tasks, "Reject claim (invalid)" and "Reject claim (not covered)".** A block-structured
  model cannot let two branches share one task, and the two rejections happen at different depths.

## Layout and pictures

The models are generated by `tools/build_to_be_bpmn.py` and `tools/build_to_be_dmn.py` on an explicit grid:
the main path runs left to right on one row, each alternative branch gets its own row above or below, and
every branch rejoins at its join gateway. In the DRD each decision sits above its own row of inputs, the
decisions made once per delay on the left and the ones made once per claim on the right. The pictures are
rendered by the bpmn-io viewers (`tools/render/render.mjs`), the same rendering Camunda Modeler uses, and the
landscape is a plain draw.io diagram in draw.io's default style, so every picture can be opened and changed in
a standard tool.

## Running it on the collected data

`uv run rejsegaranti replay` sends every stored delay and claim on the server through the TO-BE processes,
marked as reprocessed so nobody on the mailing list gets an email. The results page shows the AS-IS judgement
(the four AS-IS tables) and the TO-BE outcome of the same claims side by side at `/results/to-be`.

Result on a copy of the server's data taken on 18 November 2026 (299 real delays from 7 to 18 November,
756 simulated claims), on a local Camunda:

| | TO-BE paid | TO-BE rejected | Total |
|---|---|---|---|
| AS-IS paid | 713 | 21 | 734 |
| AS-IS rejected | 0 | 22 | 22 |
| Total | 713 | 43 | 756 |

- **TO-BE:** 713 paid (158,258 kr), 43 rejected: 22 "No verified delay" and 21 "Receipt already claimed".
  0 incidents, and running the replay twice gives the same result.
- **The 21 claims decided differently are all reused receipts.** The AS-IS tables have no rule for a receipt
  that was already refunded, so the AS-IS pays them; the TO-BE rejects them in "Claim valid" (change 8).
- **The 22 "No verified delay" claims are rejected by both.** Their trip matches no recorded delay.
- **No delay was excluded,** so the exclusion rules made no difference on this data. In those 12 days no
  planned-change notice arrived, there was no DMI warning, the operators sent no cause codes, and no weather
  reading reached a limit. The exclusion rows are tested by the decision-table cases instead, not by the data.
- **Received time:** locally the claims have no AS-IS-era Camunda instance, so the claim counts as received at
  the trip start and none is late. On the server the replay takes the time the claim first reached Camunda.
- **An appeal was tested by hand:** a rejected claim was appealed, a second appeal on it was refused (409), the
  case worker upheld it in Tasklist, and the claim became paid at 300 kr with no incident.
- **A limit of the simulated claims:** they were generated for Line L and include shared bikes, shared
  scooters and private cars. Movia's tables only ask whether a taxi receipt is attached, so both models pay
  them. The simulator should be changed to make taxi claims only.

## What is still not automated or not real

- The claims are simulated; the delays and weather are real (see `deploy/README.md`).
- "Notify affected passengers" is simulated: Movia would need the passengers on the bus, for example from
  Rejsekort check-ins, which we do not have.
- Only delays are detected. "Bus passed the stop", "train did not stop" and "left over 2 minutes early" are in
  the tables, but the real-time feed cannot show them, so they only come from what the passenger reports.
- The bank transfer is simulated (`PAY-<claimId>`).
- The TO-BE judges one incident per claim. The AS-IS judges each leg of a trip separately (a multi-instance
  task), which the TO-BE does not do yet: every collected claim is a single delayed leg, so the replay cannot
  show the difference, and a per-leg loop would need a multi-instance task, which processdoctor cannot read.
  A passenger with a multi-leg trip makes one claim per leg.
- The weather limits are the group's proposal (see "Force majeure thresholds" below).

## Force majeure thresholds

- **First choice, DMI warnings:** level 2 ("Farligt vejr", may disrupt traffic) or level 3 counts. Level 1
  ("Voldsomt vejr") does not, and a level 1 warning wins over the readings below.
- **Fallback, OpenWeatherMap** (only without a DMI warning): wind speed at least 24.5 m/s (Beaufort 10,
  storm), gusts at least 32.7 m/s (Beaufort 12), rain at least 30 mm in an hour (DMI's cloudburst is 15 mm in
  30 minutes; OpenWeatherMap only gives hourly totals), snow at least 5 mm water equivalent in an hour.

These are rows of the "Cause of disruption" table (hit policy First, because the readings can overlap).
