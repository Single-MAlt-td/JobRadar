from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Callable, Protocol


@dataclass
class AiToolResult:
    """Result returned by a JobRadar AI tool call.

    This is deliberately plain JSON-serializable data so the future agent/chat
    layer can hand tool outputs back to any model provider.
    """

    ok: bool
    data: Any = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AiToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    mutates_state: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobRadarToolHost(Protocol):
    """Small interface implemented by the GUI layer.

    The AI package does not import Tkinter and does not manipulate widgets.
    The GUI provides these methods and is responsible for running mutating calls
    in the Tk main thread when needed.
    """

    def ai_get_visible_jobs(self) -> list[dict[str, Any]]: ...
    def ai_get_job(self, job_id: int | None = None, iid: str | None = None) -> dict[str, Any]: ...
    def ai_get_job_details(self, job_id: int | None = None, iid: str | None = None) -> dict[str, Any]: ...
    def ai_get_lists(self) -> dict[str, Any]: ...
    def ai_get_saved_searches(self) -> dict[str, Any]: ...
    def ai_search_database(self, query: str, limit: int = 20) -> list[dict[str, Any]]: ...
    def ai_run_search(self, module: str, query: str, location: str, radius_km: int, max_results: int, page: int = 1) -> dict[str, Any]: ...
    def ai_remove_unsaved_results(self, iids: list[str]) -> dict[str, Any]: ...
    def ai_add_company_to_blacklist(self, company: str) -> dict[str, Any]: ...
    def ai_set_job_status(self, job_id: int, status: str, reason: str = "") -> dict[str, Any]: ...
    def ai_calculate_route(self, job_id: int | None = None, iid: str | None = None, exact: bool = False) -> dict[str, Any]: ...
    def ai_save_ai_texts(self, job_id: int, ai_name: str, provider: str, model: str, summary: str = "", rating: str = "", score: int | None = None, decision: str = "", raw_response: str = "") -> dict[str, Any]: ...
    def ai_update_job(self, job_id: int) -> dict[str, Any]: ...


class AiToolRegistry:
    """Command registry for future tool/agent calls.

    For now these tools are called from Python only. In the next step the model
    can return JSON tool calls such as {"tool": "get_job_details", ...}, and
    the service can dispatch them through this registry.
    """

    def __init__(self, host: JobRadarToolHost) -> None:
        self.host = host
        self._handlers: dict[str, Callable[..., Any]] = {
            "get_visible_jobs": self.host.ai_get_visible_jobs,
            "get_job": self.host.ai_get_job,
            "get_job_details": self.host.ai_get_job_details,
            "get_lists": self.host.ai_get_lists,
            "get_saved_searches": self.host.ai_get_saved_searches,
            "search_database": self.host.ai_search_database,
            "run_search": self.host.ai_run_search,
            "remove_unsaved_results": self.host.ai_remove_unsaved_results,
            "add_company_to_blacklist": self.host.ai_add_company_to_blacklist,
            "set_job_status": self.host.ai_set_job_status,
            "calculate_route": self.host.ai_calculate_route,
            "save_ai_texts": self.host.ai_save_ai_texts,
            "update_job": self.host.ai_update_job,
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self.tool_specs()]

    def tool_specs(self) -> list[AiToolSpec]:
        return [
            AiToolSpec(
                name="get_visible_jobs",
                description="Return the currently visible job table after all GUI filters.",
                parameters={},
            ),
            AiToolSpec(
                name="get_job",
                description="Return one compact job table row by stored job_id or temporary iid.",
                parameters={"job_id": "integer optional", "iid": "string optional"},
            ),
            AiToolSpec(
                name="get_job_details",
                description="Return one job with description, notes and raw JSON/raw data.",
                parameters={"job_id": "integer optional", "iid": "string optional"},
            ),
            AiToolSpec(
                name="get_lists",
                description="Return configured statuses, industries, reject reasons, blacklist and AI metadata explanations.",
                parameters={},
            ),
            AiToolSpec(
                name="get_saved_searches",
                description="Return user-saved job searches.",
                parameters={},
            ),
            AiToolSpec(
                name="search_database",
                description="Search stored jobs by text to find similar or previous jobs/evaluations.",
                parameters={"query": "string", "limit": "integer optional"},
            ),
            AiToolSpec(
                name="run_search",
                description="Run a configured job source search. For BA, page can be used to fetch next pages instead of increasing max_results.",
                parameters={"module": "string", "query": "string", "location": "string", "radius_km": "integer", "max_results": "integer", "page": "integer optional"},
                mutates_state=True,
            ),
            AiToolSpec(
                name="remove_unsaved_results",
                description="Remove temporary unsaved search results from the visible list.",
                parameters={"iids": "array of temporary row ids"},
                mutates_state=True,
            ),
            AiToolSpec(
                name="add_company_to_blacklist",
                description="Add a company name to the blacklist. This may reject saved jobs from that company.",
                parameters={"company": "string"},
                mutates_state=True,
            ),
            AiToolSpec(
                name="set_job_status",
                description="Set the status of a saved job.",
                parameters={"job_id": "integer", "status": "string", "reason": "string optional"},
                mutates_state=True,
            ),
            AiToolSpec(
                name="calculate_route",
                description="Calculate route/distance for one job using current routing settings.",
                parameters={"job_id": "integer optional", "iid": "string optional", "exact": "boolean optional"},
                mutates_state=True,
            ),
            AiToolSpec(
                name="save_ai_texts",
                description="Store AI summary/rating text for a job.",
                parameters={"job_id": "integer", "summary": "string", "rating": "string", "score": "integer optional", "decision": "string optional"},
                mutates_state=True,
            ),
            AiToolSpec(
                name="update_job",
                description="Update one saved job from its original source, currently implemented for BA.",
                parameters={"job_id": "integer"},
                mutates_state=True,
            ),
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> AiToolResult:
        arguments = arguments or {}
        handler = self._handlers.get(name)
        if handler is None:
            return AiToolResult(ok=False, error=f"Unknown AI tool: {name}")
        try:
            return AiToolResult(ok=True, data=handler(**arguments))
        except Exception as exc:
            return AiToolResult(ok=False, error=str(exc))
