"""Build MCP tool results: ``{"content": [text], "isError": bool}``."""

import json


def tool_result(payload, is_error=False, indent=2):
    """A text result: a string goes out as it is, anything else as JSON.

    *indent* is ``json.dumps``'s: None is compact, for payloads with long arrays.
    """
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=indent)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def ok_result(payload, indent=2):
    return tool_result(payload, False, indent)


def error_result(payload, indent=2):
    return tool_result(payload, True, indent)
