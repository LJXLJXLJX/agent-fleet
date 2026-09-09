import json
import os
import sys
import urllib.request

from blocking import GUIDANCE, LeakPolicy
from config import ANONYMOUS_KEY, API_KEY_HEADER, LEAK_PROFILE, MCP_URL

LOCAL_TO_REMOTE = {
    "web_search": "web_search_exa",
    "web_fetch": "web_fetch_exa",
}
REMOTE_TO_LOCAL = {value: key for key, value in LOCAL_TO_REMOTE.items()}
SESSION_ID = ""


def _decode(body: str, request_id) -> dict:
    if not body.startswith("event:"):
        return json.loads(body)
    for event in body.split("\n\n"):
        data = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
        if data and data != ["[DONE]"]:
            message = json.loads("\n".join(data))
            if message.get("id") == request_id:
                return message
    raise ValueError("hosted MCP returned no JSON-RPC message")


def _request(message: dict) -> dict:
    global SESSION_ID
    key = os.environ.get("EXA_API_KEY", ANONYMOUS_KEY).strip()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
        "User-Agent": "agent-fleet-exa-mcp/1",
    }
    headers.update({"Mcp-Session-Id": SESSION_ID} if SESSION_ID else {})
    if key and key != ANONYMOUS_KEY:
        headers[API_KEY_HEADER] = key
    request = urllib.request.Request(
        MCP_URL,
        data=json.dumps(message).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        SESSION_ID = response.headers.get("Mcp-Session-Id", SESSION_ID)
        return _decode(response.read().decode(), message.get("id")) if "id" in message else {}


def _forward(message: dict, policy: LeakPolicy) -> dict:
    if message.get("method") not in {
        "initialize", "ping", "notifications/initialized", "notifications/cancelled",
        "tools/list", "tools/call",
    }:
        return {"jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32601, "message": "Method unavailable under the research policy"}}
    outgoing = dict(message)
    if message.get("method") == "tools/call":
        outgoing["params"] = dict(message.get("params") or {})
        name = outgoing["params"].get("name")
        outgoing["params"]["name"] = LOCAL_TO_REMOTE.get(name, name)
        if (
            outgoing["params"]["name"] not in REMOTE_TO_LOCAL
            or policy.blocked_request(outgoing["params"].get("arguments", {}))
        ):
            return policy.response(message)
    result = _request(outgoing)
    if message.get("method") == "tools/call":
        arguments = outgoing["params"].get("arguments", {})
        query = arguments.get("query", "") if outgoing["params"]["name"] == "web_search_exa" else ""
        if policy.blocked_response(result, query):
            return policy.response(message)
    if message.get("method") == "tools/list":
        result["result"]["tools"] = [
            tool for tool in result["result"]["tools"] if tool.get("name") in REMOTE_TO_LOCAL
        ]
        for tool in result.get("result", {}).get("tools", []):
            tool["name"] = REMOTE_TO_LOCAL.get(tool.get("name"), tool.get("name"))
            tool["description"] = tool.get("description", "").replace("_exa", "")
            tool["description"] += GUIDANCE
    return result


def main() -> None:
    policy = LeakPolicy(LEAK_PROFILE, os.environ.get("WEB_MCP_INSTRUCTION", ""))
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            response = _forward(message, policy)
        except Exception as exc:  # noqa: BLE001 - keep serving after a bad request.
            if "id" not in message:
                continue
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32000, "message": type(exc).__name__},
            }
        if "id" not in message:
            continue
        print(json.dumps(response, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
