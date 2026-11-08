"""PocketAgent Evolve core services.

The legacy ``agent`` package remains the LangChain integration layer.  This
package owns durable state, policies, MCP transport, memories, and skills so
the CLI, browser, and FastAPI entry points can share the same behavior.
"""

from .bootstrap import RuntimeServices, get_runtime_services, reset_runtime_services

__all__ = ["RuntimeServices", "get_runtime_services", "reset_runtime_services"]
