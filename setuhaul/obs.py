"""Observability: one trace per inbound message, spans per step, metrics in
CloudWatch Embedded Metric Format (EMF).

Locally the records go to an in-memory sink (and optionally stdout). In AWS
Lambda, printing these JSON lines to stdout is enough: CloudWatch Logs stores
the spans, and EMF lines become CloudWatch metrics with no extra API call.
X-Ray / OpenTelemetry (ADOT) can be layered on by wrapping `span()`.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager

NAMESPACE = "SetuHaul/DriverAgent"


class Telemetry:
    def __init__(self, service: str = "driver-agent", echo: bool | None = None):
        self.service = service
        self.echo = (os.environ.get("SETUHAUL_LOG_STDOUT") == "1") if echo is None else echo
        self.records: list[dict] = []
        self.trace_id: str | None = None
        self._attrs: dict = {}

    # -- tracing --------------------------------------------------------------
    def new_trace(self, **attrs) -> str:
        self.trace_id = uuid.uuid4().hex[:16]
        self._attrs = attrs
        return self.trace_id

    @contextmanager
    def span(self, name: str, **attrs):
        t0 = time.perf_counter()
        rec = {"type": "span", "service": self.service, "trace_id": self.trace_id, "span": name,
               **self._attrs, **attrs}
        try:
            yield rec
            rec["status"] = "OK"
        except Exception as e:  # noqa: BLE001 - recorded then re-raised
            rec["status"] = "ERROR"
            rec["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            self._emit(rec)

    def event(self, name: str, **attrs):
        self._emit({"type": "event", "service": self.service, "trace_id": self.trace_id, "event": name,
                    **self._attrs, **attrs})

    # -- metrics (EMF) --------------------------------------------------------
    def metric(self, name: str, value: float = 1, unit: str = "Count", **dims):
        dims = {"Service": self.service, **{k: str(v) for k, v in dims.items()}}
        rec = {
            "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
                "Namespace": NAMESPACE, "Dimensions": [list(dims.keys())],
                "Metrics": [{"Name": name, "Unit": unit}]}]},
            name: value, **dims, "trace_id": self.trace_id, "type": "metric",
        }
        self._emit(rec)

    def _emit(self, rec: dict):
        self.records.append(rec)
        if self.echo:
            print(json.dumps(rec, default=str, ensure_ascii=False))

    # -- helpers for evals / dashboards --------------------------------------
    def counters(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.records:
            if r.get("type") == "metric":
                for m in r["_aws"]["CloudWatchMetrics"][0]["Metrics"]:
                    key = m["Name"]
                    extra = [f"{k}={r[k]}" for k in r["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0] if k != "Service"]
                    if extra:
                        key += "{" + ",".join(extra) + "}"
                    out[key] = out.get(key, 0) + r[m["Name"]]
        return out


NULL = Telemetry(echo=False)
