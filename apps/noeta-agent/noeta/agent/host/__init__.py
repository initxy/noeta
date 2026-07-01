"""``noeta.agent.host`` — the noeta-agent product's host layer.

T8 deleted the legacy HTTP/SSE
transport + the standalone ``noeta.server`` assembly (A) and the
``noeta.agent.host.session`` runner (``AgentSessionRunner`` / ``CodeSessionConfig``)
+ its ``noeta.agent.api`` facade (B); the backend now assembles engines through
``noeta.sdk`` (Client / SdkHost). What remains here is shared host **material**
reused by ``noeta.agent.backend`` — ``preview_gateway`` (the ``open_app`` HTML
preview), ``mcp_registry`` (the remote/stdio MCP connector store), and
``storage`` (the durable sqlite triple).
"""
