# Process 1 – Rejsegaranti claim: AS-IS (issue #7)

Author: Liam Palmqvist · Notation: BPMN · Model: `models/rejsegaranti-as-is.bpmn` (+ `.pdf`, `.svg`)

## The rules today

Source: [dinletbane.dk – Rejsegaranti](https://dinletbane.dk/da/kontakt-og-kundeservice/rejsegaranti/), read 31 October 2026.

| Rule | What the operator says |
|---|---|
| Trigger | Line L is announced 30 minutes or more late |
| Covered transport | Taxi, shared bike, e-scooter or shared car. Must be publicly available, owned by a named VAT-registered company with a CVR number, and approved for commercial passenger transport |
| Payout | The passenger pays first; refund up to **300 kr** |
| Distance | Start within **250 m** of a Line L station hit by the stoppage; end within 250 m of the nearest Line L station from which the journey can continue |
| Receipt | Must state start, end, distance, time, number of minutes and amount in DKK |
| Exclusion 1 | Planned changes announced in good time, i.e. **at the latest the day before** |
| Exclusion 2 | Force majeure: events the operator cannot influence, avert or foresee, such as fire, unusual natural events and person collisions |

## The model

Two pools:

- **Passenger**: notices the delay, takes alternative transport, pays and keeps the receipt, fills in the online form, then waits. Either the operator asks for missing details (loop) or the decision arrives.
- **Hovedstadens Letbane, customer service**: every check is manual (user tasks), in this order:
  1. look up the delay in the operations log (30+ min?)
  2. check notices of planned changes (announced by the day before?)
  3. assess force majeure
  4. check the receipt; if details are missing, ask the passenger and wait
  5. check that start and end are within 250 m of a station
  6. calculate the refund (max 300 kr) and transfer it, or write a rejection
  7. send the decision to the passenger

## Assumptions to confirm

The website describes the rules, not the internal handling. These parts of the model are our assumptions and should be checked with the operator or the TAs:

- the order of the checks (delay first, receipt later)
- that the delay and notices are checked by hand against an operations log
- the "ask for missing details" loop
- that the refund and rejection go out as one "send decision" step

## Weak points that the TO-BE addresses

- Each claim repeats the same delay, notice and force-majeure checks for the same incident.
- The force-majeure judgement is made case by case, so decisions can differ.
- Incomplete receipts cause a back-and-forth with the passenger.
- The 250 m check is done by hand from addresses on the receipt.
- Nobody outside customer service learns the outcomes (no feedback to monitoring).
