"""Activity names shared by the BPMN models, the event log and the DCR graph.

Change a name here and in tools/build_bpmn.py together; the tests check
that they agree.
"""

# rejsegaranti-delay-case (started by the CEP)
DELAY_DETECTED = "Line L delay of 30+ minutes detected"
CHECK_NOTICE = "Check planned service notice"
CHECK_FORCE_MAJEURE = "Check force majeure"
NOTICE_BY_DAY_BEFORE = "Planned change announced by the day before"
FORCE_MAJEURE = "Force majeure"
GUARANTEE_APPLIES = "Rejsegaranti applies"
REPORT_ASSESSMENT = "Report delay assessment to CEP"
DELAY_CASE_DONE = "Delay case assessed"

# rejsegaranti-claim (one instance per passenger claim)
CLAIM_SUBMITTED = "Claim submitted"
MATCH_DELAY = "Match claim to CEP delay situation"
NO_VERIFIED_DELAY = "No verified delay"
DELAY_VERIFIED = "Delay verified"
CHECK_RECEIPT = "Check receipt"
CHECK_DISTANCE = "Check distance to stations"
RECEIPT_REJECTED = "Receipt rejected"
RECEIPT_VERIFIED = "Receipt verified"
TRIP_TOO_FAR = "Trip outside 250 m"
TRIP_WITHIN = "Trip within 250 m"
CALC_PAYOUT = "Calculate payout"
PAY_OUT = "Pay out claim"
REJECT = "Reject claim"
REPORT_OUTCOME = "Report claim outcome to CEP"
CLAIM_CLOSED = "Claim closed"
