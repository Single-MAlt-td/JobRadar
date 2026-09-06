from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from ..models import JobCandidate
from .models import AiModelConfig
from .tools import AiToolRegistry, AiToolResult


@dataclass(frozen=True)
class AgentRunConfig:
    """Configuration for one MVP 0.6.6.7 agent run."""

    planning_model: AiModelConfig
    screening_model: AiModelConfig
    detail_model: AiModelConfig | None
    query: str
    location: str
    radius_km: int = 40
    results_per_search: int = 25
    target_jobs: int = 5
    maximum_searches: int = 20
    maximum_search_attempts: int = 40
    perform_detail_analysis: bool = True
    calculate_routes: bool = False
    additional_instructions: str = ""


class AgentStopRequested(RuntimeError):
    pass


class AgentTaskQueue:
    def __init__(self) -> None:
        self._items: list[object] = []

    def add(self, task: object) -> None:
        self._items.append(task)

    def get_nowait(self) -> object:
        if not self._items:
            raise IndexError("Agent task queue is empty")
        return self._items.pop(0)

    def empty(self) -> bool:
        return not self._items


class AgentTools:
    """Facade used by the worker-thread agent."""

    def __init__(
        self,
        registry: AiToolRegistry,
        *,
        search_page: Callable[[str, str, int, int, int], list[JobCandidate]] | None = None,
        publish_candidates: Callable[[list[JobCandidate], int, bool], list[dict]] | None = None,
        screen_rows: Callable[[list[dict], AiModelConfig], dict] | None = None,
        apply_screening: Callable[[list[dict]], dict] | None = None,
        build_detail_payloads: Callable[[list[str]], list[dict]] | None = None,
        analyze_details: Callable[[list[dict], AiModelConfig], dict] | None = None,
        apply_details: Callable[[list[dict], AiModelConfig, str], dict] | None = None,
        plan_searches: Callable[[AiModelConfig, list[dict] | None, str], dict] | None = None,
        record_search_outcome: Callable[[dict], None] | None = None,
        ui_call: Callable[[Callable[[], object]], object] | None = None,
    ) -> None:
        self.registry = registry
        self._search_page = search_page
        self._publish_candidates = publish_candidates
        self._screen_rows = screen_rows
        self._apply_screening = apply_screening
        self._build_detail_payloads = build_detail_payloads
        self._analyze_details = analyze_details
        self._apply_details = apply_details
        self._plan_searches = plan_searches
        self._record_search_outcome = record_search_outcome
        self._ui_call = ui_call

    def list_tools(self) -> list[dict]:
        return self.registry.list_tools()

    def execute(self, name: str, arguments: dict | None = None) -> AiToolResult:
        return self.registry.execute(name, arguments or {})

    def record_search_outcome(self, outcome: dict) -> None:
        if self._record_search_outcome is None or self._ui_call is None:
            return
        self._ui_call(lambda: self._record_search_outcome(dict(outcome)))

    def plan_searches(
        self,
        model: AiModelConfig,
        feedback: list[dict] | None = None,
        additional_instructions: str = "",
    ) -> dict:
        if self._plan_searches is None:
            raise RuntimeError("The search-planning callback is not configured.")
        return self._plan_searches(model, feedback, additional_instructions)

    def search_ba_page(self, config: AgentRunConfig, query: str, page: int) -> list[JobCandidate]:
        if self._search_page is None:
            raise RuntimeError("The BA search callback is not configured.")
        return self._search_page(query, config.location, int(config.radius_km), int(page), int(config.results_per_search))

    def publish_search_results(self, candidates: list[JobCandidate], page: int, first_page: bool) -> list[dict]:
        if self._publish_candidates is None or self._ui_call is None:
            raise RuntimeError("The GUI publishing callbacks are not configured.")
        return list(self._ui_call(lambda: self._publish_candidates(candidates, page, first_page)) or [])

    def screen(self, rows: list[dict], model: AiModelConfig) -> dict:
        if self._screen_rows is None:
            raise RuntimeError("The AI screening callback is not configured.")
        return self._screen_rows(rows, model)

    def apply_screening_results(self, items: Iterable[dict]) -> dict:
        if self._apply_screening is None or self._ui_call is None:
            raise RuntimeError("The screening-result callback is not configured.")
        clean_items = [item for item in items if isinstance(item, dict)]
        return dict(self._ui_call(lambda: self._apply_screening(clean_items)) or {})

    def detail_payloads(self, iids: list[str]) -> list[dict]:
        if self._build_detail_payloads is None or self._ui_call is None:
            raise RuntimeError("The detail-payload callback is not configured.")
        return list(self._ui_call(lambda: self._build_detail_payloads(iids)) or [])

    def analyze_details(self, payloads: list[dict], model: AiModelConfig) -> dict:
        if self._analyze_details is None:
            raise RuntimeError("The detail-analysis callback is not configured.")
        return self._analyze_details(payloads, model)

    def apply_detail_results(self, items: list[dict], model: AiModelConfig, raw_response: str) -> dict:
        if self._apply_details is None or self._ui_call is None:
            raise RuntimeError("The detail-result callback is not configured.")
        return dict(self._ui_call(lambda: self._apply_details(items, model, raw_response)) or {})


class AgentController:
    """Plans queries, searches BA, screens results and abandons weak queries."""

    def __init__(self, tools: AgentTools) -> None:
        self.tools = tools
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self, config: AgentRunConfig, *, on_status, on_log, on_done, on_error) -> bool:
        with self._lock:
            if self.is_running:
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(config, on_status, on_log, on_done, on_error),
                daemon=True,
                name="JobRadarAgent",
            )
            self._thread.start()
            return True

    def request_stop(self) -> None:
        self._stop_event.set()

    def _check_stop(self) -> None:
        if self._stop_event.is_set():
            raise AgentStopRequested

    @staticmethod
    def _screen_score(item: dict) -> int:
        try:
            return max(0, min(25, int(round(float(item.get("score", 0))))))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _detail_selection(cls, items: list[dict]) -> list[dict]:
        selected: list[dict] = []
        red_candidates: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or not str(item.get("iid") or "").strip():
                continue
            score = cls._screen_score(item)
            hard = item.get("hard_exclusion") is True
            if hard:
                continue
            if score >= 9:
                selected.append(item)
            else:
                red_candidates.append(item)

        # Red jobs are only worth the extra API cost when the shallow result itself
        # contains a concrete reason why the title/company may be misleading. There is
        # deliberately no quota such as "always inspect two red jobs".
        def has_hidden_potential(item: dict) -> bool:
            signals = item.get("interesting_signals")
            if not isinstance(signals, list) or not any(str(v).strip() for v in signals):
                return False
            confidence = str(item.get("confidence") or "").strip().lower()
            reason = " ".join(
                str(item.get(key) or "")
                for key in ("reason", "exclusion_reason")
            ).lower()
            uncertainty_words = (
                "unclear", "uncertain", "ambiguous", "could hide", "might hide",
                "unklar", "unsicher", "mehr dahinter", "titel", "potential",
                "potenzial", "possibly", "möglicherweise",
            )
            return confidence in {"low", "uncertain", "niedrig"} or any(word in reason for word in uncertainty_words)

        selected.extend(item for item in red_candidates if has_hidden_potential(item))
        return selected

    @staticmethod
    def _clean_query_plan(data: dict, fallback: str) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        raw = data.get("queries", []) if isinstance(data, dict) else []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    query, reason = item.strip(), ""
                elif isinstance(item, dict):
                    query = str(item.get("query") or "").strip()
                    reason = str(item.get("reason") or "").strip()
                else:
                    continue
                key = " ".join(query.casefold().split())
                if not query or key in seen:
                    continue
                seen.add(key)
                result.append({"query": query, "reason": reason})
        if not result and fallback.strip():
            result.append({"query": fallback.strip(), "reason": "Fallback from the main search field"})
        return result

    @staticmethod
    def _is_poor_page(screen_counts: dict, detail_counts: dict | None, item_count: int) -> bool:
        counts = detail_counts or screen_counts
        green = int(counts.get("potential", 0) or 0)
        orange = int(counts.get("maybe", 0) or 0)
        red = int(counts.get("unlikely", 0) or 0)
        if item_count <= 0:
            return True
        return green == 0 and orange <= 1 and red >= max(3, item_count // 2)

    def _run(self, config: AgentRunConfig, on_status, on_log, on_done, on_error) -> None:
        search_requests = successful_searches = candidates_seen = rows_screened = 0
        quick_green = quick_orange = quick_red = 0
        detail_analyzed = detail_green = detail_orange = detail_red = 0
        processed_iids: set[str] = set()
        detailed_iids: set[str] = set()
        feedback: list[dict] = []
        recoverable_errors: list[str] = []
        stop_reason = "maximum number of successful searches reached"

        try:
            on_log("Agent run started.")
            on_log(f"Planning model: {config.planning_model.name}")
            on_log(f"Search area: {config.location}, radius {config.radius_km} km, {config.results_per_search} results per page.")
            if config.additional_instructions.strip():
                on_log("Additional user instructions were supplied for search planning.")
            on_status("Planning search strategy...")
            plan_data = self.tools.plan_searches(config.planning_model, feedback, config.additional_instructions)
            plan = self._clean_query_plan(plan_data, config.query)
            if not plan:
                raise RuntimeError("The planning model returned no usable BA search queries.")
            strategy = str(plan_data.get("strategy") or "").strip()
            if strategy:
                on_log(f"Planner strategy: {strategy}")
            on_log(f"Planner created {len(plan)} search ideas.")

            query_index = 0
            first_publish = True
            while successful_searches < config.maximum_searches and search_requests < config.maximum_search_attempts:
                self._check_stop()
                if query_index >= len(plan):
                    on_status("Replanning search strategy...")
                    on_log("Search plan exhausted; asking the planning model for new ideas based on outcomes.")
                    plan_data = self.tools.plan_searches(config.planning_model, feedback, config.additional_instructions)
                    new_plan = self._clean_query_plan(plan_data, "")
                    used = {" ".join(item["query"].casefold().split()) for item in plan}
                    new_plan = [item for item in new_plan if " ".join(item["query"].casefold().split()) not in used]
                    if not new_plan:
                        stop_reason = "planner produced no new distinct search ideas"
                        break
                    plan.extend(new_plan)

                idea = plan[query_index]
                query = idea["query"]
                reason = idea.get("reason", "")
                on_log(f"CURRENT SEARCH: {query}" + (f" — {reason}" if reason else ""))
                page = 1
                poor_pages = 0
                query_stats = {"query": query, "pages": 0, "green": 0, "orange": 0, "red": 0, "new_jobs": 0,
                               "screened_jobs": 0, "detail_analyzed": 0, "score_sum": 0, "score_count": 0,
                               "outcome": ""}

                while successful_searches < config.maximum_searches and search_requests < config.maximum_search_attempts:
                    self._check_stop()
                    search_requests += 1
                    query_stats["pages"] += 1
                    on_status(
                        f"Attempt {search_requests}/{config.maximum_search_attempts} · successful {successful_searches}/{config.maximum_searches}: {query} · page {page} · promising {detail_green or quick_green}/{config.target_jobs}"
                    )
                    on_log(f"Attempt {search_requests}/{config.maximum_search_attempts} · successful {successful_searches}/{config.maximum_searches}: {query!r}, page {page}...")
                    candidates = self.tools.search_ba_page(config, query, page)
                    candidates_seen += len(candidates)
                    if candidates:
                        successful_searches += 1
                    on_log(f"BA returned {len(candidates)} candidates. Successful searches: {successful_searches}/{config.maximum_searches}.")

                    rows = self.tools.publish_search_results(candidates, page, first_page=first_publish)
                    first_publish = False
                    fresh_rows: list[dict] = []
                    for row in rows:
                        iid = str(row.get("iid") or "").strip()
                        if iid and iid not in processed_iids:
                            processed_iids.add(iid)
                            fresh_rows.append(row)
                    query_stats["new_jobs"] += len(fresh_rows)

                    page_screen = {"marked": 0, "potential": 0, "maybe": 0, "unlikely": 0}
                    page_detail: dict | None = None
                    items: list[dict] = []
                    if fresh_rows:
                        on_status(f"Attempt {search_requests}/{config.maximum_search_attempts}: screening {len(fresh_rows)} jobs...")
                        on_log(f"Screening {len(fresh_rows)} new job(s) for query {query!r}, page {page}...")
                        try:
                            result = self.tools.screen(fresh_rows, config.screening_model)
                            raw_items = result.get("items", []) if isinstance(result, dict) else []
                            items = raw_items if isinstance(raw_items, list) else []
                            page_screen = self.tools.apply_screening_results(items)
                            marked_now = int(page_screen.get("marked", 0) or 0)
                            query_stats["screened_jobs"] += marked_now
                            for screened_item in items:
                                if isinstance(screened_item, dict):
                                    try:
                                        query_stats["score_sum"] += float(screened_item.get("score", 0) or 0)
                                        query_stats["score_count"] += 1
                                    except (TypeError, ValueError):
                                        pass
                            rows_screened += marked_now
                            quick_green += int(page_screen.get("potential", 0) or 0)
                            quick_orange += int(page_screen.get("maybe", 0) or 0)
                            quick_red += int(page_screen.get("unlikely", 0) or 0)
                            on_log(f"Quick screening (preliminary): green {page_screen.get('potential', 0)}, orange {page_screen.get('maybe', 0)}, red {page_screen.get('unlikely', 0)}.")
                        except Exception as exc:
                            error_text = f"Quick screening failed for query {query!r}, page {page}: {exc}"
                            recoverable_errors.append(error_text)
                            on_log(f"WARNING: {error_text}. The agent will continue with the next page/search.")
                            items = []

                        if items and config.perform_detail_analysis and config.detail_model:
                            chosen = [item for item in self._detail_selection(items) if str(item.get("iid") or "") not in detailed_iids]
                            chosen_iids = [str(item.get("iid") or "") for item in chosen]
                            if chosen_iids:
                                on_status(f"Attempt {search_requests}/{config.maximum_search_attempts}: detailed analysis of {len(chosen_iids)} jobs...")
                                on_log(f"Detailed analysis of {len(chosen_iids)} job(s) for query {query!r}, page {page}...")
                                payloads = self.tools.detail_payloads(chosen_iids)
                                if payloads:
                                    try:
                                        detail_result = self.tools.analyze_details(payloads, config.detail_model)
                                        detail_items = detail_result.get("items", []) if isinstance(detail_result, dict) else []
                                        if not isinstance(detail_items, list):
                                            detail_items = []
                                        page_detail = self.tools.apply_detail_results(detail_items, config.detail_model, str(detail_result.get("raw_response") or ""))
                                        detailed_iids.update(chosen_iids)
                                        detail_updated_now = int(page_detail.get("updated", 0) or 0)
                                        query_stats["detail_analyzed"] += detail_updated_now
                                        detail_analyzed += detail_updated_now
                                        detail_green += int(page_detail.get("potential", 0) or 0)
                                        detail_orange += int(page_detail.get("maybe", 0) or 0)
                                        detail_red += int(page_detail.get("unlikely", 0) or 0)
                                        on_log(f"Detail results: green {page_detail.get('potential', 0)}, orange {page_detail.get('maybe', 0)}, red {page_detail.get('unlikely', 0)}.")
                                    except Exception as exc:
                                        error_text = f"Detail analysis failed for {len(chosen_iids)} job(s) from query {query!r}, page {page}: {exc}"
                                        recoverable_errors.append(error_text)
                                        on_log(
                                            f"WARNING: {error_text}. The agent will continue; affected colors and 0..25 scores remain preliminary quick-screening results."
                                        )
                    else:
                        on_log("No new jobs on this page; known/duplicate jobs were skipped.")

                    effective = page_detail or page_screen
                    query_stats["green"] += int(effective.get("potential", 0) or 0)
                    query_stats["orange"] += int(effective.get("maybe", 0) or 0)
                    query_stats["red"] += int(effective.get("unlikely", 0) or 0)
                    poor = self._is_poor_page(page_screen, page_detail, max(len(fresh_rows), int(effective.get("updated", 0) or 0)))
                    poor_pages = poor_pages + 1 if poor else 0

                    promising = detail_green if config.perform_detail_analysis and config.detail_model else quick_green
                    if promising >= config.target_jobs:
                        stop_reason = f"target of {config.target_jobs} promising jobs reached"
                        query_stats["outcome"] = stop_reason
                        if query_stats.get("score_count"):
                            query_stats["average_screening_score"] = round(query_stats["score_sum"] / query_stats["score_count"], 2)
                        query_stats.pop("score_sum", None)
                        query_stats.pop("score_count", None)
                        self.tools.record_search_outcome(query_stats)
                        feedback.append(query_stats)
                        raise StopIteration
                    if len(candidates) < config.results_per_search:
                        query_stats["outcome"] = "partial/empty final page; switch query"
                        on_log("Current query reached its final BA page. Switching to another search idea.")
                        break
                    if poor_pages >= 2:
                        query_stats["outcome"] = "two consecutive low-quality pages; switch query"
                        on_log("Two consecutive pages contained almost only unsuitable jobs. Switching to another search idea.")
                        break
                    page += 1

                if query_stats.get("score_count"):
                    query_stats["average_screening_score"] = round(query_stats["score_sum"] / query_stats["score_count"], 2)
                query_stats.pop("score_sum", None)
                query_stats.pop("score_count", None)
                self.tools.record_search_outcome(query_stats)
                feedback.append(query_stats)
                query_index += 1

            if search_requests >= config.maximum_search_attempts and successful_searches < config.maximum_searches:
                stop_reason = f"maximum of {config.maximum_search_attempts} search attempts reached"
            elif successful_searches >= config.maximum_searches:
                stop_reason = f"maximum of {config.maximum_searches} successful searches reached"
            on_status("Finished")
        except StopIteration:
            on_status("Finished")
        except AgentStopRequested:
            on_status("Stopped")
            on_done(
                "Agent stopped after the current step.\n\n"
                f"Search attempts: {search_requests}\nSuccessful searches: {successful_searches}\nNew jobs screened: {rows_screened}\nDetailed analyses: {detail_analyzed}.\n\n"
                "All results remain temporary."
            )
            return
        except Exception as exc:
            on_status("Failed")
            on_error(exc)
            return
        finally:
            with self._lock:
                self._thread = None

        warning_text = ""
        if recoverable_errors:
            preview = "\n".join(f"- {text}" for text in recoverable_errors[:5])
            if len(recoverable_errors) > 5:
                preview += f"\n- ... and {len(recoverable_errors) - 5} more error(s) (see agent log)."
            warning_text = (
                f"\n\nWARNING: {len(recoverable_errors)} AI step(s) still failed after automatic retries. "
                "The agent continued instead of stopping. Jobs whose detail analysis failed retain only preliminary quick-screening colors/scores.\n"
                + preview
            )
        on_done(
            "Agent analysis finished.\n\n"
            f"Search attempts: {search_requests}\nSuccessful searches: {successful_searches}\nBA candidates: {candidates_seen}\nNew jobs screened: {rows_screened}\n"
            f"Preliminary quick screening (0..25) — green {quick_green}, orange {quick_orange}, red {quick_red}\n"
            f"Successful detailed analyses: {detail_analyzed}\nFinal detail result (0..100) — green {detail_green}, orange {detail_orange}, red {detail_red}\n"
            f"Search ideas tried: {len(feedback)}\n\n"
            f"Stopped because: {stop_reason}.\nAll AI scores, summaries and colors are temporary; no status was changed."
            + warning_text
        )
