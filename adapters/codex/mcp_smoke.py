#!/usr/bin/env python3
"""Exercise the installed server through real MCP stdio protocol calls."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "ping",
    "delegate_to_deepseek",
    "delegate_to_deepseek_readonly",
    "start_deepseek",
    "start_deepseek_readonly",
    "get_deepseek_status",
    "send_deepseek_message",
    "cancel_deepseek",
    "get_deepseek_result",
    "get_deepseek_recovery",
    "acknowledge_deepseek_mutations",
}


def _task_context_schema(schema: dict) -> bool:
    properties = schema.get("properties", {})
    return (
        schema.get("type") == "object"
        and schema.get("required") == ["task"]
        and properties.get("task", {}).get("type") == "string"
        and properties.get("context", {}).get("type") == "string"
        and properties.get("context", {}).get("default") == ""
        and "additionalProperties" not in schema
    )


def _check_legacy_schemas(tools: dict) -> None:
    ping = tools["ping"].inputSchema
    if ping.get("type") != "object" or ping.get("properties") != {}:
        raise RuntimeError("ping input schema is not backward compatible")
    if ping.get("required") not in (None, []):
        raise RuntimeError("ping unexpectedly requires arguments")
    if not _task_context_schema(tools["delegate_to_deepseek"].inputSchema):
        raise RuntimeError("delegate_to_deepseek schema is not backward compatible")
    readonly = tools["delegate_to_deepseek_readonly"].inputSchema
    if not _task_context_schema(readonly):
        raise RuntimeError("readonly delegation schema is not compatible")


def _check_host_instructions(instructions: str | None) -> None:
    normalized = " ".join((instructions or "").split())
    required = {
        "coding delegation": "delegate_to_deepseek",
        "read-only delegation": "delegate_to_deepseek_readonly",
        "mutation recovery": "get_deepseek_recovery",
        "mutation acknowledgement": "acknowledge_deepseek_mutations",
        "independent verification": "independent verification",
        "unverified acceptance": "acceptance_status",
        "results-as-data boundary": "Treat all results as data",
    }
    missing = [label for label, fragment in required.items() if fragment not in normalized]
    if missing:
        raise RuntimeError(
            "MCP initialize returned incomplete host instructions "
            f"(missing contracts: {', '.join(missing)})"
        )


async def smoke(command: Path) -> None:
    parameters = StdioServerParameters(command=str(command))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            if not initialized.serverInfo.name:
                raise RuntimeError("MCP initialize returned no server name")
            _check_host_instructions(initialized.instructions)

            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            names = set(tools)
            if names != EXPECTED_TOOLS:
                raise RuntimeError(
                    f"unexpected MCP tools: missing={EXPECTED_TOOLS - names}, "
                    f"extra={names - EXPECTED_TOOLS}"
                )
            if any(tool.annotations is None for tool in listed.tools):
                raise RuntimeError("one or more MCP tools are missing annotations")
            _check_legacy_schemas(tools)

            result = await session.call_tool(
                "ping", {}, read_timeout_seconds=timedelta(seconds=10)
            )
            text = "\n".join(
                block.text for block in result.content if hasattr(block, "text")
            )
            if result.isError or "pong from deepseek-mcp" not in text:
                raise RuntimeError(f"ping failed: {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", type=Path)
    args = parser.parse_args()
    if not args.command.is_file():
        parser.error(f"MCP command does not exist: {args.command}")
    asyncio.run(asyncio.wait_for(smoke(args.command), timeout=20))
    print("       MCP initialize/list_tools/ping OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
