from __future__ import annotations

import json
import threading
import re
from typing import Callable, Iterable

from .clients import create_ai_client
from .memory import AiMemoryService, load_profile_text
from .models import AiEvaluationResult, AiModelConfig, EmbeddingConfig
from .tools import AiToolRegistry


class AiEvaluationService:
    """Coordinates asynchronous AI evaluation jobs.

    The service never touches Tkinter widgets. GUI code passes callbacks and is
    responsible for forwarding callback handling to the Tk main thread via
    widget.after(...).
    """

    def __init__(self, db, tools: AiToolRegistry | None = None) -> None:
        self.db = db
        self.tools = tools
        self._usage_local = threading.local()

    def _capture_usage(self, client) -> None:
        usage = getattr(client, "last_usage", None)
        self._usage_local.value = usage.to_dict() if usage is not None else {}

    def consume_last_usage(self) -> dict:
        value = getattr(self._usage_local, "value", {}) or {}
        self._usage_local.value = {}
        return dict(value)

    def set_tools(self, tools: AiToolRegistry) -> None:
        self.tools = tools

    @staticmethod
    def _output_language(user_config: dict) -> str:
        value = str(user_config.get("ai_output_language") or "German").strip().lower()
        return "English" if value.startswith("en") else "German"

    def available_tool_specs(self) -> list[dict]:
        return self.tools.list_tools() if self.tools is not None else []

    def execute_tool(self, name: str, arguments: dict | None = None) -> dict:
        if self.tools is None:
            return {"ok": False, "error": "AI tools are not configured."}
        return self.tools.execute(name, arguments).to_dict()

    def evaluate_jobs_async(
        self,
        job_ids: Iterable[int],
        model_config: AiModelConfig,
        on_result: Callable[[AiEvaluationResult], None],
        on_error: Callable[[int, Exception], None],
        on_done: Callable[[], None],
    ) -> None:
        thread = threading.Thread(
            target=self._worker,
            args=(list(job_ids), model_config, on_result, on_error, on_done),
            daemon=True,
        )
        thread.start()

    def _worker(
        self,
        job_ids: list[int],
        model_config: AiModelConfig,
        on_result: Callable[[AiEvaluationResult], None],
        on_error: Callable[[int, Exception], None],
        on_done: Callable[[], None],
    ) -> None:
        for job_id in job_ids:
            try:
                result = self.evaluate_job(job_id, model_config)
                on_result(result)
            except Exception as exc:
                on_error(job_id, exc)
        on_done()

    def evaluate_job(self, job_id: int, model_config: AiModelConfig) -> AiEvaluationResult:
        # Placeholder only. The provider interface is ready, but real prompt/API
        # logic will be implemented in the next AI step.
        create_ai_client(model_config)
        raise NotImplementedError("AI evaluation is not implemented yet.")

    def build_context_package(
        self,
        job_text: str,
        user_config: dict,
        embedding_config: EmbeddingConfig | None = None,
    ) -> dict:
        """Build the context that will later be sent to a chat model.

        This is intentionally provider-independent. It combines the stable
        profile.md with semantically retrieved memories.
        """
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        memory_limit = int(user_config.get("ai_memory_retrieval_limit", 10) or 10)
        memories: list[dict] = []
        if bool(user_config.get("ai_memory_enabled", True)):
            config = embedding_config or EmbeddingConfig.from_dict(user_config.get("ai_embedding"))
            memory_service = AiMemoryService(self.db, config)
            for memory, score in memory_service.retrieve_relevant_memories(job_text, limit=memory_limit):
                memories.append(
                    {
                        "id": memory.id,
                        "text": memory.text,
                        "category": memory.category,
                        "importance": memory.importance,
                        "similarity": score,
                    }
                )
        return {
            "profile": profile_text,
            "relevant_memories": memories,
            "job_text": job_text,
        }


    def plan_agent_searches(
        self,
        model_config: AiModelConfig,
        user_config: dict,
        location: str,
        radius_km: int,
        saved_searches: dict | None = None,
        current_query: str = "",
        feedback: list[dict] | None = None,
        additional_instructions: str = "",
    ) -> dict:
        """Create a diverse BA search plan and refine it from prior outcomes."""
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        language = self._output_language(user_config)
        system_prompt = (
            "You plan creative but realistic job searches for the German Bundesagentur für Arbeit job search. "
            "Use the user profile and previous search outcomes. Return valid JSON only. "
            "Search terms should be short enough for a normal keyword field and should vary titles, skills and domains. "
            "Do not simply paginate one broad query forever."
        )
        user_prompt = (
            f"USER PROFILE:\n{profile_text}\n\n"
            f"SEARCH AREA: {location}, radius {radius_km} km\n\n"
            "CURRENT SEARCH FIELD (inspiration only, not mandatory):\n"
            f"{current_query}\n\n"
            "SAVED SEARCHES (inspiration only):\n"
            f"{json.dumps(saved_searches or {}, ensure_ascii=False, indent=2)}\n\n"
            "PREVIOUS SEARCH OUTCOMES:\n"
            f"{json.dumps(feedback or [], ensure_ascii=False, indent=2)}\n\n"
            "ADDITIONAL USER INSTRUCTIONS FOR THIS RUN:\n"
            f"{str(additional_instructions or '').strip() or '(none)'}\n\n"
            "Create 8 to 12 diverse search ideas. Derive concrete titles, skills, technologies and domains from the user profile. "
            "Include German and English variants where useful, but do not inject preferences that are not present in the profile. "
            "Follow the additional user instructions unless they conflict with safety or the available BA search interface. "
            "Avoid repeating failed or near-identical queries from the feedback. Keep each query concise. "
            "Return EXACTLY this JSON shape:\n"
            "{\n"
            f"  \"strategy\": \"short {language} explanation\",\n"
            "  \"queries\": [\n"
            "    {\"query\": \"...\", \"reason\": \"why this may work\"}\n"
            "  ]\n"
            "}"
        )
        client = create_ai_client(model_config)
        data, raw = self._complete_json_object(
            client, system_prompt, user_prompt, operation="AI search planning"
        )
        queries = data.get("queries")
        if not isinstance(queries, list):
            data["queries"] = []
        data.setdefault("raw_response", raw)
        return data

    def screen_jobs_from_table(
        self,
        rows: list[dict],
        model_config: AiModelConfig,
        user_config: dict,
    ) -> dict:
        """Ask an AI model for a quick shortlist based on table columns only."""
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        language = self._output_language(user_config)
        allowed_industries = [str(v).strip() for v in user_config.get("ai_allowed_industries", []) if str(v).strip()]
        configured_reasons = user_config.get("reject_reasons", [])
        if not isinstance(configured_reasons, list):
            configured_reasons = []
        system_prompt = (
            "You are a cautious job-search assistant for one specific user. "
            "Use the profile as the primary evaluation standard. "
            "Only judge whether a job is worth closer inspection based on the table data. "
            "You may use common public knowledge about well-known companies only for rough triage, but do not invent concrete facts. "
            "Detect obvious ANÜ/Arbeitnehmerüberlassung and fixed-term signals from title, company, term and location text. "
            f"Write all user-facing natural-language output in {language}. Return valid JSON."
        )
        user_prompt = (
            "USER PROFILE:\n"
            f"{profile_text}\n\n"
            "TASK:\n"
            "Screen the selected jobs using ONLY the table data below. Decide which jobs are potentially worth closer inspection. "
            "Do not do a full job analysis. Use distance, title, company, industry, salary, term and location. "
            "This is a triage step: estimate whether it is worth opening the details. Use the user profile as the sole source of personal preferences and exclusions. "
            "Do not assume that any industry, contract type, technology, seniority level or work style is desirable or undesirable unless the profile says so.\n"
            "Screening scores must be integers from 0 to 25. 0 means not worth checking, 25 means very high priority for a detail check. This is NOT a full fit score; estimate rough potential from title, company, industry, location and commute. Do not use decimals.\n"
            "If a job is obviously ANÜ/Arbeitnehmerüberlassung from title/company/table text, set is_anue=true. If it is obviously fixed-term/befristet/fixed term from title or term, set is_fixed_term=true. Otherwise false.\n"
            "If a job has no industry in the table, choose an industry ONLY if it is exactly one of ALLOWED INDUSTRIES. If none fits exactly, leave industry empty but still provide likely_industry.\n\n"
            "ALLOWED INDUSTRIES:\n"
            f"{json.dumps(allowed_industries, ensure_ascii=False, indent=2)}\n\n"
            "For each item also state confidence (high|medium|low). Set hard_exclusion=true only when the table data and user profile together show an explicit, decisive blocker. Do not treat a merely imperfect skill match or uncertain seniority as a hard exclusion. Add a short exclusion_reason and interesting_signals (a list of useful clues). For an unlikely/red job, interesting_signals must be empty unless the title, company or metadata gives a concrete indication that the detailed description could reveal a materially better-fitting role than the first impression suggests. Do not invent such signals merely to justify a detail analysis.\n"
            "For every unlikely job, also return reject_reason as a short noun phrase only, for example 'central SAP consulting role' or 'sales role'. Do not append 'instead of ...'. Select rejection_tags only from the configured list below and never invent tags. If none matches, use an empty list.\n\n"
            "CONFIGURED REJECTION TAGS WITH EXPLANATIONS:\n"
            f"{json.dumps(configured_reasons, ensure_ascii=False, indent=2)}\n\n"
            "Return EXACTLY this JSON shape:\n"
            "{\n"
            f"  \"chat_text\": \"Markdown-like {language} summary with bullets. Use **bold** for job titles and short reasons.\",\n"
            "  \"items\": [\n"
            f"    {{\"iid\": \"...\", \"decision\": \"potential|maybe|unlikely\", \"score\": 0, \"confidence\": \"high|medium|low\", \"hard_exclusion\": false, \"exclusion_reason\": \"short reason or empty\", \"reject_reason\": \"short noun phrase or empty\", \"rejection_tags\": [\"existing tag\"], \"interesting_signals\": [\"simulation\"], \"industry\": \"exact allowed industry or empty\", \"likely_industry\": \"free text guess\", \"is_anue\": false, \"is_fixed_term\": false, \"reason\": \"short {language} reason\"}}\n"
            "  ]\n"
            "}\n\n"
            "JOBS:\n"
            f"{json.dumps(rows, ensure_ascii=False, indent=2)}"
        )
        client = create_ai_client(model_config)
        data, raw = self._complete_json_object(
            client, system_prompt, user_prompt, operation="AI job screening"
        )
        items = data.get("items")
        if isinstance(items, list):
            # Canonicalize model-returned tags case-insensitively. Models sometimes
            # reproduce an allowed tag with different capitalization even though the
            # wording itself is correct (for example "embedded focus" instead of
            # "Embedded focus"). Do not discard such valid suggestions.
            allowed_tags_by_key = {
                str(configured.get("name") or "").strip().casefold(): str(configured.get("name") or "").strip()
                for configured in configured_reasons
                if isinstance(configured, dict) and str(configured.get("name") or "").strip()
            }
            for item in items:
                if not isinstance(item, dict):
                    continue
                tags = item.get("rejection_tags")
                if not isinstance(tags, list):
                    tags = []
                canonical_tags = []
                seen_tags = set()
                for tag in tags:
                    canonical = allowed_tags_by_key.get(str(tag or "").strip().casefold())
                    if canonical and canonical.casefold() not in seen_tags:
                        seen_tags.add(canonical.casefold())
                        canonical_tags.append(canonical)
                item["rejection_tags"] = canonical_tags
                if str(item.get("decision") or "").strip().lower() == "unlikely" and not str(item.get("reject_reason") or "").strip():
                    item["reject_reason"] = str(item.get("exclusion_reason") or item.get("reason") or "").strip()
        data.setdefault("raw_response", raw)
        return data

    @staticmethod
    def _extract_detail_score_from_rating(rating: str) -> int | None:
        """Extract the score written inside the human-readable rating text.

        Detail-analysis prompts require a 0..100 score. Some models nevertheless
        returned a JSON score of 80/100 while writing ``Score: 8`` and
        ``Unlikely`` in the rating. The prose score is closer to the actual
        reasoning and is therefore used as the consistency anchor.
        """
        text = str(rating or "")
        patterns = (
            r"(?im)^\s*[-*]?\s*\*{0,2}score\s*:?\*{0,2}\s*:?\s*(\d{1,3})(?:\s*/\s*100)?\b",
            r"(?im)^\s*score\s*[:=-]\s*(\d{1,3})(?:\s*/\s*100)?\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = int(match.group(1))
            if 0 <= value <= 100:
                return value
        return None

    @staticmethod
    def _strict_detail_score(value) -> int | None:
        if value is None or value == "":
            return None
        try:
            number = float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
        number = round(number)
        if 0 <= number <= 100:
            return int(number)
        return None

    @classmethod
    def _normalize_detail_items(cls, data: dict) -> dict:
        """Make JSON score, visible rating score and decision consistent."""
        items = data.get("items")
        if not isinstance(items, list):
            return data
        for item in items:
            if not isinstance(item, dict):
                continue
            json_score = cls._strict_detail_score(item.get("score"))
            rating_score = cls._extract_detail_score_from_rating(item.get("rating", ""))

            # Prefer the score explicitly written in the detailed reasoning. It
            # catches the common 8 -> 80 and 10 -> 100 wrapper inconsistency.
            score = rating_score if rating_score is not None else json_score
            if score is not None:
                item["score"] = score
                item["decision"] = (
                    "potential" if score >= 70 else "maybe" if score >= 45 else "unlikely"
                )
            elif str(item.get("decision") or "").strip().lower() not in {
                "potential", "maybe", "unlikely"
            }:
                item["decision"] = "maybe"
        return data

    @staticmethod
    def _parse_json_object(text: str) -> dict:
        """Parse model JSON even when it is fenced or contains raw control chars."""
        raw = str(text or "").strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()

        candidates = [raw]
        obj_start = raw.find("{")
        obj_end = raw.rfind("}")
        if obj_start >= 0 and obj_end > obj_start and (obj_start != 0 or obj_end != len(raw) - 1):
            candidates.append(raw[obj_start : obj_end + 1])

        last_error = None
        for candidate in candidates:
            for strict in (True, False):
                try:
                    return json.loads(candidate, strict=strict)
                except json.JSONDecodeError as exc:
                    last_error = exc
        if last_error is not None:
            raise last_error
        raise json.JSONDecodeError("No JSON object found", raw, 0)

    def _complete_json_object(
        self,
        client,
        system_prompt: str,
        user_prompt: str,
        *,
        operation: str,
        max_attempts: int = 3,
    ) -> tuple[dict, str]:
        """Request structured JSON and retry malformed model responses."""
        prompt = user_prompt
        last_error = None
        last_raw = ""
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            last_raw = client.complete(system_prompt, prompt)
            self._capture_usage(client)
            try:
                data = self._parse_json_object(last_raw)
                if not isinstance(data, dict):
                    raise RuntimeError("AI response did not contain a JSON object.")
                return data, last_raw
            except (json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                prompt = (
                    user_prompt
                    + "\n\nIMPORTANT CORRECTION REQUEST:\n"
                    + f"Your previous response for {operation} could not be parsed as JSON. "
                    + f"Parser error: {exc}. Return the complete answer again as valid JSON only. "
                    + "Escape all newlines, tabs, quotation marks and backslashes inside JSON strings. "
                    + "Do not use Markdown fences or explanatory prose outside the JSON object.\n\n"
                    + "PREVIOUS INVALID RESPONSE:\n"
                    + last_raw[:12000]
                )
        message = f"{operation} failed after {attempts} attempts"
        if last_error is not None:
            message += f": {last_error}"
        raise RuntimeError(message)


    def analyze_jobs_in_detail(
        self,
        jobs: list[dict],
        model_config: AiModelConfig,
        user_config: dict,
    ) -> dict:
        """Create temporary AI summary/rating for selected jobs using full job data."""
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        language = self._output_language(user_config)
        is_english = language == "English"
        rejection_line_label = "Rejection reason" if is_english else "Ablehnungsgrund"
        rejection_tags_label = "Would use rejection tags" if is_english else "Passende Ablehnungs-Tags"
        none_word = "none" if is_english else "keine"
        allowed_industries = [str(v).strip() for v in user_config.get("ai_allowed_industries", []) if str(v).strip()]
        configured_reasons = user_config.get("reject_reasons", [])
        if not isinstance(configured_reasons, list):
            configured_reasons = []
        system_prompt = (
            "You are a cautious job-search assistant for one specific user. "
            "Use the profile as the primary evaluation standard. "
            "Analyze the selected jobs using their full job description, notes and raw data. "
            "Do not invent facts. If company background is unclear without web research, say so. "
            "Return valid JSON only."
        )
        if is_english:
            detail_format = (
                "AI SUMMARY FORMAT for each job:\n"
                "## Quick overview\n"
                "- **Bottom line / show-stopper first:** If there is a massive blocker according to the user profile or explicit job constraints, mention it FIRST and very clearly.\n"
                "- **What does the company work on?** Explain the company/domain context in plain language. Use information from the posting, e.g. division/team/product context, not only task keywords.\n"
                "- **What does the team work on?** Explain the team/product area and why it exists.\n"
                "- **What would I do all day?** State the likely day-to-day work in concrete terms, not just technologies.\n"
                "- **Matching skills:** ...\n"
                "- **Gaps / unclear:** ...\n"
                "- **Warning signs:** ...\n\n"
                "AI RATING FORMAT for each job:\n"
                "## Rating\n"
                "- **Score:** integer 0-100\n"
                "- **Likely Industry:** ...\n"
                "- **Overall assessment:** Start with the main blocker or main positive reason.\n"
                "- **Company / team / day-to-day:** Explicitly evaluate: What does the company do? What does the team do? What would the user likely do all day?\n"
                "- **Technical fit:** ...\n"
                "- **Interest / purpose:** ...\n"
                "- **Work-style fit:** assess the role against any work-style preferences or constraints explicitly stated in the user profile.\n"
                "- **Location / contract:** ...\n"
                "- **Risks / no-gos:** make show-stoppers visually prominent.\n"
                "- **Recommendation:** ...\n\n"
            )
        else:
            detail_format = (
                "AI SUMMARY FORMAT for each job:\n"
                "## Kurzüberblick\n"
                "- **Kurzfazit / Show-Stopper zuerst:** If there is a massive blocker according to the user profile or explicit job constraints, mention it FIRST and very clearly.\n"
                "- **Woran arbeitet die Firma?** Explain the company/domain context in plain language. Use information from the posting, e.g. division/team/product context, not only task keywords.\n"
                "- **Woran arbeitet das Team?** Explain the team/product area and why it exists.\n"
                "- **Was mache ich den ganzen Tag?** State the likely day-to-day work in concrete terms, not just technologies.\n"
                "- **Passende Skills:** ...\n"
                "- **Lücken / Unklar:** ...\n"
                "- **Warnsignale:** ...\n\n"
                "AI RATING FORMAT for each job:\n"
                "## Bewertung\n"
                "- **Score:** integer 0-100\n"
                "- **Likely Industry:** ...\n"
                "- **Gesamturteil:** Start with the main blocker or main positive reason.\n"
                "- **Firma / Team / Alltag:** Explicitly evaluate: What does the company do? What does the team do? What would the user likely do all day?\n"
                "- **Technische Passung:** ...\n"
                "- **Interesse / Sinn:** ...\n"
                "- **Arbeitsstil-Fit:** assess the role against any work-style preferences or constraints explicitly stated in the user profile.\n"
                "- **Standort / Vertrag:** ...\n"
                "- **Risiken / No-Gos:** make show-stoppers visually prominent.\n"
                "- **Empfehlung:** ...\n\n"
            )

        user_prompt = (
            "USER PROFILE:\n"
            f"{profile_text}\n\n"
            "TASK:\n"
            f"For each selected job, produce a compact AI summary and a more detailed AI rating in {language}. "
            "Use clean Markdown-style formatting with headings, short bullets and **bold keywords**. "
            "Avoid long paragraphs. Make the result easy to scan in a small UI text panel.\n\n"
            f"{detail_format}"
            f"For every unlikely/rejected job, add a clearly visible line near the top of AI SUMMARY: **{rejection_line_label}:** followed by a short noun phrase only. Do not append an explanatory 'instead of ...' clause. "
            f"Also add **{rejection_tags_label}:** followed only by matching tag names from the configured list below. Never invent a new tag. If no configured tag matches, write '{none_word}'. "
            "Return the same information in JSON fields reject_reason and rejection_tags. For potential/maybe jobs these may be empty.\n\n"
            "CONFIGURED REJECTION TAGS WITH EXPLANATIONS:\n"
            f"{json.dumps(configured_reasons, ensure_ascii=False, indent=2)}\n\n"
            "The AI summary must not merely list technologies. It must explain the actual domain/context and the likely everyday work. "
            "The summary must start with company/location/commute information, then show-stoppers must be prominent near the top and not hidden later in the text. "
            "The AI rating should be more detailed and include notes, risks, profile-specific work-style fit, relevant contract/location constraints, commute where available, and whether it should be examined further. Do not invent personal preferences that are absent from the profile.\n"
            "Scores must be integers from 0 to 100. 0 means unsuitable, 100 means excellent fit. Do not use 0..10 scores and do not use decimals. The JSON score, the score written in the rating text, and the decision must be mutually consistent and use the exact same 0..100 value.\n"
            "If the job/company has no industry, choose an industry ONLY if it is exactly one of ALLOWED INDUSTRIES. If none fits exactly, leave industry empty but still mention likely_industry in the rating header.\n\n"
            "ALLOWED INDUSTRIES:\n"
            f"{json.dumps(allowed_industries, ensure_ascii=False, indent=2)}\n\n"
            "Return EXACTLY this JSON shape:\n"
            "{\n"
            f"  \"chat_text\": \"Short {language} Markdown-like overview of the results.\",\n"
            "  \"items\": [\n"
            "    {\"iid\": \"...\", \"summary\": \"...\", \"rating\": \"...\", \"score\": 0, \"decision\": \"potential|maybe|unlikely\", \"reject_reason\": \"short noun phrase or empty\", \"rejection_tags\": [\"existing tag\"], \"industry\": \"exact allowed industry or empty\", \"likely_industry\": \"free text guess\"}\n"
            "  ]\n"
            "}\n\n"
            "JOBS:\n"
            f"{json.dumps(jobs, ensure_ascii=False, indent=2)}"
        )
        client = create_ai_client(model_config)
        data, raw = self._complete_json_object(
            client, system_prompt, user_prompt, operation="detailed job analysis"
        )
        self._normalize_detail_items(data)
        items = data.get("items")
        if isinstance(items, list):
            allowed_tags_by_key = {
                str(configured.get("name") or "").strip().casefold(): str(configured.get("name") or "").strip()
                for configured in configured_reasons
                if isinstance(configured, dict) and str(configured.get("name") or "").strip()
            }
            for item in items:
                if not isinstance(item, dict) or str(item.get("decision") or "").lower() != "unlikely":
                    continue
                reason = str(item.get("reject_reason") or "").strip()
                tags = item.get("rejection_tags")
                if not isinstance(tags, list):
                    tags = []
                canonical_tags = []
                seen_tags = set()
                for tag in tags:
                    canonical = allowed_tags_by_key.get(str(tag or "").strip().casefold())
                    if canonical and canonical.casefold() not in seen_tags:
                        seen_tags.add(canonical.casefold())
                        canonical_tags.append(canonical)
                tags = canonical_tags
                item["rejection_tags"] = tags
                summary = str(item.get("summary") or "").strip()
                prefix_lines = []
                summary_fold = summary.casefold()
                if reason and "rejection reason:" not in summary_fold and "ablehnungsgrund:" not in summary_fold:
                    prefix_lines.append(f"- **{rejection_line_label}:** {reason}")
                if "would use rejection tags:" not in summary_fold and "passende ablehnungs-tags:" not in summary_fold:
                    prefix_lines.append(f"- **{rejection_tags_label}:** {', '.join(tags) if tags else none_word}")
                if prefix_lines:
                    item["summary"] = ("## Quick overview\n" if is_english else "## Kurzüberblick\n") + "\n".join(prefix_lines) + "\n\n" + summary
        data.setdefault("raw_response", raw)
        return data

    def chat_about_screening(
        self,
        rows: list[dict],
        conversation: list[dict],
        question: str,
        model_config: AiModelConfig,
        user_config: dict,
        context_options: dict | None = None,
    ) -> str:
        """Follow-up chat for the temporary table screening window."""
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        language = self._output_language(user_config)
        options = context_options or {}
        recent = conversation[-12:] if options.get("chat_history", True) else []
        if recent and recent[-1].get("role") == "user" and str(recent[-1].get("content") or "") == question:
            recent = recent[:-1]
        system_prompt = (
            f"You are a job-search assistant. Answer in {language}, concise and readable. "
            "Use the user profile and the screened table rows. Do not invent details beyond the visible table data. "
            "If a question requires full job descriptions or web research, say that explicitly."
        )
        blocks = []
        if options.get("user_profile", True):
            blocks.append("USER PROFILE:\n" + profile_text)
        if options.get("job_data", True):
            blocks.append("SCREENED TABLE ROWS:\n" + json.dumps(rows, ensure_ascii=False, indent=2))
        if recent:
            blocks.append("RECENT CHAT:\n" + json.dumps(recent, ensure_ascii=False, indent=2))
        blocks.append("USER QUESTION:\n" + question)
        user_prompt = "\n\n".join(blocks) + "\n"
        client = create_ai_client(model_config)
        answer = client.complete(system_prompt, user_prompt)
        self._capture_usage(client)
        return answer

    def chat_about_job(
        self,
        job_context: dict,
        conversation: list[dict],
        question: str,
        model_config: AiModelConfig,
        user_config: dict,
        context_options: dict | None = None,
    ) -> str:
        """Follow-up chat for one job or detail-analysis context."""
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        language = self._output_language(user_config)
        options = context_options or {}
        recent = conversation[-12:] if options.get("chat_history", True) else []
        if recent and recent[-1].get("role") == "user" and str(recent[-1].get("content") or "") == question:
            recent = recent[:-1]
        system_prompt = (
            f"You are a job-search assistant. Answer in {language}, concise and readable. "
            "Use the user profile and the provided job context. Do not invent facts. "
            "If web research would be required, explicitly say so."
        )
        blocks = []
        if options.get("user_profile", False):
            blocks.append("USER PROFILE:\n" + profile_text)
        if options.get("job_data", False):
            job_data = {k: v for k, v in job_context.items() if k not in {"ai_summary", "ai_rating", "notes"}}
            blocks.append("JOB DATA:\n" + json.dumps(job_data, ensure_ascii=False, indent=2))
        if options.get("ai_analysis", False):
            analysis = {k: job_context.get(k) for k in ("ai_summary", "ai_rating") if job_context.get(k)}
            if analysis:
                blocks.append("AI ANALYSIS:\n" + json.dumps(analysis, ensure_ascii=False, indent=2))
        if options.get("notes", False) and job_context.get("notes"):
            blocks.append("NOTES:\n" + str(job_context.get("notes")))
        if recent:
            blocks.append("RECENT CHAT:\n" + json.dumps(recent, ensure_ascii=False, indent=2))
        blocks.append("USER QUESTION:\n" + question)
        user_prompt = "\n\n".join(blocks) + "\n"
        client = create_ai_client(model_config)
        answer = client.complete(system_prompt, user_prompt)
        self._capture_usage(client)
        return answer

    def reformat_job_description(self, description: str, model_config: AiModelConfig) -> str:
        """Improve line breaks and bullet structure without changing the content."""
        system_prompt = (
            "You re-format copied job descriptions for readability. "
            "Do not summarize, translate, add, remove or reinterpret content. "
            "Only improve structure: line breaks, blank lines, headings if already implied, and bullet lists. "
            "Return only the re-formatted job description text."
        )
        user_prompt = (
            "Re-format the following job description so it is easier to read in a plain text UI. "
            "Keep all factual content unchanged. Preserve language. Do not add explanations.\n\n"
            "JOB DESCRIPTION:\n"
            f"{description}"
        )
        client = create_ai_client(model_config)
        answer = client.complete(system_prompt, user_prompt).strip()
        self._capture_usage(client)
        return answer


    def parse_jobs_from_list_text(
        self,
        text: str,
        source: str,
        model_config: AiModelConfig,
        user_config: dict,
    ) -> dict:
        """Parse a pasted job-result list into structured job candidates.

        This is intended for copied LinkedIn/portal result lists where usually only
        title, company, location, publication age and sometimes salary are visible.
        """
        profile_path = str(user_config.get("ai_profile_path") or "").strip() or None
        profile_text = load_profile_text(profile_path)
        language = self._output_language(user_config)
        system_prompt = (
            "You extract structured job listings from noisy copied job-search result text. "
            "Return valid JSON only. Do not evaluate the jobs. Do not invent missing fields. "
            "If a field is unknown, use an empty string or null. Preserve German location names. "
            "The same title may appear duplicated inside one card; deduplicate it. "
            "Salaries like '52K €/yr - 80K €/yr' should become min_salary_k=52 and max_salary_k=80. "
        )
        user_prompt = (
            "USER PROFILE FOR CONTEXT ONLY - DO NOT FILTER JOBS BASED ON IT:\n"
            f"{profile_text}\n\n"
            "SOURCE NAME:\n"
            f"{source}\n\n"
            "TASK:\n"
            "Extract all recognizable job listings from the copied text. The input may come from LinkedIn or similar pages. "
            "Typical fields are title, company, location, country, publication date/age, salary and a tiny amount of metadata. Keep relative publication ages like 'Vor 4 Tagen' verbatim in published_date; JobRadar will normalize them after parsing. "
            "Return EXACTLY this JSON shape:\n"
            "{\n"
            f"  \"chat_text\": \"Short {language} note with number of parsed jobs and any uncertainty.\",\n"
            "  \"items\": [\n"
            "    {\"title\": \"...\", \"company\": \"...\", \"location\": \"...\", \"published_date\": \"...\", \"min_salary_k\": null, \"max_salary_k\": null, \"url\": \"\", \"description\": \"\", \"raw_excerpt\": \"short original snippet for this listing\"}\n"
            "  ]\n"
            "}\n\n"
            "COPIED TEXT:\n"
            f"{text}"
        )
        client = create_ai_client(model_config)
        data, raw = self._complete_json_object(
            client, system_prompt, user_prompt, operation="AI list parsing"
        )
        data.setdefault("raw_response", raw)
        if not isinstance(data.get("items"), list):
            data["items"] = []
        return data

    def parse_memory_updates(self, raw_response: str) -> list[dict]:
        """Extract memory updates from a structured model response.

        Expected future response shape:
        {
            "evaluation": {...},
            "memory_updates": [
                {"text": "...", "confidence": 0.9, "importance": 0.8}
            ]
        }
        """
        try:
            data = json.loads(raw_response)
        except json.JSONDecodeError:
            return []
        updates = data.get("memory_updates", []) if isinstance(data, dict) else []
        return updates if isinstance(updates, list) else []

    def save_memory_updates_if_allowed(
        self,
        updates: list[dict],
        user_config: dict,
        embedding_config: EmbeddingConfig | None = None,
    ) -> int:
        if not bool(user_config.get("ai_auto_memory_enabled", True)):
            return 0
        min_conf = float(user_config.get("ai_auto_memory_min_confidence", 0.85) or 0.85)
        min_importance = float(user_config.get("ai_auto_memory_min_importance", 0.7) or 0.7)
        config = embedding_config or EmbeddingConfig.from_dict(user_config.get("ai_embedding"))
        memory_service = AiMemoryService(self.db, config)
        saved = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            text = str(update.get("text") or "").strip()
            confidence = float(update.get("confidence", 0.0) or 0.0)
            importance = float(update.get("importance", 0.0) or 0.0)
            category = str(update.get("category") or "preference").strip()
            if not text or confidence < min_conf or importance < min_importance:
                continue
            memory_service.add_memory(
                text=text,
                category=category,
                source="auto",
                confidence=confidence,
                importance=importance,
            )
            saved += 1
        return saved
