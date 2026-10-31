"""Tiny helper to write BPMN 2.0 XML with diagram interchange (DI).

Elements are placed by centre coordinates; sequence flows get orthogonal
waypoints automatically unless given explicitly.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

SIZE = {
    "task": (100, 80), "serviceTask": (100, 80), "sendTask": (100, 80), "userTask": (100, 80),
    "manualTask": (100, 80), "receiveTask": (100, 80),
    "startEvent": (36, 36), "endEvent": (36, 36), "intermediateThrowEvent": (36, 36),
    "intermediateCatchEvent": (36, 36),
    "exclusiveGateway": (50, 50), "parallelGateway": (50, 50), "eventBasedGateway": (50, 50),
}
GATEWAYS = {"exclusiveGateway", "parallelGateway", "eventBasedGateway"}


def q(s: str) -> str:
    return escape(s, {'"': "&quot;"})


class Node:
    def __init__(self, id, type, name, x, y, attrs="", body="", label="below"):
        self.id, self.type, self.name, self.x, self.y = id, type, name, x, y
        self.label = label
        self.attrs, self.body = attrs, body
        self.w, self.h = SIZE[type]
        self.incoming, self.outgoing = [], []

    left = property(lambda s: (s.x - s.w / 2, s.y))
    right = property(lambda s: (s.x + s.w / 2, s.y))
    top = property(lambda s: (s.x, s.y - s.h / 2))
    bottom = property(lambda s: (s.x, s.y + s.h / 2))


class Flow:
    def __init__(self, id, src, tgt, name="", cond=None, wp=None):
        self.id, self.src, self.tgt, self.name, self.cond, self.wp = id, src, tgt, name, cond, wp


def auto_waypoints(a: Node, b: Node):
    if abs(a.y - b.y) < 1 and b.x > a.x:
        return [a.right, b.left]
    if b.x > a.x:
        if a.type in GATEWAYS:  # leave a gateway from top/bottom
            p = a.top if b.y < a.y else a.bottom
            return [p, (a.x, b.y), b.left]
        if b.type in GATEWAYS:  # enter a gateway from top/bottom
            p = b.top if a.y < b.y else b.bottom
            return [a.right, (b.x, a.y), p]
        if abs(a.x - b.x) < 1:
            return [a.bottom if b.y > a.y else a.top, b.top if b.y > a.y else b.bottom]
        mx = (a.right[0] + b.left[0]) / 2
        return [a.right, (mx, a.y), (mx, b.y), b.left]
    if abs(a.x - b.x) < 1:
        return [a.bottom if b.y > a.y else a.top, b.top if b.y > a.y else b.bottom]
    if abs(a.y - b.y) < 1:  # backwards on the same row
        return [a.left, b.right]
    raise ValueError(f"give waypoints for {a.id} -> {b.id}")


class Process:
    def __init__(self, id, name, executable, pool, lanes=None, extra_attrs=""):
        self.id, self.name, self.executable = id, name, executable
        self.pool = pool  # (x, y, w, h)
        self.lanes = lanes or []  # [(id, name, y, h)]
        self.extra_attrs = extra_attrs
        self.nodes, self.flows = {}, []
        self.lane_of = {}

    def add(self, id, type, name, x, y, attrs="", body="", lane=None, label="below"):
        n = Node(id, type, name, x, y, attrs, body, label)
        self.nodes[id] = n
        if lane:
            self.lane_of[id] = lane
        return n

    def flow(self, src, tgt, name="", cond=None, wp=None, id=None):
        f = Flow(id or f"Flow_{src}_{tgt}", self.nodes[src], self.nodes[tgt], name, cond, wp)
        f.src.outgoing.append(f.id)
        f.tgt.incoming.append(f.id)
        self.flows.append(f)
        return f

    def xml(self):
        out = [f'  <bpmn:process id="{self.id}" name="{q(self.name)}" isExecutable="{str(self.executable).lower()}"{self.extra_attrs}>']
        if self.lanes:
            out.append(f'    <bpmn:laneSet id="LaneSet_{self.id}">')
            for lid, lname, _, _ in self.lanes:
                out.append(f'      <bpmn:lane id="{lid}" name="{q(lname)}">')
                for nid, l in self.lane_of.items():
                    if l == lid:
                        out.append(f"        <bpmn:flowNodeRef>{nid}</bpmn:flowNodeRef>")
                out.append("      </bpmn:lane>")
            out.append("    </bpmn:laneSet>")
        for n in self.nodes.values():
            name = f' name="{q(n.name)}"' if n.name else ""
            out.append(f"    <bpmn:{n.type} id=\"{n.id}\"{name}{n.attrs}>")
            for i in n.incoming:
                out.append(f"      <bpmn:incoming>{i}</bpmn:incoming>")
            for o in n.outgoing:
                out.append(f"      <bpmn:outgoing>{o}</bpmn:outgoing>")
            if n.body:
                out.append("      " + n.body)
            out.append(f"    </bpmn:{n.type}>")
        for f in self.flows:
            name = f' name="{q(f.name)}"' if f.name else ""
            if f.cond:
                out.append(f'    <bpmn:sequenceFlow id="{f.id}"{name} sourceRef="{f.src.id}" targetRef="{f.tgt.id}">')
                out.append(f'      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">{q(f.cond)}</bpmn:conditionExpression>')
                out.append("    </bpmn:sequenceFlow>")
            else:
                out.append(f'    <bpmn:sequenceFlow id="{f.id}"{name} sourceRef="{f.src.id}" targetRef="{f.tgt.id}" />')
        out.append("  </bpmn:process>")
        return "\n".join(out)

    def di(self):
        out = []
        for lid, _, ly, lh in self.lanes:
            x, _, w, _ = self.pool
            out.append(f'      <bpmndi:BPMNShape id="{lid}_di" bpmnElement="{lid}" isHorizontal="true">'
                       f'<dc:Bounds x="{x + 30}" y="{ly}" width="{w - 30}" height="{lh}" /></bpmndi:BPMNShape>')
        for n in self.nodes.values():
            extra = ' isMarkerVisible="true"' if n.type == "exclusiveGateway" else ""
            out.append(f'      <bpmndi:BPMNShape id="{n.id}_di" bpmnElement="{n.id}"{extra}>'
                       f'<dc:Bounds x="{n.x - n.w / 2:.0f}" y="{n.y - n.h / 2:.0f}" width="{n.w}" height="{n.h}" />')
            if n.type not in SIZE or n.w < 100:  # external label below events/gateways
                if n.label == "left":
                    lw = 88
                    lx, ly = n.x - n.w / 2 - 4 - lw, n.y - 20
                else:
                    lw = 110
                    lx, ly = n.x - lw / 2, n.y + n.h / 2 + 4
                out.append(f'        <bpmndi:BPMNLabel><dc:Bounds x="{lx:.0f}" y="{ly:.0f}" width="{lw}" height="40" /></bpmndi:BPMNLabel>')
            out.append("      </bpmndi:BPMNShape>")
        for f in self.flows:
            wp = f.wp or auto_waypoints(f.src, f.tgt)
            out.append(f'      <bpmndi:BPMNEdge id="{f.id}_di" bpmnElement="{f.id}">')
            for x, y in wp:
                out.append(f'        <di:waypoint x="{x:.0f}" y="{y:.0f}" />')
            if f.name:
                (x1, y1), (x2, y2) = wp[0], wp[1]
                if abs(y1 - y2) < 1:  # horizontal: just after the source
                    lx, ly = x1 + 2, y1 - 17
                else:
                    lx, ly = x1 + 4, (y1 + y2) / 2 - 8
                out.append(f'        <bpmndi:BPMNLabel><dc:Bounds x="{lx:.0f}" y="{ly:.0f}" width="26" height="14" /></bpmndi:BPMNLabel>')
            out.append("      </bpmndi:BPMNEdge>")
        return "\n".join(out)


def document(defs_id, processes, collapsed=(), message_flows=(), messages=(), doc="", annotations=()):
    """collapsed: [(participant id, name, (x,y,w,h))]
    message_flows: [(id, name, sourceRef, targetRef, waypoints)]"""
    h = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
         'xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" '
         'xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" '
         'xmlns:di="http://www.omg.org/spec/DD/20100524/DI" '
         'xmlns:camunda="http://camunda.org/schema/1.0/bpmn" '
         'xmlns:modeler="http://camunda.org/schema/modeler/1.0" '
         'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
         f'id="{defs_id}" targetNamespace="http://poeds-group-14.dtu.dk/rejsegaranti" '
         'exporter="tools/build_bpmn.py" exporterVersion="1.0" '
         'modeler:executionPlatform="Camunda Platform" modeler:executionPlatformVersion="7.24.0">']
    if doc:
        h.append(f"  <bpmn:documentation>{q(doc)}</bpmn:documentation>")
    h.append('  <bpmn:collaboration id="Collaboration_1">')
    for p in processes:
        h.append(f'    <bpmn:participant id="Participant_{p.id}" name="{q(p.name)}" processRef="{p.id}" />')
    for pid, name, _ in collapsed:
        h.append(f'    <bpmn:participant id="{pid}" name="{q(name)}" />')
    for mid, name, s, t, _ in message_flows:
        nm = f' name="{q(name)}"' if name else ""
        h.append(f'    <bpmn:messageFlow id="{mid}"{nm} sourceRef="{s}" targetRef="{t}" />')
    for aid, text, _ in annotations:
        h.append(f'    <bpmn:textAnnotation id="{aid}"><bpmn:text>{q(text)}</bpmn:text></bpmn:textAnnotation>')
    h.append("  </bpmn:collaboration>")
    for mid, mname in messages:
        h.append(f'  <bpmn:message id="{mid}" name="{q(mname)}" />')
    for p in processes:
        h.append(p.xml())
    h.append('  <bpmndi:BPMNDiagram id="BPMNDiagram_1">')
    h.append('    <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="Collaboration_1">')
    for p in processes:
        x, y, w, hh = p.pool
        h.append(f'      <bpmndi:BPMNShape id="Participant_{p.id}_di" bpmnElement="Participant_{p.id}" isHorizontal="true">'
                 f'<dc:Bounds x="{x}" y="{y}" width="{w}" height="{hh}" /></bpmndi:BPMNShape>')
        h.append(p.di())
    for pid, _, (x, y, w, hh) in collapsed:
        h.append(f'      <bpmndi:BPMNShape id="{pid}_di" bpmnElement="{pid}" isHorizontal="true">'
                 f'<dc:Bounds x="{x}" y="{y}" width="{w}" height="{hh}" /></bpmndi:BPMNShape>')
    for mid, name, _, _, wp in message_flows:
        h.append(f'      <bpmndi:BPMNEdge id="{mid}_di" bpmnElement="{mid}">')
        for x, y in wp:
            h.append(f'        <di:waypoint x="{x:.0f}" y="{y:.0f}" />')
        if name:
            segs = list(zip(wp, wp[1:]))
            (x1, y1), (x2, y2) = max(segs, key=lambda s: abs(s[0][0] - s[1][0]) + abs(s[0][1] - s[1][1]) * (0.5 if len(segs) > 1 else 1))
            if abs(y1 - y2) < 1:  # horizontal: above the middle
                lx, ly = (x1 + x2) / 2 - 60, y1 - 15
            else:
                lx, ly = x1 + 6, (y1 + y2) / 2 - 7
            h.append(f'        <bpmndi:BPMNLabel><dc:Bounds x="{lx:.0f}" y="{ly:.0f}" width="120" height="14" /></bpmndi:BPMNLabel>')
        h.append("      </bpmndi:BPMNEdge>")
    for aid, _, (x, y, w, hh) in annotations:
        h.append(f'      <bpmndi:BPMNShape id="{aid}_di" bpmnElement="{aid}"><dc:Bounds x="{x}" y="{y}" width="{w}" height="{hh}" /></bpmndi:BPMNShape>')
    h.append("    </bpmndi:BPMNPlane>")
    h.append("  </bpmndi:BPMNDiagram>")
    h.append("</bpmn:definitions>")
    return "\n".join(h) + "\n"
