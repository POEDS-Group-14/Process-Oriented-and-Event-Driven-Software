"""Tests with HAND-WRITTEN FORMAT EXAMPLES (not real Movia data). Replace or extend them with real
messages from raw/dataudveksleren/ once the feed is connected."""
import csv
import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from recorder import owm, siri
from recorder.__main__ import Recorder
from recorder.config import Config

T = datetime(2026, 11, 4, 7, 40, tzinfo=timezone.utc)

ET_XML = """<Siri xmlns="http://www.siri.org.uk/siri" version="2.0"><ServiceDelivery>
<ResponseTimestamp>2026-11-04T08:40:00+01:00</ResponseTimestamp>
<EstimatedTimetableDelivery><EstimatedJourneyVersionFrame>
 <EstimatedVehicleJourney>
  <LineRef>MOVIA:5C</LineRef><OperatorRef>MOVIA</OperatorRef>
  <FramedVehicleJourneyRef><DataFrameRef>2026-11-04</DataFrameRef><DatedVehicleJourneyRef>J1</DatedVehicleJourneyRef></FramedVehicleJourneyRef>
  <RecordedCalls>
   <RecordedCall><StopPointRef>S1</StopPointRef><StopPointName>Stop one</StopPointName>
    <AimedDepartureTime>2026-11-04T08:00:00+01:00</AimedDepartureTime><ActualDepartureTime>2026-11-04T07:56:30+01:00</ActualDepartureTime></RecordedCall>
   <RecordedCall><StopPointRef>S2</StopPointRef>
    <AimedDepartureTime>2026-11-04T08:10:00+01:00</AimedDepartureTime><ActualDepartureTime>2026-11-04T08:11:00+01:00</ActualDepartureTime></RecordedCall>
  </RecordedCalls>
  <EstimatedCalls>
   <EstimatedCall><StopPointRef>S3</StopPointRef>
    <AimedDepartureTime>2026-11-04T08:20:00+01:00</AimedDepartureTime><ExpectedDepartureTime>2026-11-04T08:45:00+01:00</ExpectedDepartureTime></EstimatedCall>
   <EstimatedCall><StopPointRef>S4</StopPointRef><Cancellation>true</Cancellation>
    <AimedDepartureTime>2026-11-04T08:30:00+01:00</AimedDepartureTime></EstimatedCall>
   <EstimatedCall><StopPointRef>S5</StopPointRef>
    <AimedArrivalTime>2026-11-04T08:40:00+01:00</AimedArrivalTime><ExpectedArrivalTime>2026-11-04T09:01:00+01:00</ExpectedArrivalTime></EstimatedCall>
  </EstimatedCalls>
 </EstimatedVehicleJourney>
</EstimatedJourneyVersionFrame></EstimatedTimetableDelivery></ServiceDelivery></Siri>"""

SX_XML = """<Siri xmlns="http://www.siri.org.uk/siri"><ServiceDelivery><SituationExchangeDelivery><Situations>
<PtSituationElement><CreationTime>2026-11-02T10:00:00+01:00</CreationTime><SituationNumber>SX-1</SituationNumber>
<Version>1</Version><Planned>true</Planned>
<ValidityPeriod><StartTime>2026-11-04T05:00:00+01:00</StartTime><EndTime>2026-11-04T12:00:00+01:00</EndTime></ValidityPeriod>
<MiscellaneousReason>roadworks</MiscellaneousReason><Summary>Line 5C diverted</Summary>
<Affects><Networks><AffectedNetwork><AffectedLine><LineRef>MOVIA:5C</LineRef></AffectedLine></AffectedNetwork></Networks>
<StopPoints><AffectedStopPoint><StopPointRef>S3</StopPointRef></AffectedStopPoint></StopPoints></Affects>
</PtSituationElement></Situations></SituationExchangeDelivery></ServiceDelivery></Siri>"""

# Shape from https://openweathermap.org/current (values made up)
OWM_JSON = {"coord": {"lon": 12.5683, "lat": 55.6761}, "weather": [{"main": "Rain", "description": "light rain"}],
            "main": {"temp": 7.2}, "wind": {"speed": 12.3, "gust": 19.0}, "rain": {"1h": 0.8}, "dt": 1793781600}


class SiriTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(siri.message_kind(ET_XML), "ET")
        self.assertEqual(siri.message_kind(SX_XML), "SX")
        self.assertEqual(siri.message_kind("<Siri/>"), "other")

    def test_et_incidents(self):
        rows = {r["stopRef"]: r for r in siri.parse_et(ET_XML, T)}
        self.assertEqual(rows["S1"]["type"], "early_departure")      # actual, 3.5 min early
        self.assertEqual(rows["S1"]["minutes"], -3.5)
        self.assertNotIn("S2", rows)                                 # 1 min late: nothing
        self.assertEqual(rows["S3"]["type"], "delay")                # predicted +25
        self.assertEqual(rows["S3"]["timeKind"], "expected")
        self.assertEqual(rows["S4"]["type"], "cancelled")
        self.assertEqual(rows["S5"]["type"], "delay")                # last stop: arrival +21
        self.assertEqual(rows["S5"]["minutes"], 21.0)
        self.assertEqual(rows["S3"]["journeyRef"], "2026-11-04:J1")

    def test_expected_early_is_not_an_incident(self):
        xml = ET_XML.replace("<ActualDepartureTime>2026-11-04T07:56:30+01:00</ActualDepartureTime>", "") \
                    .replace("<RecordedCall><StopPointRef>S1</StopPointRef>", "<RecordedCall><StopPointRef>S1</StopPointRef><ExpectedDepartureTime>2026-11-04T07:50:00+01:00</ExpectedDepartureTime>")
        self.assertNotIn("S1", {r["stopRef"] for r in siri.parse_et(xml, T)})

    def test_sx(self):
        (r,) = siri.parse_sx(SX_XML, T)
        self.assertEqual((r["situationNumber"], r["planned"], r["lines"], r["stops"], r["reason"]),
                         ("SX-1", "true", "MOVIA:5C", "S3", "roadworks"))
        self.assertEqual(r["validFrom"], "2026-11-04T05:00:00+01:00")


class OwmTest(unittest.TestCase):
    def test_row(self):
        r = owm.to_row(OWM_JSON, T)
        self.assertEqual((r["wind_speed"], r["wind_gust"], r["rain_1h"], r["snow_1h"]), (12.3, 19.0, 0.8, 0))


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["RECORDER_DATA_DIR"] = self.dir
        self.rec = Recorder(Config())

    def _csv(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_raw_kept_and_incidents_deduplicated(self):
        self.rec.handle_siri(ET_XML, {"messageId": "m1"})
        self.rec.handle_siri(ET_XML, {"messageId": "m2"})  # same message again: no new incident rows
        self.assertEqual(len(self._csv("movia-incidents.csv")), 4)
        raw_dir = os.path.join(self.dir, "raw", "dataudveksleren")
        (f,) = os.listdir(raw_dir)
        with gzip.open(os.path.join(raw_dir, f), "rt", encoding="utf-8") as fh:
            recs = [json.loads(l) for l in fh]
        self.assertEqual([r["meta"]["messageId"] for r in recs], ["m1", "m2"])
        self.assertEqual(recs[0]["payload"], ET_XML)

    def test_prediction_becoming_actual_is_written_again(self):
        self.rec.handle_siri(ET_XML, {})
        actual = ET_XML.replace("<ExpectedDepartureTime>2026-11-04T08:45:00+01:00</ExpectedDepartureTime>",
                                "<ActualDepartureTime>2026-11-04T08:46:00+01:00</ActualDepartureTime>")
        self.rec.handle_siri(actual, {})
        s3 = [r for r in self._csv("movia-incidents.csv") if r["stopRef"] == "S3"]
        self.assertEqual([r["timeKind"] for r in s3], ["expected", "actual"])

    def test_situation_first_seen_survives_restart(self):
        self.rec.handle_siri(SX_XML, {})
        first = self._csv("movia-situations.csv")[0]["firstSeen"]
        Recorder(Config()).handle_siri(SX_XML, {})
        self.assertEqual(self._csv("movia-situations.csv")[1]["firstSeen"], first)

    def _sql(self, q):
        import sqlite3
        db = sqlite3.connect(os.path.join(self.dir, "recorder.sqlite"))
        return db.execute(q).fetchall()

    def test_sqlite_gets_the_same_rows_without_duplicates(self):
        self.rec.handle_siri(ET_XML, {})
        self.rec.handle_siri(SX_XML, {})
        self.assertEqual(self._sql("SELECT count(*) FROM movia_incidents")[0][0], 4)
        self.assertEqual(self._sql("SELECT minutes, timeKind FROM movia_incidents WHERE stopRef='S3'"), [(25.0, "expected")])
        # a restarted recorder forgets its in-memory dedup, but the UNIQUE key still blocks duplicates
        Recorder(Config()).handle_siri(ET_XML, {})
        self.assertEqual(self._sql("SELECT count(*) FROM movia_incidents")[0][0], 4)
        self.assertEqual(self._sql("SELECT situationNumber, planned FROM movia_situations"), [("SX-1", "true")])

    def test_sqlite_weather(self):
        self.rec.store.rows("weather-owm.csv", owm.FIELDS, [owm.to_row(OWM_JSON, T)])
        self.rec.store.rows("weather-owm.csv", owm.FIELDS, [owm.to_row(OWM_JSON, T)])  # same observation twice
        self.assertEqual(self._sql("SELECT wind_speed, wind_gust, rain_1h, snow_1h FROM weather_owm"),
                         [(12.3, 19.0, 0.8, 0.0)])

    def test_garbage_message_is_stored_not_fatal(self):
        self.rec.handle_siri("not xml", {"messageId": "bad"})
        self.assertTrue(os.listdir(os.path.join(self.dir, "raw", "dataudveksleren")))


if __name__ == "__main__":
    unittest.main()
