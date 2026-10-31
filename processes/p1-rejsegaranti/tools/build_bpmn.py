"""Generate the AS-IS and TO-BE BPMN models for process 1 (rejsegaranti).

  uv run --project service python tools/build_bpmn.py        -> models/rejsegaranti-as-is.bpmn
                                        models/rejsegaranti-to-be.bpmn

Both files open in Camunda Modeler. If you edit a model in the Modeler,
stop regenerating it (or port the change here) so edits are not lost.
Activity names must match service/rejsegaranti/activities.py.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))
sys.path.insert(0, os.path.dirname(__file__))

from rejsegaranti import activities as A  # noqa: E402
from bpmn_layout import Process, document  # noqa: E402

AUTHOR = "Liam Palmqvist"
OUT = os.path.join(os.path.dirname(__file__), "..", "models")


def ext(topic: str) -> str:
    return f' camunda:type="external" camunda:topic="{topic}"'


HTTL = ' camunda:historyTimeToLive="180"'


# --------------------------------------------------------------------------
# TO-BE
# --------------------------------------------------------------------------

def to_be() -> str:
    W = 2060
    # ---- delay case pool ------------------------------------------------
    Pt = 110
    yT, E, M, P = Pt + 55, Pt + 120, Pt + 195, Pt + 300
    d = Process("rejsegaranti-delay-case", "Rejsegaranti delay case (started by the CEP)", True,
                (100, Pt, W, 370), extra_attrs=HTTL)
    d.add("D_Start", "startEvent", A.DELAY_DETECTED, 190, M,
          body='<bpmn:messageEventDefinition id="D_StartDef" messageRef="Msg_Delay" />')
    d.add("D_Fork", "parallelGateway", "", 280, M)
    d.add("D_CheckNotice", "serviceTask", A.CHECK_NOTICE, 390, M, ext("rg-check-notice"))
    d.add("D_CheckFM", "serviceTask", A.CHECK_FORCE_MAJEURE, 390, P, ext("rg-check-force-majeure"))
    d.add("D_Join", "parallelGateway", "", 500, M, ' camunda:asyncBefore="true"')
    d.add("D_GwNotice", "exclusiveGateway", "Announced by the day before?", 600, M, ' default="D_f_notice_no"')
    d.add("D_EvNotice", "intermediateThrowEvent", A.NOTICE_BY_DAY_BEFORE, 600, E, label="left")
    d.add("D_GwFM", "exclusiveGateway", "Force majeure?", 740, M, ' default="D_f_fm_no"')
    d.add("D_EvFM", "intermediateThrowEvent", A.FORCE_MAJEURE, 740, E, label="left")
    d.add("D_EvApplies", "intermediateThrowEvent", A.GUARANTEE_APPLIES, 860, M)
    d.add("D_Merge", "exclusiveGateway", "", 960, M)
    d.add("D_Report", "sendTask", A.REPORT_ASSESSMENT, 1070, M, ext("rg-report-assessment"))
    d.add("D_End", "endEvent", A.DELAY_CASE_DONE, 1180, M)
    d.flow("D_Start", "D_Fork")
    d.flow("D_Fork", "D_CheckNotice")
    d.flow("D_Fork", "D_CheckFM")
    d.flow("D_CheckNotice", "D_Join")
    d.flow("D_CheckFM", "D_Join")
    d.flow("D_Join", "D_GwNotice")
    d.flow("D_GwNotice", "D_EvNotice", "yes", "${noticeExcluded}")
    d.flow("D_GwNotice", "D_GwFM", "no", id="D_f_notice_no")
    d.flow("D_GwFM", "D_EvFM", "yes", "${forceMajeure}")
    d.flow("D_GwFM", "D_EvApplies", "no", id="D_f_fm_no")
    d.flow("D_EvNotice", "D_Merge", wp=[(600, E - 18), (600, yT), (960, yT), (960, M - 25)])
    d.flow("D_EvFM", "D_Merge", wp=[(740, E - 18), (740, yT), (960, yT), (960, M - 25)])
    d.flow("D_EvApplies", "D_Merge")
    d.flow("D_Merge", "D_Report")
    d.flow("D_Report", "D_End")
    dM = M

    # ---- claim pool -----------------------------------------------------
    Pt = 510
    yT, E, M, P = Pt + 55, Pt + 120, Pt + 195, Pt + 300
    c = Process("rejsegaranti-claim", "Rejsegaranti claim handling", True, (100, Pt, W, 370), extra_attrs=HTTL)
    c.add("C_Start", "startEvent", A.CLAIM_SUBMITTED, 190, M,
          body='<bpmn:messageEventDefinition id="C_StartDef" messageRef="Msg_Claim" />')
    c.add("C_Match", "serviceTask", A.MATCH_DELAY, 300, M, ext("rg-match-delay"))
    c.add("C_GwDelay", "exclusiveGateway", "Delay verified?", 420, M, ' default="C_f_delay_yes"')
    c.add("C_EvNoDelay", "intermediateThrowEvent", A.NO_VERIFIED_DELAY, 420, E, label="left")
    c.add("C_GwNotice", "exclusiveGateway", "Announced by the day before?", 560, M, ' default="C_f_notice_no"')
    c.add("C_EvNotice", "intermediateThrowEvent", A.NOTICE_BY_DAY_BEFORE, 560, E, label="left")
    c.add("C_GwFM", "exclusiveGateway", "Force majeure?", 700, M, ' default="C_f_fm_no"')
    c.add("C_EvFM", "intermediateThrowEvent", A.FORCE_MAJEURE, 700, E, label="left")
    c.add("C_EvDelayOK", "intermediateThrowEvent", A.DELAY_VERIFIED, 800, M)
    c.add("C_Fork", "parallelGateway", "", 880, M)
    c.add("C_CheckReceipt", "serviceTask", A.CHECK_RECEIPT, 990, M, ext("rg-check-receipt"))
    c.add("C_CheckDistance", "serviceTask", A.CHECK_DISTANCE, 990, P, ext("rg-check-distance"))
    c.add("C_Join", "parallelGateway", "", 1100, M, ' camunda:asyncBefore="true"')
    c.add("C_GwReceipt", "exclusiveGateway", "Receipt valid?", 1190, M, ' default="C_f_receipt_yes"')
    c.add("C_EvReceiptBad", "intermediateThrowEvent", A.RECEIPT_REJECTED, 1190, E, label="left")
    c.add("C_EvReceiptOK", "intermediateThrowEvent", A.RECEIPT_VERIFIED, 1290, M)
    c.add("C_GwDist", "exclusiveGateway", "Within 250 m?", 1390, M, ' default="C_f_dist_yes"')
    c.add("C_EvFar", "intermediateThrowEvent", A.TRIP_TOO_FAR, 1390, E, label="left")
    c.add("C_EvNear", "intermediateThrowEvent", A.TRIP_WITHIN, 1490, M)
    c.add("C_Calc", "serviceTask", A.CALC_PAYOUT, 1590, M, ext("rg-calc-payout"))
    c.add("C_Pay", "serviceTask", A.PAY_OUT, 1720, M, ext("rg-pay-out"))
    c.add("C_RejMerge", "exclusiveGateway", "", 1720, yT)
    c.add("C_Reject", "serviceTask", A.REJECT, 1840, yT, ext("rg-reject"))
    c.add("C_OutMerge", "exclusiveGateway", "", 1840, M)
    c.add("C_Report", "sendTask", A.REPORT_OUTCOME, 1970, M, ext("rg-report-outcome"))
    c.add("C_End", "endEvent", A.CLAIM_CLOSED, 2080, M)
    c.flow("C_Start", "C_Match")
    c.flow("C_Match", "C_GwDelay")
    c.flow("C_GwDelay", "C_EvNoDelay", "no", "${!delayVerified}")
    c.flow("C_GwDelay", "C_GwNotice", "yes", id="C_f_delay_yes")
    c.flow("C_GwNotice", "C_EvNotice", "yes", "${noticeExcluded}")
    c.flow("C_GwNotice", "C_GwFM", "no", id="C_f_notice_no")
    c.flow("C_GwFM", "C_EvFM", "yes", "${forceMajeure}")
    c.flow("C_GwFM", "C_EvDelayOK", "no", id="C_f_fm_no")
    c.flow("C_EvDelayOK", "C_Fork")
    c.flow("C_Fork", "C_CheckReceipt")
    c.flow("C_Fork", "C_CheckDistance")
    c.flow("C_CheckReceipt", "C_Join")
    c.flow("C_CheckDistance", "C_Join")
    c.flow("C_Join", "C_GwReceipt")
    c.flow("C_GwReceipt", "C_EvReceiptBad", "no", "${!receiptValid}")
    c.flow("C_GwReceipt", "C_EvReceiptOK", "yes", id="C_f_receipt_yes")
    c.flow("C_EvReceiptOK", "C_GwDist")
    c.flow("C_GwDist", "C_EvFar", "no", "${!withinRadius}")
    c.flow("C_GwDist", "C_EvNear", "yes", id="C_f_dist_yes")
    c.flow("C_EvNear", "C_Calc")
    c.flow("C_Calc", "C_Pay")
    c.flow("C_Pay", "C_OutMerge")
    for ev, x in (("C_EvNoDelay", 420), ("C_EvNotice", 560), ("C_EvFM", 700),
                  ("C_EvReceiptBad", 1190), ("C_EvFar", 1390)):
        c.flow(ev, "C_RejMerge", wp=[(x, E - 18), (x, yT), (1695, yT)])
    c.flow("C_RejMerge", "C_Reject")
    c.flow("C_Reject", "C_OutMerge")
    c.flow("C_OutMerge", "C_Report")
    c.flow("C_Report", "C_End")

    cep = ("Participant_CEP", "CEP (Siddhi agents, issue #37)", (100, 20, W, 60))
    pas = ("Participant_Passenger", "Passenger (claim form)", (100, 910, W, 60))
    mflows = [
        ("MF_Delay", "Line L delay 30+ min", "Participant_CEP", "D_Start", [(190, 80), (190, dM - 18)]),
        ("MF_Assessed", "DelayAssessed", "D_Report", "Participant_CEP", [(1070, dM - 40), (1070, 80)]),
        ("MF_Outcome", "ClaimPaid / ClaimRejected", "C_Report", "Participant_CEP", [(1970, M - 40), (1970, 80)]),
        ("MF_Claim", "claim with receipt", "Participant_Passenger", "C_Start", [(190, 910), (190, M + 18)]),
        ("MF_Refund", "refund", "C_Pay", "Participant_Passenger", [(1720, M + 40), (1720, 910)]),
        ("MF_Rejection", "rejection with reason", "C_Reject", "Participant_Passenger",
         [(1890, yT), (1895, yT), (1895, 910)]),
    ]
    notes = [("Annotation_Author",
              f"Process 1 Rejsegaranti, TO-BE. Author: {AUTHOR}. Scope: Line L. "
              "Situations (delays, notices, weather) come from the CEP; claims are simulated.",
              (1400, 140, 420, 60))]
    return document("Definitions_rejsegaranti_to_be", [d, c], collapsed=[cep, pas], message_flows=mflows,
                    messages=[("Msg_Delay", "DelaySituationDetected"), ("Msg_Claim", "ClaimSubmitted")],
                    doc=f"Rejsegaranti TO-BE, author {AUTHOR}", annotations=notes)


# --------------------------------------------------------------------------
# AS-IS
# --------------------------------------------------------------------------

def as_is() -> str:
    W = 2240
    yP1, yP2 = 80, 170
    p = Process("rejsegaranti-as-is-passenger", "Passenger", False, (100, 20, W, 230))
    p.add("P_Start", "startEvent", "Line L 30+ minutes late", 180, yP2)
    p.add("P_Taxi", "task", "Take taxi, shared bike or shared car", 300, yP2)
    p.add("P_Receipt", "task", "Pay and keep the receipt", 430, yP2)
    p.add("P_Form", "sendTask", "Fill in rejsegaranti form with receipt", 560, yP2)
    p.add("P_Merge", "exclusiveGateway", "", 650, yP2)
    p.add("P_Wait", "eventBasedGateway", "", 730, yP2)
    p.add("P_Ask", "intermediateCatchEvent", "Asked for missing details", 800, yP1,
          body='<bpmn:messageEventDefinition id="P_AskDef" />')
    p.add("P_Send", "sendTask", "Send missing receipt details", 910, yP1)
    p.add("P_Decision", "intermediateCatchEvent", "Decision received", 860, yP2,
          body='<bpmn:messageEventDefinition id="P_DecisionDef" />')
    p.add("P_End", "endEvent", "Claim settled", 970, yP2)
    p.flow("P_Start", "P_Taxi")
    p.flow("P_Taxi", "P_Receipt")
    p.flow("P_Receipt", "P_Form")
    p.flow("P_Form", "P_Merge")
    p.flow("P_Merge", "P_Wait")
    p.flow("P_Wait", "P_Ask")
    p.flow("P_Wait", "P_Decision")
    p.flow("P_Ask", "P_Send")
    p.flow("P_Send", "P_Merge", wp=[(910, yP1 - 40), (910, 30), (650, 30), (650, yP2 - 25)])
    p.flow("P_Decision", "P_End")

    yL, yM, yB = 370, 490, 610
    cs = Process("rejsegaranti-as-is-customer-service", "Hovedstadens Letbane, customer service", False,
                 (100, 300, W, 360))
    cs.add("S_Start", "startEvent", "Claim received", 560, yM, body='<bpmn:messageEventDefinition id="S_StartDef" />')
    cs.add("S_Delay", "userTask", "Look up the delay in the operations log", 680, yM)
    cs.add("S_GwDelay", "exclusiveGateway", "Delay of 30+ minutes?", 800, yM, ' default="S_f_delay_yes"')
    cs.add("S_Notice", "userTask", "Check notices of planned changes", 910, yM)
    cs.add("S_GwNotice", "exclusiveGateway", "Announced by the day before?", 1030, yM, ' default="S_f_notice_no"')
    cs.add("S_FM", "userTask", "Assess force majeure", 1140, yM)
    cs.add("S_GwFM", "exclusiveGateway", "Force majeure?", 1240, yM, ' default="S_f_fm_no"')
    cs.add("S_Receipt", "userTask", "Check the receipt", 1350, yM)
    cs.add("S_GwReceipt", "exclusiveGateway", "Receipt complete?", 1470, yM, ' default="S_f_receipt_yes"')
    cs.add("S_Ask", "sendTask", "Ask passenger for missing details", 1470, yL)
    cs.add("S_Details", "intermediateCatchEvent", "Details received", 1350, yL,
           body='<bpmn:messageEventDefinition id="S_DetailsDef" />')
    cs.add("S_Dist", "userTask", "Check start and end within 250 m of a station", 1590, yM)
    cs.add("S_GwDist", "exclusiveGateway", "Within 250 m?", 1710, yM, ' default="S_f_dist_yes"')
    cs.add("S_Calc", "userTask", "Calculate refund (max 300 kr)", 1830, yM)
    cs.add("S_Transfer", "userTask", "Transfer refund", 1960, yM)
    cs.add("S_Reject", "userTask", "Write rejection", 1960, yB)
    cs.add("S_OutMerge", "exclusiveGateway", "", 2080, yM)
    cs.add("S_Send", "sendTask", "Send decision to passenger", 2190, yM)
    cs.add("S_End", "endEvent", "Claim closed", 2290, yM)
    cs.flow("S_Start", "S_Delay")
    cs.flow("S_Delay", "S_GwDelay")
    cs.flow("S_GwDelay", "S_Notice", "yes", id="S_f_delay_yes")
    cs.flow("S_Notice", "S_GwNotice")
    cs.flow("S_GwNotice", "S_FM", "no", id="S_f_notice_no")
    cs.flow("S_FM", "S_GwFM")
    cs.flow("S_GwFM", "S_Receipt", "no", id="S_f_fm_no")
    cs.flow("S_Receipt", "S_GwReceipt")
    cs.flow("S_GwReceipt", "S_Ask", "no")
    cs.flow("S_Ask", "S_Details")
    cs.flow("S_Details", "S_Receipt")
    cs.flow("S_GwReceipt", "S_Dist", "yes", id="S_f_receipt_yes")
    cs.flow("S_Dist", "S_GwDist")
    cs.flow("S_GwDist", "S_Calc", "yes", id="S_f_dist_yes")
    cs.flow("S_Calc", "S_Transfer")
    cs.flow("S_Transfer", "S_OutMerge")
    for gw, x, label in (("S_GwDelay", 800, "no"), ("S_GwNotice", 1030, "yes"),
                         ("S_GwFM", 1240, "yes"), ("S_GwDist", 1710, "no")):
        cs.flow(gw, "S_Reject", label, wp=[(x, yM + 25), (x, yB), (1910, yB)])
    cs.flow("S_Reject", "S_OutMerge", wp=[(2010, yB), (2080, yB), (2080, yM + 25)])
    cs.flow("S_OutMerge", "S_Send")
    cs.flow("S_Send", "S_End")

    mflows = [
        ("MF_Form", "claim form + receipt", "P_Form", "S_Start", [(560, yP2 + 40), (560, yM - 18)]),
        ("MF_Ask", "request for details", "S_Ask", "P_Ask", [(1470, yL - 40), (1470, 275), (800, 275), (800, yP1 + 18)]),
        ("MF_Details", "missing details", "P_Send", "S_Details", [(910, yP1 + 40), (910, 260), (1350, 260), (1350, yL - 18)]),
        ("MF_Decision", "refund or rejection", "S_Send", "P_Decision", [(2190, yM - 40), (2190, 290), (860, 290), (860, yP2 + 18)]),
    ]
    notes = [("Annotation_Author",
              f"Process 1 Rejsegaranti, AS-IS (manual handling today). Author: {AUTHOR}. "
              "Rules: dinletbane.dk/da/kontakt-og-kundeservice/rejsegaranti",
              (1150, 60, 420, 60))]
    return document("Definitions_rejsegaranti_as_is", [p, cs], message_flows=mflows,
                    doc=f"Rejsegaranti AS-IS, author {AUTHOR}", annotations=notes)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, fn in (("rejsegaranti-as-is.bpmn", as_is), ("rejsegaranti-to-be.bpmn", to_be)):
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
            fh.write(fn())
        print("wrote", name)
