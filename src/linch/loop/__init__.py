"""The agent loop package.

Split by responsibility:

- ``runner``     — ``run_loop`` / ``resume_loop`` and the turn loop itself
- ``streaming``  — provider streaming + ContextLengthError recovery
- ``request``    — user message / context / ProviderRequest assembly
- ``terminals``  — terminal event tails and closed-loop gates
- ``checkpoint`` — event persistence and checkpoint (de)serialization

The public surface is re-exported here so ``from linch.loop import ...``
keeps working exactly as it did when this was a single module.
"""

from .request import apply_provider_capabilities as apply_provider_capabilities
from .request import build_user_message as build_user_message
from .request import final_text as final_text
from .runner import _run_loop_impl as _run_loop_impl
from .runner import resume_loop as resume_loop
from .runner import run_loop as run_loop
from .streaming import stream_turn as stream_turn

__all__ = [
    "apply_provider_capabilities",
    "build_user_message",
    "final_text",
    "resume_loop",
    "run_loop",
    "stream_turn",
]
