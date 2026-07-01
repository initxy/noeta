"""L0 protocol layer: typed dataclasses, Protocols, and error types.

This module defines the boundary contracts for the entire Noeta runtime.
It must not contain business logic. Import-only dependencies are stdlib.
"""

from __future__ import annotations

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher, Lease, LeaseRegistry
from noeta.protocols.event_log import (
    EventLog,
    EventLogReader,
    EventLogSubscriber,
    EventLogWriter,
    Subscriber,
    Unsubscribe,
)
from noeta.protocols.messages import (
    Block,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    MessageOrigin,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.step_context import StepContext

__all__: list[str] = [
    "Block",
    "ContentStore",
    "Dispatcher",
    "EventLog",
    "EventLogReader",
    "EventLogSubscriber",
    "EventLogWriter",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Lease",
    "LeaseRegistry",
    "Message",
    "MessageOrigin",
    "StepContext",
    "Subscriber",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "Unsubscribe",
]
