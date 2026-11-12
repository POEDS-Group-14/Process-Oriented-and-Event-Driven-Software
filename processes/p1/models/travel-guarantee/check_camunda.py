"""Deploy travel-guarantee.dmn to a running Camunda 7 and check every decision against the
expected results in camunda-test-cases.json (all 1,920 input combinations of 'Travel guarantee
applies', plus boundary cases of 'Claim valid').

  python3 check_camunda.py                       # Camunda at http://localhost:8085/engine-rest
  CAMUNDA_URL=http://localhost:8080/engine-rest python3 check_camunda.py

Start Camunda first, e.g. with the project's docker-compose.yml (docker compose up -d).
Standard library only.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("CAMUNDA_URL", f"http://localhost:{os.environ.get('CAMUNDA_PORT', '8085')}/engine-rest")
HERE = os.path.dirname(os.path.abspath(__file__))


def call(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


def deploy(path):
    b = uuid.uuid4().hex
    parts = []
    for name, value in (("deployment-name", "travel-guarantee-dmn"), ("enable-duplicate-filtering", "true")):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    with open(path, "rb") as fh:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="data"; filename="{os.path.basename(path)}"\r\n'
                     f"Content-Type: application/xml\r\n\r\n".encode() + fh.read() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return call("POST", "/deployment/create", b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"})


def typed(v):
    if isinstance(v, bool):
        return {"value": v, "type": "Boolean"}
    if isinstance(v, int):
        return {"value": v, "type": "Integer"}
    return {"value": v, "type": "String"}


def main():
    d = deploy(os.path.join(HERE, "travel-guarantee.dmn"))
    print("deployed:", d.get("id"), ", ".join(x["key"] for x in (d.get("deployedDecisionDefinitions") or {}).values()) or "(unchanged)")
    with open(os.path.join(HERE, "camunda-test-cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)
    failed = 0
    for i, c in enumerate(cases, 1):
        body = json.dumps({"variables": {k: typed(v) for k, v in c["inputs"].items()}}).encode()
        try:
            res = call("POST", f"/decision-definition/key/{c['decision']}/evaluate", body, {"Content-Type": "application/json"})
            got = {k: v["value"] for k, v in res[0].items()} if res else {}
        except urllib.error.HTTPError as e:
            got = {"error": e.read().decode()[:300]}
        if got != c["expected"]:
            failed += 1
            if failed <= 10:
                print(f"FAIL {c['decision']} {c['inputs']}\n  expected {c['expected']}\n  got      {got}")
        if i % 200 == 0:
            print(f"{i}/{len(cases)} checked, {failed} failed")
    print(f"done: {len(cases) - failed}/{len(cases)} cases match")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
