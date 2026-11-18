# Process 1 - Interface between the CEP and the engine (for #37 and #38)

The bridge (`uv run rejsegaranti run`) listens on `http://localhost:8090`. Bodies are JSON. Siddhi's `@map(type='json')` wrapper `{"event": {...}}` is accepted as well.

## CEP → engine

| Endpoint | Sent by | Effect |
|---|---|---|
| `POST /cep/delay` | delay agent: a delay on one of the process's lines (the recorder sends 20+ min) | Starts `tobe-assess-disruption` (message `DelayReported`) once per `delayId`. Later re-emits update the stored situation but start nothing. Delays on other lines get 202 and are ignored. Whether the delay is long enough is decided by the `ToBe_Incident` table (more than 20 min) |
| `POST /cep/weather` | weather agent (#36) | Stored. Read by *Collect disruption data* for the `ToBe_Cause` table |
| `POST /cep/notice` | notice recorder / process 6 | Stored, keeping the earliest `firstSeen`. Read by *Collect disruption data* (hours announced before the change) |

## Claims and appeals

| Endpoint | Sent by | Effect |
|---|---|---|
| `POST /claims` | claim form or simulator | Stamps `receivedAt` and starts `tobe-decide-claim` (message `ClaimSubmitted`). Needs `tripStart`, `startLat`, `startLon`, `endLat`, `endLon` (422 with the missing fields otherwise); the receipt is optional, a claim without one is rejected by the tables ("No receipt"). 409 if the `claimId` was submitted already |
| `POST /appeals` | the passenger | `{"claimId": "...", "reason": "..."}`. Starts `tobe-handle-appeal` (message `AppealSubmitted`, business key the claim id). 404 if the claim is unknown, 409 if it was appealed already |
| `POST /replay/reset`, `/replay/delay`, `/replay/claim` | `rejsegaranti replay`, on the same machine only | Back up the registry and clear the decisions, then start the instances again with `reprocessed = true` |

### Claim

```json
{"claimId": "C0001", "tripStart": "2026-11-04T07:20:00+01:00",
 "startLat": 55.77, "startLon": 12.50, "endLat": 55.73, "endLon": 12.47,
 "incident": "delay",
 "receipt": {"receiptId": "R00001", "provider": "Dantaxi 4x48", "transportType": "taxi",
             "time": "2026-11-04T07:20:00+01:00", "amountDkk": 180.0}}
```

- `incident` is optional (default `delay`); the other values of the `ToBe_Incident` table (`bus passed stop`,
  `train did not stop`, `left over 2 min early`, `missed connection`) can come with `visibleToDriver`, `busFull`,
  `stopRequested` and `minutesToNextDeparture`.
- The days to claim are counted from `tripStart` to `receivedAt` in Copenhagen calendar days.

### Delay situation

```json
{"delayId": "L-2026-11-04-0700-LYN", "line": "L",
 "stops": ["Lyngby", "Gammelmosevej", "Buddinge"],
 "plannedDeparture": "2026-11-04T07:00:00+01:00",
 "delayMinutes": 42, "detectedAt": "2026-11-04T07:30:00+01:00",
 "cause": null}
```

- `stops` can also be one comma-separated string.
- `line` is required when the bridge runs on several lines (`REJSEGARANTI_LINES`).
- Stop names must match the stations of that line in `service/rejsegaranti/stations.py` (Line L) or `data/stations.csv`.
- `cause` is optional: `strike`, `fire`, `person_collision` or `natural_event` reach the `ToBe_Cause` table; anything else counts as `none`.

### Weather situation

```json
{"situationId": "DMI-123", "source": "DMI", "level": 2, "type": "storm",
 "area": "Hovedstaden", "validFrom": "...", "validTo": "..."}
{"situationId": "OWM-2026-11-04T07", "source": "OWM",
 "metrics": {"wind_speed": 26.1, "wind_gust": 34.0, "rain_1h": 2.0, "snow_1h": 0},
 "validFrom": "...", "validTo": "..."}
```

### Notice (from process 6)

```json
{"noticeId": "rp-98765", "lines": ["L"], "stops": [],
 "effectiveFrom": "2026-11-04T05:00:00+01:00", "effectiveTo": "2026-11-04T12:00:00+01:00",
 "firstSeen": "2026-11-02T10:14:00+01:00"}
```

An empty `stops` list means the whole line.

## Engine → CEP

If `CEP_URL` is set, each event is POSTed there as `{"event": {...}}`. Every event is also written to `out/cep-events.jsonl`.

| eventType | Fields |
|---|---|
| `PassengersNotified` | `delayId`, `line`, `reprocessed` (simulated: there is no passenger registry) |
| `DelayAssessed` | `delayId`, `exclusion` (`none` when the guarantee can apply), `guaranteeApplies`, the collected inputs (`line`, `service`, `operatorCause`, `dmiLevel`, `windSpeed`, `windGust`, `rain1h`, `snow1h`, `hoursAnnouncedBefore`, `delayMinutes`), `reprocessed` |
| `ClaimPaid` / `ClaimRejected` | `claimId`, `delayId`, `outcome`, `payoutDkk`, `reason`, `reprocessed` |
| `AppealDecided` | `claimId`, `reason`, `upheld`, `reviewNote`, `refundDkk`, `previousOutcome`, `decidedAt` |

All events also carry `eventType` and `emittedAt`. `reprocessed` is true for instances started by
`rejsegaranti replay`; the mailing list sends no email for those.

## Siddhi sketch (for the CEP owner)

```sql
@App:name('RejsegarantiLink')

-- to the engine: the delay agent's output
@sink(type='http', publisher.url='http://localhost:8090/cep/delay', method='POST',
      headers="'Content-Type:application/json'", @map(type='json'))
define stream DelaySituation (delayId string, line string, stops string,
    plannedDeparture string, delayMinutes double, detectedAt string, cause string);

-- from the engine: claim outcomes and delay assessments
-- (two event shapes on one endpoint, so allow missing attributes)
@source(type='http', receiver.url='http://0.0.0.0:8006/rejsegaranti', basic.auth.enabled='false',
        @map(type='json', fail.on.missing.attribute='false'))
define stream RejsegarantiEvent (eventType string, emittedAt string, delayId string,
    claimId string, outcome string, payoutDkk double, reason string,
    guaranteeApplies bool, exclusion string, reprocessed bool);

-- example: total paid out per delay, to watch the cost of a disruption
from RejsegarantiEvent[eventType == 'ClaimPaid' and not reprocessed]
select delayId, sum(payoutDkk) as paidDkk, count() as claims
group by delayId
insert into PayoutPerDelay;
```

Run the bridge with `CEP_URL=http://localhost:8006/rejsegaranti`.
