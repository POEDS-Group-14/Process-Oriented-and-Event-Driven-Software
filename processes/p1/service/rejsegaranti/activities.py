"""Process keys, activity names and external-task topics of the TO-BE models (models/to-be/).

Change a name here and regenerate the models with tools/build_to_be_bpmn.py; the tests check that the
models and this file agree. The activity names are also the event names in the exported logs, and the
task labels processdoctor.compute.dtu.dk reads.
"""

# Assess disruption: one instance per delay, started by the delay monitor (CEP)
ASSESS_KEY = "tobe-assess-disruption"
DELAY_REPORTED = "Delay reported"
COLLECT_DISRUPTION = "Collect disruption data"
DECIDE_EXCLUSION = "Decide exclusion"
NOTIFY_PASSENGERS = "Notify affected passengers"
PUBLISH_ASSESSMENT = "Publish disruption assessment"
DISRUPTION_ASSESSED = "Disruption assessed"

# Decide claim and pay out: one instance per claim
CLAIM_KEY = "tobe-decide-claim"
CLAIM_SUBMITTED = "Claim submitted"
CHECK_VALIDITY = "Check claim validity"
MATCH_CLAIM = "Match claim to disruption"
DECIDE_GUARANTEE = "Decide travel guarantee"
CALC_REFUND = "Calculate refund"
PAY_OUT = "Pay out refund"
REJECT_INVALID = "Reject claim (invalid)"
REJECT_NOT_COVERED = "Reject claim (not covered)"
SEND_DECISION = "Send decision to passenger"
CLAIM_DECIDED = "Claim decided"

# Handle appeal: one instance per appeal against a decision
APPEAL_KEY = "tobe-handle-appeal"
APPEAL_RECEIVED = "Appeal received"
RETRIEVE_CLAIM = "Retrieve claim and evidence"
REVIEW_APPEAL = "Review appeal"
SEND_APPEAL_DECISION = "Send appeal decision"
APPEAL_DECIDED = "Appeal decided"

# Messages that start the processes
DELAY_MESSAGE = "DelayReported"
CLAIM_MESSAGE = "ClaimSubmitted"
APPEAL_MESSAGE = "AppealSubmitted"

# External-task topics (service tasks); the business rule tasks call the ToBe_ decisions directly
TOPICS = {
    COLLECT_DISRUPTION: "tb-collect-disruption",
    NOTIFY_PASSENGERS: "tb-notify-passengers",
    PUBLISH_ASSESSMENT: "tb-publish-assessment",
    MATCH_CLAIM: "tb-match-claim",
    PAY_OUT: "tb-pay-out",
    REJECT_INVALID: "tb-reject",
    REJECT_NOT_COVERED: "tb-reject",
    SEND_DECISION: "tb-send-decision",
    RETRIEVE_CLAIM: "tb-retrieve-claim",
    SEND_APPEAL_DECISION: "tb-send-appeal-decision",
}

# Who performs each activity, for the org:resource attribute of the exported logs
ROLES = {
    **{a: "Claims system" for a in (COLLECT_DISRUPTION, DECIDE_EXCLUSION, NOTIFY_PASSENGERS, PUBLISH_ASSESSMENT,
                                    CHECK_VALIDITY, MATCH_CLAIM, DECIDE_GUARANTEE, CALC_REFUND, REJECT_INVALID,
                                    REJECT_NOT_COVERED, SEND_DECISION, RETRIEVE_CLAIM, SEND_APPEAL_DECISION)},
    PAY_OUT: "Finance",
    REVIEW_APPEAL: "Case worker",
}
