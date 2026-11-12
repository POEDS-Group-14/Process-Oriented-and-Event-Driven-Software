# Process 1 - Travel guarantee claim: AS-IS (issue #7)

Author: Liam Palmqvist · Notation: BPMN and DMN · Operator: Movia (bus and local train)

## The models

All files are in `models/travel-guarantee/`.

| File | What it is |
|---|---|
| `travel-guarantee-process.bpmn` (+ `.png`) | **The AS-IS process:** Movia customer service handling a claim |
| `travel-guarantee.dmn`, `travel-guarantee-drd.png` | **The DRD** with its four decision tables |
| `table-1-claim-valid.png` ... `table-4-travel-guarantee-applies.png` | The four tables, numbered in the order they are used |
| `travel-guarantee.bpmn` (+ `.png`, `.svg`) | The first attempt: the same rules as a cascade of BPMN gateways (see "Why DMN") |
| `check_camunda.py`, `camunda-test-cases.json` | Deploys the DMN to Camunda 7 and checks every input combination (1,934 cases) |

## The process

One pool, *Movia customer service: travel guarantee claim*:

1. **Claim form received**, with the taxi receipt and the trip legs.
2. **Check claim validity** (business rule task, Table 1). An invalid claim ends in *Rejected: no receipt or too late*.
3. **Decide travel guarantee for each leg** (multi-instance business rule task, Tables 2 to 4, using the disruption data: cause and planned changes).
4. **Any leg covered?** If not, the claim ends in *Rejected: not covered*.
5. **Calculate refund**: the taxi fare, at most 300 kr per taxi. The cap is a calculation, so it is a task, not a table.
6. **Pay refund** and end in *Refund paid*.

The process is not executable yet: collecting the per-leg results into "Any leg covered?" still has to be built in Camunda.

## The decisions (DRD)

The tables are numbered in the order the process uses them. The DRD itself shows only which information each decision needs, not the order. All four tables use the Unique hit policy.

| Table | Decision | Inputs | Outcome |
|---|---|---|---|
| 1 | Claim valid | taxi receipt attached; days from the incident to the claim reaching Movia | Valid, or "No receipt" / "Claim too late" (more than 14 days) |
| 2 | Excluded by cause | service (bus, local train, Flextrafik); cause (none, strike, weather, force majeure); planned change announced 24 h before | none, or Flextrafik / Strike, weather or force majeure / Planned change |
| 3 | Incident qualifies | incident (delay over 20 min, bus passed stop, train did not stop, left over 2 min early, missed connection); visible to driver; bus full; stop requested in time; next departure within 20 min | Qualifies, or the reason it does not |
| 4 | Travel guarantee applies | the results of Tables 2 and 3 | Covered, or not covered with the reason. An exclusion always wins |

Knowledge sources: *Travel guarantee terms (Movia) [1]* for Tables 2 to 4, *Movia claim rules [2][3]* for Table 1.

## The rules

From Movia's travel guarantee terms [1] and claim rules [2]-[5]:

| Rule | Where it is modelled |
|---|---|
| Covered if the bus or local train is more than 20 minutes late (an actual delay, not a projected one) | Table 3 |
| Covered if the bus passes the stop although the passenger was clearly visible, the bus was not full, and the next bus is not due within 20 minutes | Table 3 |
| Covered if the local train does not stop although a stop was requested in time | Table 3 |
| Covered if the bus or train leaves more than 2 minutes early and the next one is not due within 20 minutes | Table 3 |
| Not covered: missed connections after a delay | Table 3 |
| Not covered: planned changes announced on dinoffentligetransport.dk 24 hours before departure | Table 2 |
| Not covered: bus strikes, weather that makes the timetable impossible, force majeure | Table 2 |
| Not covered: Flextrafik | Table 2 |
| Covers only the part of the trip the bus or local train should have covered; no other costs (flights, shows, lost earnings) | Table 4 (annotation), one decision per leg |
| The taxi receipt must be attached to the claim | Table 1 |
| The claim must reach Movia within 14 days of the incident | Table 1. Only found in sources from 2010 to 2022 [3][4][5] |
| Refund of at most 300 kr per taxi, however many people share it | Task "Calculate refund". Per-taxi wording only in [3] |

## Why DMN, not a gateway cascade

The first attempt (`travel-guarantee.bpmn`) put every rule in a chain of BPMN gateways. The rules turned out to be a set of decisions, not a sequence of activities, so they were moved into DMN tables. The process now has one gateway after each decision.

## Why one decision per leg

The first DMN version assessed the whole trip with one Incident input, so two incidents on one trip could not be modelled (for example a bus leaving 3 minutes early followed by a missed connection). The process now runs Tables 2 to 4 once per leg, with a multi-instance business rule task. Each leg has exactly one incident, the tables keep the Unique hit policy, and the guarantee is given per part of the trip, as the terms say. Allowing several incidents per assessment was rejected: it breaks Unique and makes it unclear which part of the trip is covered.

## Modelling checks (Lectures 7 and 8)

- **Soundness:** every XOR split carries on or ends in its own end event, so each claim ends in exactly one end event, and every activity can be reached.
- **No gateway cascades:** the rules are in the tables.
- **Structural consistency:** each business rule task calls a decision in the DRD.
- **Input data consistency:** the process shows where the DRD inputs come from (taxi receipt, claim form, disruption data).
- **Output and branch coverage:** both outcomes of Claim valid and of the leg assessment have a branch.
- **Decision tables:** every input combination matches exactly one rule (complete and unique), checked in Camunda with `check_camunda.py`.

## Assumptions and open points

- The 14-day deadline and the per-taxi wording of the 300 kr cap are only found in older sources. Check them against a current Movia page.
- [1] could not be loaded to check that its text matches the terms above.
- The claim form's fields (incident date, trip legs) are our assumption of what Movia asks for.

## Sources

[1] Din Offentlige Transport. *Rejsegaranti - søg erstatning ved forsinkelse.* https://dinoffentligetransport.dk/rejsegaranti (accessed 3 November 2026).
[2] Lokaltog A/S. *FAQ.* https://www.lokaltog.dk/alt-om-rejsen/faq/ (accessed 3 November 2026).
[3] Ankenævnet for Bus, Tog og Metro. *Afgørelse, journalnummer 2022-0042* (complaint against Trafikselskabet Movia). https://www.abtm.dk/assets/verdicts/XXAFG2022-0042_99133.pdf (accessed 3 November 2026).
[4] Din Avis. *Taxa eller erstatning efter 20 minutter.* 27 April 2010. https://dinavis.dk/samfund/ECE15120647/taxa-eller-erstatning-efter-20-minutter/ (accessed 3 November 2026).
[5] Din Avis. *Tag en taxa, hvis bussen ikke kommer.* 3 February 2011. https://dinavis.dk/samfund/ECE15347586/tag-en-taxa-hvis-bussen-ikke-kommer/ (accessed 3 November 2026).

## History: the Line L version

The first AS-IS modelled the Line L (Hovedstadens Letbane) rejsegaranti: 30 minutes late, trips within 250 m of a station, at most 300 kr, all checks done by hand. Line L data was not available, so the process moved to Movia, whose rules differ (20 minutes, no 250 m rule). The Line L model is kept in `models/archive/rejsegaranti-as-is.*` (generated by `tools/build_bpmn.py`).
