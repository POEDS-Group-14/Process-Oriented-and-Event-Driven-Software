# Process 1 – Interface between the CEP and the engine (for #37 and #38)

The bridge (`uv run rejsegaranti run`) listens on `http://localhost:8090`. Bodies are JSON. Siddhi's `@map(type='json')` wrapper `{"event": {...}}` is accepted as well.

## CEP → engine

| Endpoint | Sent by | Effect |
|---|---|---|
| `POST /cep/delay` | delay agent: delay ≥ 30 min on one of the process's lines | Starts `rejsegaranti-delay-case` once per `delayId`. Later re-emits update the stored situation but start nothing |
| `POST /cep/weather` | weather agent (#36) | Stored. Used by *Check force majeure* |
| `POST /cep/notice` | notice recorder / process 6 | Stored, keeping the earliest `firstSeen`. Used by *Check planned service notice* |
| `POST /claims` | claim form or simulator | Starts `rejsegaranti-claim`. Returns 422 with the missing fields if the receipt is incomplete |

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
- `cause` is optional: `fire`, `person_collision` or `natural_event` count as force majeure.

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
| `DelayAssessed` | `delayId`, `guaranteeApplies`, `noticeExcluded`, `forceMajeure`, `exclusionReason` |
| `ClaimPaid` / `ClaimRejected` | `claimId`, `delayId`, `outcome`, `payoutDkk`, `reason` |

All events also carry `eventType` and `emittedAt`.

## Siddhi sketch (for the CEP owner)

```sql
@App:name('RejsegarantiLink')

-- to the engine: the delay agent's output
@sink(type='http', publisher.url='http://localhost:8090/cep/delay', method='POST',
      headers="'Content-Type:application/json'", @map(type='json'))
define stream LineLDelaySituation (delayId string, line string, stops string,
    plannedDeparture string, delayMinutes double, detectedAt string, cause string);

-- from the engine: claim outcomes and delay assessments
-- (two event shapes on one endpoint, so allow missing attributes)
@source(type='http', receiver.url='http://0.0.0.0:8006/rejsegaranti', basic.auth.enabled='false',
        @map(type='json', fail.on.missing.attribute='false'))
define stream RejsegarantiEvent (eventType string, emittedAt string, delayId string,
    claimId string, outcome string, payoutDkk double, reason string,
    guaranteeApplies bool, exclusionReason string);

-- example: total paid out per delay, to watch the cost of a disruption
from RejsegarantiEvent[eventType == 'ClaimPaid']
select delayId, sum(payoutDkk) as paidDkk, count() as claims
group by delayId
insert into PayoutPerDelay;
```

Run the bridge with `CEP_URL=http://localhost:8006/rejsegaranti`.
