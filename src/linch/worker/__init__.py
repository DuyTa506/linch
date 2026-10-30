"""Optional PgQueuer runtime. Install ``linch[worker]`` and import explicitly."""

from importlib.util import find_spec as _find_spec

if _find_spec("pgqueuer") is None:
    raise ModuleNotFoundError("Worker runtime requires pip install 'linch[worker]'")

from .dispatch import RunRequest, enqueue_run, enqueue_runs, install
from .runtime import AgentFactory, Worker, WorkerJob
from .schedules import ScheduleDefinition, cancel_schedule, create_schedule, list_schedules
from .tools import register_schedule_tools

__all__ = [
    "ScheduleDefinition",
    "cancel_schedule",
    "create_schedule",
    "list_schedules",
    "register_schedule_tools",
    "AgentFactory",
    "RunRequest",
    "Worker",
    "WorkerJob",
    "enqueue_run",
    "enqueue_runs",
    "install",
]
