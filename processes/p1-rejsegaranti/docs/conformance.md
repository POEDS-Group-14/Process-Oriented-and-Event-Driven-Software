# Process 1 – Event log and conformance check (issue #11)

Author: Liam Palmqvist

## Status

| Step | Status |
|---|---|
| Export of the event log from Camunda history (`python3 -m rejsegaranti export`) | Built. Not yet run against a live engine |
| Real Line L delay data | **Blocked**: Rejseplanen API 2.0 access is on request only. Until access is granted, delays are simulated |
| Local pre-check against the DCR rules (`python3 -m rejsegaranti check`) | Built and tested |
| Check in processdoctor.dtu.dk | To do (manual upload) |
| Deviation analysis for the report | Template below. Needs the real-data run |

## How the log is built

- One trace per claim (process `rejsegaranti-claim`). The case id is the business key (`claimId`).
- Events are completed service and send tasks, milestone events, and start and end events. Gateways are left out.
- Timestamps are the activities' end times in Camunda.
- Trace attributes: `delayId`, `outcome`, `payoutDkk`, `rejectionReason`, `exclusionReason`.
- The delay-case log (`delay-cases-camunda.xes`) is exported too, for the lead-time and coverage analysis.

## Steps

```bash
# 1. Camunda + bridge running (see README), then send data
python3 -m rejsegaranti simulate --post                       # simulated delays
python3 -m rejsegaranti simulate --post --delays recorded.csv # once real delays exist
# 2. Export
python3 -m rejsegaranti export          # -> out/claims-camunda.xes / .csv
# 3. Pre-check
python3 -m rejsegaranti check ../out/claims-camunda.xes -v
# 4. Upload models/rejsegaranti-rules.dcr.xml and out/claims-camunda.xes to processdoctor.dtu.dk
```

## Dry run on simulated data (no engine)

`simulate --offline` runs the same rule code as the workers, walks the BPMN paths, and writes a log with the same activity names. Seed 14, 40 simulated delays (37 of them 30+ minutes), 116 simulated claims. 6 of those claims were refused at the claim form for an incomplete receipt and so never became cases, which leaves 110 traces.

| Log | Traces | Variants | Violating traces | Fitness (share of conforming traces) |
|---|---|---|---|---|
| `claims-offline.xes` | 110 | 9 | 0 | 1.000 |
| `claims-offline-noise10.xes` (about 10% deliberately broken traces) | 110 | 14 | 7 | 0.936 |

All 7 injected deviations were caught:

- 4 × no outcome reported (R7)
- 2 × payout despite a notice by the day before (R2)
- 1 × double payout (R6)

**These figures are from simulated data and only show that the pipeline and the rules work.** They must not be reported as findings about Line L.

Outcomes in the clean simulated log: 61 paid and 49 rejected. Rejection reasons:

- 14 had no verified 30+ minute delay
- 10 had a notice published by the day before
- 15 were force majeure (DMI warning, OWM thresholds or cause code)
- 6 had receipt problems (private transport, duplicate)
- 4 failed the 250 m rule

## Deviation analysis (to fill in after the real-data run)

What to look for in the Camunda log:

- **Incidents:** claims whose delay case was never assessed (match retries ran out), or duplicate payouts stopped at "Pay out claim". These show up as unfinished traces.
- **No verified delay:** compare with the CEP's raw delay stream. Is the 30-minute threshold or the match window (planned departure up to delay + 60 min) too strict?
- **Notice exclusions:** depend on the first-seen time from the recorder (process 6). Note the limitation that this is not the real publication time.
- **Force majeure:** how often it is DMI-based versus OWM-based, and whether the OWM thresholds ever trigger without a DMI warning.

## Limitations to state in the report

- Claims are simulated. There is no public claims data.
- Station coordinates are approximate (±100 m) until they are checked against OpenStreetMap or Rejseplanen stop data. That matters for a 250 m rule.
- Notice timing uses the first time our recorder saw the notice, not when it was published.
