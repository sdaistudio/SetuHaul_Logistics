"""Optional LLM fallback for message understanding ONLY.

Used when the deterministic parser returns UNKNOWN (e.g. a language outside
EN/HI/Hinglish, heavy slang, or a long rambling voice-note transcript).

Contract:
* input: PII-redacted text + the list of allowed fields
* output: JSON matching FALLBACK_SCHEMA, temperature 0, forced tool-call
* the output is re-validated and then goes through the SAME workflow, guards
  and capacity checks as a parsed message. The model never sees capacity,
  never ranks, never books, never writes driver-facing text.

On AWS: Amazon Bedrock Converse API (a small Claude model), with a Bedrock
Guardrail attached (prompt-attack filter + PII). Optionally Amazon Translate
first for Marathi / Gujarati / Punjabi / Bengali, then the deterministic parser.
"""
from __future__ import annotations

import json
import os
import re

FALLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"enum": ["REPORT_DELAY", "ASK_OPTIONS", "SELECT_OPTION", "CONFIRM", "DECLINE",
                            "CHECK_STATUS", "EARLY_ARRIVAL", "HUMAN", "SAFETY", "UNKNOWN"]},
        "eta_hhmm": {"type": ["string", "null"], "pattern": r"^\d{2}:\d{2}$"},
        "delay_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 1440},
        "earliest_start_hhmm": {"type": ["string", "null"]},
        "latest_gate_out_hhmm": {"type": ["string", "null"]},
        "option_number": {"type": ["integer", "null"], "minimum": 1, "maximum": 3},
        "language": {"enum": ["en", "hi", "hinglish", "other"]},
    },
    "required": ["intent", "language"],
}


class NullFallback:
    name = "none"

    def parse(self, redacted_text: str) -> dict | None:
        return None


def validate(out: dict) -> dict | None:
    if not isinstance(out, dict) or out.get("intent") not in FALLBACK_SCHEMA["properties"]["intent"]["enum"]:
        return None
    for k in ("eta_hhmm", "earliest_start_hhmm", "latest_gate_out_hhmm"):
        v = out.get(k)
        if v is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(v)):
            out[k] = None
    on = out.get("option_number")
    if on is not None and on not in (1, 2, 3):
        out["option_number"] = None
    return out


class BedrockFallback:  # pragma: no cover - needs AWS credentials
    name = "bedrock"

    def __init__(self, model_id: str | None = None, guardrail_id: str | None = None, guardrail_version: str = "DRAFT"):
        import boto3  # only imported on AWS
        self.client = boto3.client("bedrock-runtime")
        self.model_id = model_id or os.environ.get("FALLBACK_MODEL_ID", "anthropic.claude-haiku")
        self.guardrail_id = guardrail_id or os.environ.get("BEDROCK_GUARDRAIL_ID")
        self.guardrail_version = guardrail_version

    def parse(self, redacted_text: str) -> dict | None:
        req = {
            "modelId": self.model_id,
            "system": [{"text": "Extract fields from a truck driver's message to a dock-scheduling desk. "
                                "Only fill fields the message states. Never invent times. Output via the tool."}],
            "messages": [{"role": "user", "content": [{"text": redacted_text}]}],
            "inferenceConfig": {"temperature": 0, "maxTokens": 300},
            "toolConfig": {"tools": [{"toolSpec": {"name": "extract", "inputSchema": {"json": FALLBACK_SCHEMA}}}],
                           "toolChoice": {"tool": {"name": "extract"}}},
        }
        if self.guardrail_id:
            req["guardrailConfig"] = {"guardrailIdentifier": self.guardrail_id,
                                      "guardrailVersion": self.guardrail_version}
        resp = self.client.converse(**req)
        for block in resp["output"]["message"]["content"]:
            if "toolUse" in block:
                return validate(block["toolUse"]["input"])
        return None


def get_fallback():
    return BedrockFallback() if os.environ.get("ENABLE_LLM_FALLBACK") == "1" else NullFallback()


def dumps(o):
    return json.dumps(o, ensure_ascii=False)
