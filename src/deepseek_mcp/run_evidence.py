"""Bounded execution receipts; model prose is never used as execution evidence."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field

MAX_EVIDENCE_BYTES = 64 * 1024
MAX_OBSERVATIONS = 128


@dataclass
class EvidenceAccumulator:
    observations: list[dict] = field(default_factory=list)
    observed: int = 0
    errors: int = 0
    nonzero_exits: int = 0
    timeouts: int = 0
    output_truncated: bool = False
    search_incomplete: bool = False
    omitted: int = 0
    _bytes: int = 0

    def add(self, observation: dict) -> None:
        """Accept only validated tool-runtime metadata, never worker summaries."""
        self.observed += 1
        self.errors += observation.get("status") == "error"
        self.output_truncated |= observation.get("truncated", False)
        self.search_incomplete |= observation.get("incomplete", False)
        command = observation.get("bash", {})
        exit_code = command.get("exit_code")
        self.nonzero_exits += exit_code is not None and exit_code != 0
        self.timeouts += command.get("timed_out", False)
        size = len(json.dumps(observation, ensure_ascii=True).encode("utf-8"))
        if (len(self.observations) >= MAX_OBSERVATIONS
                or self._bytes + size > MAX_EVIDENCE_BYTES):
            self.omitted += 1
            return
        self._bytes += size
        self.observations.append(copy.deepcopy(observation))

    def payload(self, tool_calls: int) -> dict:
        return {
            "schema_version": 1,
            "provenance": "tool_runtime",
            "observations": copy.deepcopy(self.observations),
            "summary": {
                "observed_tool_calls": self.observed,
                "unobserved_tool_calls": max(0, tool_calls - self.observed),
                "tool_errors": self.errors,
                "nonzero_command_exits": self.nonzero_exits,
                "command_timeouts": self.timeouts,
                "tool_output_truncated": self.output_truncated,
                "search_incomplete": self.search_incomplete,
                "observations_omitted": self.omitted,
                "receipt_truncated": self.omitted > 0,
            },
            "limitations": [
                "Execution observations do not establish task correctness.",
                "Command exit codes do not establish that the right tests ran.",
                "Source hashes identify observed bytes, not a workspace snapshot.",
                "Bash changes are not transaction-journaled; inspect the workspace.",
                "No full transcript, diff snapshot, or exhaustive data-egress audit.",
            ],
        }


def build_run_result(state, content: str | None, turn: int, *,
                     status: str = "completed", finish_reason: str = "final_response") -> dict:
    notices = [notice for notice in (
        state.mutations.recovery_notice(), state.mutations.warning_notice()
    ) if notice]
    return {
        "final_message": content or "",
        "notices": notices,
        "execution_status": status,
        "finish_reason": finish_reason,
        "acceptance_status": "unverified",
        "turns_used": turn + 1,
        "tokens": {
            "prompt": state.prompt_tokens,
            "completion": state.completion_tokens,
            "total": state.prompt_tokens + state.completion_tokens,
        },
        "tool_calls": state.tool_calls,
        "duration_seconds": round(max(0.0, time.time() - state.started), 2),
        "mutations": state.mutations.payload(),
        "evidence": state.evidence.payload(state.tool_calls),
    }


def failure_result(state, error: BaseException, turn: int) -> dict:
    from .provider_retry import AgentLoopCancelled

    cancelled = isinstance(error, AgentLoopCancelled)
    reason = "cancelled" if cancelled else getattr(error, "finish_reason", "internal_error")
    return build_run_result(state, None, turn, status=("cancelled" if cancelled else "failed"),
                            finish_reason=reason)


def cancelled_receipt(result: dict | None) -> dict | None:
    if result is None:
        return None
    receipt = copy.deepcopy(result)
    receipt.update(final_message="", execution_status="cancelled",
                   finish_reason="cancelled", acceptance_status="unverified")
    return receipt


def mutation_cancellation(result: dict, message: str, records):
    from .provider_retry import MutationOutcomeCancelled

    error = MutationOutcomeCancelled(message, tuple(records))
    error.finish_reason = "cancelled"
    error.delegation_result = cancelled_receipt(result)
    return error
