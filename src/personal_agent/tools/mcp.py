"""Real MCP transports, exact JSON schemas and explicit host tool grants."""

import hashlib
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator
from referencing import Registry

from personal_agent.tools.registry import Tool, ToolExecutionError

logger = logging.getLogger(__name__)


def reject_remote_refs(value) -> None:
    if isinstance(value, dict):
        if "$ref" in value and not str(value["$ref"]).startswith("#"):
            raise ValueError("Remote JSON schema references are forbidden")
        for item in value.values():
            reject_remote_refs(item)
    elif isinstance(value, list):
        for item in value:
            reject_remote_refs(item)


class SchemaArguments:
    def __init__(self, schema: dict) -> None:
        if len(json.dumps(schema).encode()) > 32768:
            raise ValueError("MCP schema exceeds budget")
        reject_remote_refs(schema)
        Draft202012Validator.check_schema(schema)
        self.schema = schema
        self.validator = Draft202012Validator(schema, registry=Registry())

    def model_json_schema(self):
        return self.schema

    def model_validate_json(self, raw: str):
        value = json.loads(raw)
        self.validator.validate(value)
        return value


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    allowed_tools: frozenset[str] = frozenset()
    env: dict[str, str] = field(default_factory=dict, repr=False)


class MCPManager:
    def __init__(self, configs: list[MCPServerConfig]) -> None:
        if len(configs) > 16 or len({config.name for config in configs}) != len(configs):
            raise ValueError("Duplicate or excessive MCP servers")
        self.configs = configs
        self.stack = AsyncExitStack()
        self.status: dict[str, str] = {}

    async def start(self, registry) -> None:
        import asyncio

        from mcp import Client, StdioServerParameters

        for config in self.configs:
            local_stack = AsyncExitStack()
            try:
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", config.name):
                    raise ValueError("Invalid server name")
                if bool(config.command) == bool(config.url):
                    raise ValueError("Exactly one MCP transport is required")
                if config.url:
                    from urllib.parse import urlsplit

                    parsed = urlsplit(config.url)
                    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
                        raise ValueError("Invalid MCP URL")
                    if parsed.scheme == "http" and parsed.hostname not in {
                        "localhost",
                        "127.0.0.1",
                        "::1",
                    }:
                        raise ValueError("Remote MCP requires HTTPS")
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client

                    http_client = await local_stack.enter_async_context(
                        httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=10)
                    )
                    server = streamable_http_client(config.url, http_client=http_client)
                else:
                    server = StdioServerParameters(
                        command=config.command, args=list(config.args), env=config.env
                    )
                async with asyncio.timeout(10):
                    client = await local_stack.enter_async_context(Client(server))
                    discovered, cursor = [], None
                    for _ in range(10):
                        page = await client.list_tools(cursor=cursor)
                        discovered.extend(page.tools)
                        if len(discovered) > 100:
                            raise ValueError("MCP discovery limit")
                        cursor = page.next_cursor
                        if cursor is None:
                            break
                    else:
                        raise ValueError("MCP pagination limit")
                tools = []
                for definition in discovered:
                    if definition.name not in config.allowed_tools:
                        continue
                    remote_name = definition.name
                    digest = hashlib.sha256(f"{config.name}:{remote_name}".encode()).hexdigest()[
                        :10
                    ]
                    name = (
                        re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{config.name}_{remote_name}")[:52]
                        + "_"
                        + digest
                    )
                    schema = getattr(definition, "input_schema", None)
                    if schema is None:
                        schema = definition.inputSchema
                    arguments = SchemaArguments(schema)

                    async def invoke(args, client=client, remote_name=remote_name):
                        result = await client.call_tool(remote_name, args)
                        content = []
                        for block in result.content:
                            if getattr(block, "type", None) == "text":
                                content.append(block.text)
                        data = dict(
                            content=content,
                            structured_content=getattr(result, "structured_content", None),
                        )
                        if getattr(result, "is_error", False):
                            raise ToolExecutionError(
                                "mcp_tool_error", "MCP tool returned a failure.", data=data
                            )
                        return data

                    tools.append(
                        Tool(
                            name, (definition.description or remote_name)[:2000], arguments, invoke
                        )
                    )
                if len({tool.name for tool in tools}) != len(tools):
                    raise ValueError("Duplicate MCP tool name")
                for tool in tools:
                    if tool.name in registry._tools:
                        raise ValueError("MCP tool name collision")
                for tool in tools:
                    registry.register(tool)
                self.stack.push_async_callback(local_stack.aclose)
                self.status[config.name] = "ready"
            except Exception as exc:
                await local_stack.aclose()
                self.status[config.name] = "unavailable"
                logger.warning("mcp_connect_failed type=%s", type(exc).__name__)

    async def close(self) -> None:
        await self.stack.aclose()
