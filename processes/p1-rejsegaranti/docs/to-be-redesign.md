# Process 1 – Rejsegaranti claim: TO-BE redesign (issue #8)

Author: Liam Palmqvist · Notation: BPMN (executable, Camunda 7) · Model: `models/rejsegaranti-to-be.bpmn` (+ `.pdf`, `.svg`)

## Overview

The TO-BE splits the work into two executable processes that share a situation registry:

1. **Delay case** (`rejsegaranti-delay-case`): one instance per Line L delay. The CEP's "delay over 30 minutes" situation starts it (message `DelaySituationDetected`). It checks the planned-service notices and force majeure in parallel, records whether the guarantee applies, and reports `DelayAssessed` back to the CEP.
2. **Claim handling** (`rejsegaranti-claim`): one instance per passenger claim (message `ClaimSubmitted`). It matches the claim to a CEP delay situation, takes the exclusion result from the delay case, checks the receipt and the 250 m rule in parallel, then pays (capped at 300 kr) or rejects. It reports `ClaimPaid` or `ClaimRejected` back to the CEP.

Milestone events (intermediate throw events such as *Receipt verified* or *Force majeure*) make each decision visible in the event log, so the DCR rules can be checked against it (issues #10 and #11).

## Changes, heuristics and quality measures

Heuristic names follow Reijers & Limam Mansar's best practices from the course. Issue #39 (shared quality measures) is still open; the measures below are my proposal and use the devil's quadrangle dimensions (time, cost, quality, flexibility).

| # | Change (AS-IS → TO-BE) | Heuristic | Quality measure it improves |
|---|---|---|---|
| 1 | Manual delay lookup → the CEP detects the 30+ minute delay and starts the delay case | Task automation; integral technology | **Time:** claim handling time. **Cost:** share of claims decided without a case worker |
| 2 | Delay, notice and force-majeure checks once per claim → once per delay, reused by every claim on that delay | Task elimination; case-based work (one assessment per incident) | **Cost:** checks per claim. **Quality:** consistency (all claims on one delay get the same exclusion decision) |
| 3 | Manual force-majeure judgement → DMI warning level 2+ ("farligt vejr"); OpenWeatherMap thresholds only when there is no DMI warning; operator cause codes (fire, person collision) | Task automation; standardisation of decision rules | **Quality:** consistency and traceability (the reason is stored with the decision) |
| 4 | Notice check by hand → automatic comparison of the notice's first-seen time with the day before the change (input from process 6) | Task automation; integral technology | **Time:** handling time. **Quality:** correct exclusions |
| 5 | Receipt and distance checked one after the other → in parallel | Parallelism | **Time:** throughput time |
| 6 | Delay and exclusions are checked before the receipt | Knock-out (cheap checks that reject the most come first) | **Cost:** work spent on claims that are rejected anyway |
| 7 | "Ask for missing details" loop → the claim form refuses incomplete receipts at submission (bridge returns 422 with the missing fields) | Contact reduction; control relocation (the passenger completes the check at intake) | **Time:** fewer waiting periods. **Quality:** passenger contacts per claim |
| 8 | 250 m check by hand → computed from GPS coordinates against the station list | Task automation | **Time** and **quality** (same rule for everyone) |
| 9 | Outcomes stay in customer service → `DelayAssessed`, `ClaimPaid` and `ClaimRejected` events go back to the CEP | Integral technology; knowledge transfer back to monitoring | **Flexibility/quality:** the CEP can monitor payouts per delay, spot duplicates and feed process 6 analysis |

### Proposed measures for #39

- **Claim handling time**: claim submitted → decision sent (median and 90th percentile)
- **Automation rate**: share of claims decided without a user task or incident
- **Decision consistency**: share of claims on the same delay that receive the same exclusion decision (100% by design in the TO-BE)
- **Passenger contacts per claim**: number of messages to the passenger before the decision

## Force-majeure thresholds (to justify in the report)

- **First choice, DMI warnings:** level 2 ("Farligt vejr", may disrupt traffic) or level 3 counts as force majeure. Level 1 ("Voldsomt vejr") does not.
- **Fallback, OpenWeatherMap** (only when no DMI warning covers the time). These are proposals:

| Metric | Threshold | Reasoning |
|---|---|---|
| `wind_speed` | ≥ 24.5 m/s | Beaufort 10, "storm" |
| `wind_gust` | ≥ 32.7 m/s | Beaufort 12, hurricane-force gusts |
| `rain_1h` | ≥ 30 mm | DMI defines a cloudburst (skybrud) as 15 mm in 30 minutes; OWM reports only 1-hour totals, so 30 mm/h is the same intensity sustained over the hour |
| `snow_1h` | ≥ 5 mm water equivalent | heavy snowfall |

The thresholds are in `service/rejsegaranti/rules.py` (`OWM_THRESHOLDS`, `MIN_DMI_LEVEL`). Note that with these rules, a DMI warning *below* level 2 means "not force majeure", even if OWM is above a threshold. That is deliberate: the official warning wins.

## What is still not automated

- Other force-majeure causes without a cause code (for example an accident reported only in free text) are not detected. They would need a manual review task.
- If two claims with the same receipt pass the receipt check at the same moment, the payout step stops with an incident in Camunda Cockpit for a case worker. Duplicates are otherwise rejected in the receipt check.
- The real payout (bank transfer) is simulated (`PAY-<claimId>` reference).
