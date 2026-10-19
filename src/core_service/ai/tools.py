"""Tools the AI may call (function calling), always read-only.

The model can *look at* fleet data but has no tool that changes anything. Every argument is
validated against the tool's schema before the handler runs, every result is checked by the
:class:`PrivacyGuard`, and any failure is returned to the model as an error result instead of
crashing the analysis.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core_service.ai.privacy_guard import PrivacyGuard

log = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Any]
MAX_RESULT_ITEMS = 50


class ToolArgumentError(ValueError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    """Minimal JSON-schema check: object, required, type, enum, length and range limits."""
    if not isinstance(arguments, dict):
        raise ToolArgumentError("arguments must be an object")
    properties: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            raise ToolArgumentError(f"missing argument '{key}'")
    if schema.get("additionalProperties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ToolArgumentError(f"unknown arguments {sorted(unknown)}")
    for key, value in arguments.items():
        rule = properties.get(key)
        if rule is None:
            continue
        expected = _TYPES.get(rule.get("type", ""), ())
        if expected and (not isinstance(value, expected) or (isinstance(value, bool) and rule["type"] != "boolean")):
            raise ToolArgumentError(f"argument '{key}' must be of type {rule['type']}")
        if "enum" in rule and value not in rule["enum"]:
            raise ToolArgumentError(f"argument '{key}' must be one of {rule['enum']}")
        if isinstance(value, str) and len(value) > rule.get("maxLength", 200):
            raise ToolArgumentError(f"argument '{key}' is too long")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                raise ToolArgumentError(f"argument '{key}' must be >= {rule['minimum']}")
            if "maximum" in rule and value > rule["maximum"]:
                raise ToolArgumentError(f"argument '{key}' must be <= {rule['maximum']}")
    return arguments


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: Any


class ToolRegistry:
    def __init__(self, guard: PrivacyGuard | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._guard = guard or PrivacyGuard()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [self._tools[name].spec() for name in self.names()]

    def execute(self, name: str, arguments: Any) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, {"error": f"unknown tool '{name}'"})
        try:
            checked = validate_arguments(tool.input_schema, arguments)
            result = tool.handler(checked)
            if isinstance(result, list):
                result = result[:MAX_RESULT_ITEMS]
            self._guard.assert_clean(result)
            return ToolResult(True, result)
        except ToolArgumentError as error:
            return ToolResult(False, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - a failing tool must not abort the analysis
            log.warning("Tool %s failed: %s", name, type(error).__name__)
            return ToolResult(False, {"error": "the tool could not produce a result"})
