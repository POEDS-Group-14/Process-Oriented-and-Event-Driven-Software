# Process 1 evolution: from bus 150S to the Copenhagen network

Notes for the essay. Dates are 2026; times are UTC unless marked as Copenhagen time.

## Background

The rejsegaranti (travel guarantee) process is designed for Line L (Hovedstadens Letbane). No real-time data is published for Line L, so the live system ran the same process and rules on Movia bus 150S instead. 150S was chosen because it shares Line L's DTU and Lundtofte stops and is a high-frequency line. Delays are real (Movia and DSB S-tog real-time data from Dataudveksleren); claims are simulated, because no public claims data exists. A case starts when a journey is announced 30 or more minutes late, the threshold of the Line L guarantee.

## Problem: the stand-in line produced no cases

In the first 2.6 days of recording (7 November 22:20 to 10 November 13:05, 62.75 hours), not one 150S journey ran 30+ minutes late. The worst 150S delay was 25 minutes. As a result the bridge received no delay, Camunda started no case, no claims were simulated, and there was no event log to analyse.

In the same period the recorder saw, across all lines in the feeds:

| | Journeys 20+ min late | Journeys 30+ min late |
|---|---|---|
| All Movia and S-tog lines | 309 | 96 |
| The 79 lines serving Copenhagen | 181 | 42 (on 22 lines, 5 of them S-tog) |
| Bus 150S | 1 | 0 |

**Finding:** delays of 30+ minutes are rare on any single line, so a process that depends on them needs a wide catchment to produce data. On the Copenhagen network they occur about 16 times a day (42 in 2.6 days); on one line, possibly never in several days.

## Change (decided 10 November)

The process now runs on every Movia bus and DSB S-tog line with at least one stop in Copenhagen: 79 lines, 4,764 stop points and 2,808 stations, taken from Rejseplanen's GTFS timetable feed.

- **Selection:** a line is included if any of its stops lies in a box around Copenhagen and Frederiksberg (55.60-55.76 N, 12.40-12.65 E). All of the line's stops are kept, also those outside the box.
- **Rules:** unchanged (30 minutes, 300 kr cap, 250 m from a station, planned-change notices, force majeure).
- **Rule adaptation:** station names repeat across lines (Nørreport is on many), so a claim is matched to a delay by the nearest station of the delayed line, and the 250 m checks measure to stations of that line only.
- **Data flow:** the recorder now forwards every delayed journey; the bridge decides which lines and delays qualify.

## Comparing lines: normalisation

Raw case counts favour busy lines: 5C runs about 490 journeys a weekday, line 142 about 64. Cases are therefore expressed per 1,000 scheduled journeys:

> rate = cases / exposure x 1,000

- **Exposure** = the line's scheduled journeys per date (from the GTFS timetable) x the share of that date the system was recording.
- **Recording time** is taken from the weather observations the recorder forwards: the time from one observation to the next counts as recorded, unless the gap exceeds 30 minutes.
- **Uncertainty:** each rate gets a 95% Poisson confidence interval (Byar's approximation; exact for zero cases).
- **Ranking:** lines are ranked by the lower end of the interval, so a quiet line with one unlucky journey does not top the list.

**Finding (method):** the first version counted each weather observation as 10 minutes of recording, but observations arrive every 15-20 minutes. That would have counted the system as down about 40% of the time and overstated every rate by about 1.7x. Correcting it gives 24 hours of recording for both 8 and 9 November, as expected.

## Getting the data

The public results page offers the data as downloads: claims with outcomes (CSV), per-line rates (CSV), and the claim and delay-case event logs (XES, exported from Camunda's history, readable by ProcessDoctor). No passenger IDs or coordinates are published.

## Limitations

- The rules are Line L's, applied to buses and S-trains; Movia's own guarantee differs (it uses 20 minutes).
- Claims are simulated, about a fixed number per delay, so claims per line follow the delay rate and add no information about the line.
- Exposure uses scheduled journeys, not journeys that actually ran; cancelled journeys still count. The recorder only stores journeys that are 20+ minutes late, so actual journeys cannot be counted.
- The Copenhagen box is a judgement call; a different boundary changes which lines are in.
- Delays recorded before the change were not replayed, so the network-wide data starts on 10 November; rates are counted from 15:00 Copenhagen time that day.
- The BPMN, DCR and activity names still say Line L.

## Status (10 November, 13:52)

The system runs on all 79 lines. No qualifying delay had occurred yet: since the switch the only journeys late enough were on lines 554 (51 min) and 182 (30 min), neither of which serves Copenhagen.
