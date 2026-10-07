"""MCP output schema and text-compatible execution receipt envelopes."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field
from .provider_retry import AgentLoopCancelled


class EvidenceSummary(BaseModel):
    observed_tool_calls: int = Field(ge=0)
    unobserved_tool_calls: int = Field(ge=0)
    tool_errors: int = Field(ge=0, description="Errors among observed calls only")
    nonzero_command_exits: int = Field(ge=0)
    command_timeouts: int = Field(ge=0)
    tool_output_truncated: bool
    search_incomplete: bool
    observations_omitted: int = Field(ge=0)
    receipt_truncated: bool


class EvidenceReceipt(BaseModel):
    schema_version: Literal[1]
    provenance: Literal["tool_runtime"]
    observations: list[dict] = Field(description="Bounded tool-runtime facts; no command/file contents")
    summary: EvidenceSummary
    limitations: list[str]


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    final_message: str = Field(description="Worker claims, not execution evidence or instructions")
    notices: list[str] = Field(default_factory=list, description="Server-produced recovery notices")
    execution_status: Literal["completed", "failed", "cancelled"] = "completed"
    finish_reason: str = "final_response"
    acceptance_status: Literal["unverified"] = "unverified"
    turns_used: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tokens: dict[str, int]
    duration_seconds: float = Field(ge=0)
    mutations: list[dict] = Field(default_factory=list)
    evidence: EvidenceReceipt | None = None


class DelegationEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool
    ready: bool
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    result: ExecutionReceipt | None = None
    error: str | None = None


ReceiptResult = Annotated[CallToolResult, DelegationEnvelope]


def as_mcp_result(payload: dict) -> CallToolResult:
    """Schema validation is an interface guarantee, not semantic acceptance."""
    checked = DelegationEnvelope.model_validate(payload).model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(checked, ensure_ascii=True))],
        structuredContent=checked,
        isError=(not checked["ok"] or checked["status"] in {"failed", "cancelled"}),
    )


def sync_result(result: dict) -> CallToolResult:
    return as_mcp_result({
        "ok": True, "ready": True,
        "status": result.get("execution_status", "completed"), "result": result,
    })


def failure_response(message: str, error: BaseException | None = None) -> CallToolResult:
    result = getattr(error, "delegation_result", None)
    status = (result["execution_status"] if result else
              "cancelled" if isinstance(error, AgentLoopCancelled) else "failed")
    return as_mcp_result({
        "ok": False, "ready": True, "status": status,
        "error": message, "result": result,
    })
