"""Minimal Camunda 7 REST client (standard library only)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional


class CamundaError(RuntimeError):
    pass


def to_variables(values: Dict[str, Any]) -> Dict[str, dict]:
    """Python values -> Camunda typed variables. Lists/dicts become JSON strings."""
    out = {}
    for k, v in values.items():
        if v is None:
            out[k] = {"value": None, "type": "Null"}
        elif isinstance(v, bool):
            out[k] = {"value": v, "type": "Boolean"}
        elif isinstance(v, int):
            out[k] = {"value": v, "type": "Long"}
        elif isinstance(v, float):
            out[k] = {"value": v, "type": "Double"}
        elif isinstance(v, (list, dict)):
            out[k] = {"value": json.dumps(v, ensure_ascii=False), "type": "String"}
        else:
            out[k] = {"value": str(v), "type": "String"}
    return out


def from_variables(variables: Dict[str, dict]) -> Dict[str, Any]:
    return {k: v.get("value") for k, v in (variables or {}).items()}


class Camunda:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        default = f"http://localhost:{os.environ.get('CAMUNDA_PORT', '8085')}/engine-rest"
        self.base = (base_url or os.environ.get("CAMUNDA_URL", default)).rstrip("/")
        self.timeout = timeout

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None, raw: Optional[bytes] = None,
                 headers: Optional[dict] = None, timeout: Optional[float] = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            raise CamundaError(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')}") from None

    def get(self, path: str, **params):
        from urllib.parse import urlencode

        q = ("?" + urlencode({k: v for k, v in params.items() if v is not None})) if params else ""
        return self._request("GET", path + q)

    def post(self, path: str, body: Any = None, timeout: Optional[float] = None):
        return self._request("POST", path, body=body if body is not None else {}, timeout=timeout)

    # ---- deployment ------------------------------------------------------
    def deploy(self, name: str, files: List[str]) -> dict:
        boundary = uuid.uuid4().hex
        parts = []

        def field(n, v):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())

        field("deployment-name", name)
        field("enable-duplicate-filtering", "true")
        field("deployment-source", "rejsegaranti-deploy")
        for path in files:
            fn = os.path.basename(path)
            with open(path, "rb") as fh:
                content = fh.read()
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{fn}"; filename="{fn}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request(
            "POST", "/deployment/create", raw=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    # ---- messages --------------------------------------------------------
    def correlate(self, message: str, business_key: Optional[str] = None,
                  variables: Optional[dict] = None, correlation_keys: Optional[dict] = None) -> Any:
        body: Dict[str, Any] = {"messageName": message, "resultEnabled": True}
        if business_key:
            body["businessKey"] = business_key
        if variables:
            body["processVariables"] = to_variables(variables)
        if correlation_keys:
            body["correlationKeys"] = to_variables(correlation_keys)
        return self.post("/message", body)

    # ---- external tasks --------------------------------------------------
    def fetch_and_lock(self, worker_id: str, topics: List[str], max_tasks: int = 10,
                       lock_ms: int = 30_000, long_poll_ms: int = 10_000) -> List[dict]:
        body = {
            "workerId": worker_id,
            "maxTasks": max_tasks,
            "asyncResponseTimeout": long_poll_ms,
            "topics": [{"topicName": t, "lockDuration": lock_ms} for t in topics],
        }
        return self.post("/external-task/fetchAndLock", body, timeout=long_poll_ms / 1000 + 15) or []

    def complete(self, task_id: str, worker_id: str, variables: Optional[dict] = None) -> None:
        self.post(f"/external-task/{task_id}/complete",
                  {"workerId": worker_id, "variables": to_variables(variables or {})})

    def failure(self, task_id: str, worker_id: str, message: str, retries: int, retry_ms: int) -> None:
        self.post(f"/external-task/{task_id}/failure", {
            "workerId": worker_id, "errorMessage": message[:600],
            "retries": retries, "retryTimeout": retry_ms,
        })

    # ---- history (for the event log) ---------------------------------------
    # ---- decisions ---------------------------------------------------------
    def evaluate(self, key: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate the latest deployed DMN decision; its single result row as plain values ({} if none)."""
        rows = self.post(f"/decision-definition/key/{key}/evaluate", {"variables": to_variables(variables)})
        return {k: v.get("value") for k, v in rows[0].items()} if rows else {}

    def process_instances(self, key: str) -> List[dict]:
        return self.get("/history/process-instance", processDefinitionKey=key,
                        sortBy="startTime", sortOrder="asc", maxResults=100000) or []

    def activity_instances(self, pid: str) -> List[dict]:
        return self.get("/history/activity-instance", processInstanceId=pid,
                        sortBy="startTime", sortOrder="asc", maxResults=100000) or []

    def variables(self, pid: str) -> Dict[str, Any]:
        rows = self.get("/history/variable-instance", processInstanceId=pid, deserializeValues="false") or []
        return {r["name"]: r.get("value") for r in rows}
