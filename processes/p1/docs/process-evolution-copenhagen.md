# Process 1 evolution: from bus 150S to the Copenhagen network

Notes for the write-up of process 1 (rejsegaranti). Dates are 2026; times are UTC unless marked as Copenhagen time.

## 1. Context: the event-driven set-up

Process 1 is event-driven end to end:

1. **Event source.** Movia's and DSB S-tog's real-time feeds (SIRI-ET via Dataudveksleren) produce a stream of low-level events: one per journey and stop, with planned and expected or actual times. OpenWeatherMap observations are a second stream.
2. **Event processing (the CEP role).** The recorder folds the stop-level events of one journey into one complex event, a *delay situation* (line, journey, affected stops, delay in minutes), and forwards it with the weather situations.
3. **Process engine.** The bridge filters the situations by the business rule (a delay of 30+ minutes on a covered line) and correlates the message `DelaySituationDetected` to Camunda, which starts one *delay case* instance per delayed journey. Each simulated passenger claim starts a *claim* instance (message `ClaimSubmitted`), which is correlated to its delay case by time and place.
4. **Event log.** Camunda's history is exported as XES and checked against the DCR rules for conformance.

The process is designed for Line L (Hovedstadens Letbane). No real-time data is published for Line L, so the live system ran the same models and rules on Movia bus 150S, which shares Line L's DTU and Lundtofte stops. Delays are real; claims are simulated, because no public claims data exists.

## 2. What we observed

After 2.6 days of running (7 November 22:20 to 10 November 13:05, 62.75 hours), the engine had started no process instance at all, and so there was no event log. To find out why, we queried the recorder's store of the raw event stream rather than the engine:

| | Journeys 20+ min late | Journeys 30+ min late |
|---|---|---|
| All Movia and S-tog lines | 309 | 96 |
| The 79 lines serving Copenhagen | 181 | 42 (on 22 lines, 5 of them S-tog) |
| Bus 150S | 1 | 0 |

The worst 150S delay was 25 minutes, below the 30-minute rule.

**Diagnosis.** The event pipeline worked; the filter in front of the process was too narrow. The triggering event (a 30+ minute delay) is rare on any single line, so a process scoped to one line may not start for days. Across the Copenhagen network the same event occurred about 16 times a day (42 in 2.6 days).

## 3. The evolution

On 10 November the scope was widened from one line to every Movia bus and DSB S-tog line with at least one stop in Copenhagen: 79 lines, 4,764 stop points and 2,808 stations, taken from Rejseplanen's GTFS timetable. A line is included if any of its stops lies in a box around Copenhagen and Frederiksberg (55.60-55.76 N, 12.40-12.65 E).

The change was made in the event and data layers only:

| Layer | Before | After |
|---|---|---|
| Event source | Both feeds recorded | Unchanged |
| Event processing | Delay situations forwarded for 150S only | Forwarded for every line |
| Event filter (bridge) | Line = 150S, delay 30+ min | Line in the 79 Copenhagen lines, delay 30+ min |
| Correlation of claim to delay | Nearest station of 150S | Nearest station of *the delayed line* |
| Process models (BPMN, DCR) | | Unchanged |
| Business rules (30 min, 300 kr, 250 m, notices, force majeure) | | Unchanged |

Keeping the models and rules fixed means the event logs from before and after the change are produced by the same process, and the same DCR conformance check applies to both.

One adaptation was needed in the correlation logic. Station names repeat across lines (Nørreport is on many), so a claim is now matched only against the stations of the delayed line, and the 250 m checks measure to that line's stations. Without this, a claim could be correlated with a delay on a different line that happens to stop nearby.

## 4. Making the cases comparable across lines

With many lines in one log, raw case counts mostly reflect how often a line runs: 5C runs about 490 journeys a weekday, line 142 about 64. Cases are therefore normalised per 1,000 scheduled journeys:

> rate = cases / exposure x 1,000

- **Exposure** = the line's scheduled journeys per date (from the GTFS timetable) x the share of that date the system was recording.
- **Recording time** is read from the event stream itself: the weather observations arrive continuously, so the time from one observation to the next counts as recorded unless the gap exceeds 30 minutes.
- **Uncertainty:** each rate gets a 95% Poisson confidence interval (Byar's approximation; exact for zero cases).
- **Ranking:** lines are ranked by the lower end of the interval, so a quiet line with one unlucky journey does not top the list.

**Finding (method).** The first version counted each weather observation as 10 minutes of recording, but observations arrive every 15-20 minutes. That would have counted the system as down about 40% of the time and overstated every rate by about 1.7x. Correcting it gives 24 hours of recording for both 8 and 9 November, as expected.

The per-line rates, the claims with their outcomes, and the claim and delay-case event logs (XES, readable by ProcessDoctor) can be downloaded from the public results page.

## 5. The method we followed

1. **Monitor:** noticed that no instances or log existed after 2.6 days of running.
2. **Diagnose from event data:** compared the raw event stream with what reached the engine, and found the filter, not the pipeline, was the cause.
3. **Decide:** widened the scope to the Copenhagen network rather than lowering the 30-minute threshold, which would have changed the business rule itself.
4. **Change:** adapted the event filter and the correlation logic; kept the models and rules fixed.
5. **Verify:** automated tests (including one that a claim is checked against the delayed line's stations only), a simulated offline run on all lines replayed against the DCR rules (78 traces, 0 violating), and checks on the live server.
6. **Make the results comparable:** added the per-line normalisation, and corrected its measure of recording time after checking it against the real data.

## 6. Limitations

- The rules are Line L's, applied to buses and S-trains; Movia's own guarantee differs (it uses 20 minutes).
- Claims are simulated, about a fixed number per delay, so claims per line follow the delay rate and add no information about the line.
- Exposure uses scheduled journeys, not journeys that actually ran; cancelled journeys still count. The recorder only stores journeys that are 20+ minutes late, so actual journeys cannot be counted.
- The Copenhagen box is a judgement call; a different boundary changes which lines are in.
- Delays recorded before the change were not replayed, so the network-wide data starts on 10 November; rates are counted from 15:00 Copenhagen time that day.
- The BPMN, DCR and activity names still say Line L.

## 7. Status (10 November, 13:52)

The system runs on all 79 lines. No qualifying delay had occurred yet: since the switch the only journeys late enough were on lines 554 (51 min) and 182 (30 min), neither of which serves Copenhagen.
