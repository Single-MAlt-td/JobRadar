from __future__ import annotations

import csv
import hashlib
import html
import difflib
import json
import os
import re
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
import tkinter as tk
import tkinter.font as font
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional
from urllib.parse import unquote, urlparse

from .database import JobDatabase
from .importers import BAJobImporter, PastedListImporter
from .company_watch import (
    CompanyWatchAdapterRegistry,
    CompanyWatchError,
    build_career_source_diagnostic,
    candidate_matches_country,
)
from .models import STATUS_VALUES, JobCandidate, JobRecord, utc_now_iso
from .routing import create_router, RouteResult, GeocodeResult
from .ai import (
    AgentController,
    AgentRunConfig,
    AgentTools,
    AiEvaluationService,
    AiModelConfig,
    AiToolRegistry,
)


USER_CONFIG_PATH = Path("user.json")
SYSTEM_REJECT_REASON_CATEGORY = "General"
SYSTEM_REJECT_REASONS = {
    "duplicate": "Automatically assigned when JobRadar identifies the job as an exact duplicate of an existing job.",
    "blacklisted company": "Automatically assigned when the employer is on the company blacklist.",
    "wrong domain": "Automatically assigned when AI screening identifies a job as clearly outside the desired professional domain.",
}

DEFAULT_REJECT_REASONS = [
    "ANÜ",
    "too far away",
    "skills don't fit",
    "boring",
    "wrong domain",
    "bad company fit",
    "bad job fit",
]

DEFAULT_REJECT_REASON_DESCRIPTIONS = {
    "ANÜ": "The job is Arbeitnehmerüberlassung (employee leasing / temporary agency employment).",
    "too far away": "The job location or expected commute is too far away.",
    "skills don't fit": "The required skills or experience do not match the user's profile sufficiently.",
    "boring": "The role or its day-to-day work appears uninteresting or insufficiently engaging.",
    "bad company fit": "The company itself does not fit the user's preferences, independently of the specific role.",
    "bad job fit": "The job does not sufficiently match the user's preferences and desired type of work.",
}

DARK_THEME = {
    "bg": "#1e1f22",
    "panel": "#25262a",
    "panel2": "#2b2d31",
    "fg": "#d7d9df",
    "muted_fg": "#aeb3bd",
    "entry_bg": "#161719",
    "entry_fg": "#e6e6e6",
    "select_bg": "#3f5f8f",
    "select_fg": "#ffffff",
    "button_bg": "#33363d",
    "button_active": "#454955",
    "border": "#3b3d44",
}

STATUS_DARK_COLORS = {
    "status_new": "#25262a",
    "status_rejected": "#4a2426",
    "status_expired": "#4a2426",
    "status_needs_review": "#4a4324",
    "status_maybe": "#3c4726",
    "status_interesting": "#5a3d22",
    "status_apply": "#263f2b",
    "status_applied": "#1f4a2f",
    "status_interview": "#17613a",
}

STATUS_CATEGORY_ORDER = ["green", "blue", "red", "uncolored"]
SYSTEM_STATUS_CATEGORIES = {
    "green": ["apply", "maybe", "interesting", "needs review", "probably not"],
    "blue": ["applied", "interview", "contract"],
    "red": ["expired", "rejected by me", "rejected by company"],
    "uncolored": ["new"],
}
DEFAULT_STATUS_CATEGORIES = {
    "green": list(SYSTEM_STATUS_CATEGORIES["green"]),
    "blue": list(SYSTEM_STATUS_CATEGORIES["blue"]),
    "red": list(SYSTEM_STATUS_CATEGORIES["red"]),
    "uncolored": ["new"],
}
STATUS_CATEGORY_LABELS = {
    "green": "Green / Yellow (potential jobs)",
    "blue": "Blue / Cyan (application in progress)",
    "red": "Orange / Red (outsorted jobs)",
    "uncolored": "Uncolored (general)",
}

STATUS_LABEL_ALIASES = {
    "needs_review": "needs review",
    "rejected_by_me": "rejected by me",
    "rejected_by_company": "rejected by company",
}



@dataclass
class DisplayJob:
    """A row in the GUI. It can be a stored DB job or an unsaved search result."""

    iid: str
    db_id: int | None
    candidate: JobCandidate | None
    record: JobRecord | None
    status: str
    manual_score: int | None
    branch: str
    notes: str

    @property
    def source(self) -> str:
        return self.record.source if self.record else (self.candidate.source if self.candidate else "")

    @property
    def source_job_id(self) -> str:
        return self.record.source_job_id if self.record else (self.candidate.source_job_id if self.candidate else "")

    @property
    def title(self) -> str:
        return self.record.title if self.record else (self.candidate.title if self.candidate else "")

    @property
    def company(self) -> str:
        return self.record.company if self.record else (self.candidate.company if self.candidate else "")

    @property
    def location(self) -> str:
        return self.record.location if self.record else (self.candidate.location if self.candidate else "")

    @property
    def country(self) -> str:
        return self.record.country if self.record else (self.candidate.country if self.candidate else "")

    @property
    def url(self) -> str:
        return self.record.url if self.record else (self.candidate.url if self.candidate else "")

    @property
    def description(self) -> str:
        return self.record.description if self.record else (self.candidate.description if self.candidate else "")

    @property
    def formatted_description(self) -> str:
        return self.record.formatted_description if self.record else (self.candidate.formatted_description if self.candidate else "")

    @property
    def published_date(self) -> str:
        return self.record.published_date if self.record else (self.candidate.published_date if self.candidate else "")

    @property
    def min_salary_k(self) -> float | None:
        return self.record.min_salary_k if self.record else (self.candidate.min_salary_k if self.candidate else None)

    @property
    def max_salary_k(self) -> float | None:
        return self.record.max_salary_k if self.record else (self.candidate.max_salary_k if self.candidate else None)

    @property
    def fixed_term(self) -> str:
        return self.record.fixed_term if self.record else (self.candidate.fixed_term if self.candidate else "-")

    @property
    def status_changed_at(self) -> str:
        if self.record is not None:
            return self.record.status_changed_at or ""
        if self.candidate is not None:
            return self.candidate.status_changed_at or ""
        return ""

    @property
    def route_distance_km(self) -> float | None:
        return self.record.route_distance_km if self.record else (self.candidate.route_distance_km if self.candidate else None)

    @property
    def route_duration_min(self) -> float | None:
        return self.record.route_duration_min if self.record else (self.candidate.route_duration_min if self.candidate else None)

    @property
    def route_address_text(self) -> str:
        return self.record.route_address_text if self.record else (self.candidate.route_address_text if self.candidate else "")

    @property
    def route_quality(self) -> str:
        return self.record.route_quality if self.record else (self.candidate.route_quality if self.candidate else "")

    @property
    def selected_location_index(self) -> int:
        if self.record is not None:
            return int(self.record.selected_location_index or 0)
        if self.candidate is not None:
            return int(self.candidate.selected_location_index or 0)
        return 0

    @property
    def selected_location_manual(self) -> bool:
        if self.record is not None:
            return bool(self.record.selected_location_manual)
        if self.candidate is not None:
            return bool(self.candidate.selected_location_manual)
        return False


class JobRadarApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("JobRadar 0.6.9.28")
        self._set_window_icon()
        self.company_watch_adapters = CompanyWatchAdapterRegistry()
        self.geometry("1450x850")
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass

        self.db = JobDatabase()
        self.ba_importer = BAJobImporter()
        self.pasted_importer = PastedListImporter()
        self.importers = {"BA": self.ba_importer}

        self.config_path = USER_CONFIG_PATH
        self.user_config = self._load_user_config()
        self.ai_service = AiEvaluationService(self.db)
        self.ai_tools = AiToolRegistry(self)
        self.ai_service.set_tools(self.ai_tools)
        self.agent_controller = AgentController(AgentTools(
            self.ai_tools,
            search_page=self._agent_search_ba_page,
            publish_candidates=self._agent_publish_candidates,
            screen_rows=self._agent_screen_rows,
            apply_screening=self._agent_apply_screening,
            build_detail_payloads=self._agent_build_detail_payloads,
            analyze_details=self._agent_analyze_details,
            apply_details=self._agent_apply_details,
            plan_searches=self._agent_plan_searches,
            record_search_outcome=self._record_agent_search_outcome,
            ui_call=self._agent_call_on_ui_thread,
        ))
        self.agent_window: dict | None = None
        self.ai_models = [AiModelConfig.from_dict(item) for item in self.user_config.setdefault("ai_models", [])]
        self.user_config.setdefault("default_ai_model", "")  # legacy fallback
        self.user_config.setdefault("ai_task_defaults", {})
        self.last_ai_model_name = str(self.user_config.get("default_ai_model") or "").strip()
        self.last_ai_model_by_task: dict[str, str] = {}
        self.user_config.setdefault("show_ai_tabs_by_default", True)
        self.user_config.setdefault("ai_profile_path", "templates/profile_template.md")
        self.user_config.setdefault("ai_detail_batch_size", 4)
        self.user_config.setdefault("ai_screening_batch_size", 10)
        self.user_config.setdefault("ai_output_language", "German")
        self.saved_searches = self.user_config.setdefault("saved_searches", {})
        self.reject_reasons = self.user_config.setdefault("reject_reasons", list(DEFAULT_REJECT_REASONS))
        self.reject_reason_descriptions = self.user_config.setdefault("reject_reason_descriptions", {})
        for reason, description in DEFAULT_REJECT_REASON_DESCRIPTIONS.items():
            self.reject_reason_descriptions.setdefault(reason, description)
        self.reject_reason_categories = self.user_config.setdefault("reject_reason_categories", ["Uncategorized"])
        if "Uncategorized" not in self.reject_reason_categories:
            self.reject_reason_categories.insert(0, "Uncategorized")
        if SYSTEM_REJECT_REASON_CATEGORY not in self.reject_reason_categories:
            self.reject_reason_categories.append(SYSTEM_REJECT_REASON_CATEGORY)
        self.reject_reason_category_by_name = self.user_config.setdefault("reject_reason_category_by_name", {})
        # System rejection reasons are part of application logic and must exist
        # independently of a user's configuration. Canonicalize case variants,
        # keep them in the fixed General category and restore their descriptions.
        for system_reason, description in SYSTEM_REJECT_REASONS.items():
            existing = next((item for item in self.reject_reasons if str(item).casefold() == system_reason.casefold()), None)
            if existing is not None and existing != system_reason:
                index = self.reject_reasons.index(existing)
                self.reject_reasons[index] = system_reason
                old_description = self.reject_reason_descriptions.pop(existing, "")
                self.reject_reason_category_by_name.pop(existing, None)
                if old_description and system_reason not in self.reject_reason_descriptions:
                    self.reject_reason_descriptions[system_reason] = old_description
            elif existing is None:
                self.reject_reasons.append(system_reason)
            self.reject_reason_descriptions.setdefault(system_reason, description)
            self.reject_reason_category_by_name[system_reason] = SYSTEM_REJECT_REASON_CATEGORY
        # "bad job fit" is a normal user-editable default reason, but it belongs
        # in the built-in General category for a clean fresh-install setup.
        if "bad job fit" not in self.reject_reasons:
            self.reject_reasons.append("bad job fit")
        self.reject_reason_descriptions.setdefault("bad job fit", DEFAULT_REJECT_REASON_DESCRIPTIONS["bad job fit"])
        self.reject_reason_category_by_name.setdefault("bad job fit", SYSTEM_REJECT_REASON_CATEGORY)
        for reason in self.reject_reasons:
            category = str(self.reject_reason_category_by_name.get(reason, "") or "").strip()
            if category not in self.reject_reason_categories:
                self.reject_reason_category_by_name[reason] = "Uncategorized"
        self.status_categories = self._load_status_categories()
        self.status_values = self._flatten_status_categories()
        self.user_config["status_categories"] = self.status_categories
        self.user_config["status_values"] = self.status_values
        try:
            self.db.migrate_status_aliases(STATUS_LABEL_ALIASES)
        except Exception:
            pass
        self.manual_sources = self.user_config.setdefault("manual_sources", ["Manual", "LN", "IND"])
        self.manual_source_descriptions = self.user_config.setdefault("manual_source_descriptions", {})
        manual_source_defaults = {
            "Manual": "Manually added entry",
            "LN": "Entry created from a LinkedIn application",
            "IND": "Entry created from an Indeed application",
        }
        for code, description in manual_source_defaults.items():
            if code not in self.manual_sources:
                self.manual_sources.append(code)
            self.manual_source_descriptions.setdefault(code, description)
        self.manual_sources.sort(key=str.casefold)

        self.current_jobs: list[DisplayJob] = []
        self.pending_by_iid: dict[str, JobCandidate] = {}
        self.pending_import_index_by_iid: dict[str, int] = {}
        self.pending_duplicate_matches_by_iid: dict[str, list[dict]] = {}
        self.temp_routes_by_iid: dict[str, tuple[float, float, str, str]] = {}
        self.ai_screening_marks: dict[str, str] = {}
        self.temp_ai_screening_scores_by_iid: dict[str, int] = {}
        self.temp_ai_screening_details_by_iid: dict[str, dict] = {}
        self.temp_ai_evaluations_by_iid: dict[str, dict] = {}
        self.temp_industries_by_iid: dict[str, str] = {}
        self.temp_formatted_descriptions_by_iid: dict[str, str] = {}
        self.notes_dirty_iids: set[str] = set()
        self.raw_entry_cache: dict[int, dict] = {}
        self.pending_counter = 0
        self.selected_iid: str | None = None
        self.search_session_params: dict[str, str] | None = None
        self.search_session_page: int = 0
        self.search_session_page_size: int = 25
        self.search_session_buffer: list[JobCandidate] = []
        self.current_sort_column: str | None = None
        self.current_sort_reverse = False
        self._is_loading_selection = False
        self._filter_refresh_after_id: str | None = None
        self._last_tree_selection: set[str] = set()
        self.ai_running_by_iid: dict[str, set[str]] = {}
        self.background_tasks: list[dict] = []
        self._background_task_counter = 0
        self._ai_tooltip_window = None
        self._ai_tooltip_cell = None

        self._apply_dark_theme()
        self._build_ui()
        self._load_initial_search_fields()
        self.refresh_jobs()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def _display_status_label(status: str) -> str:
        status = str(status or "").strip()
        return STATUS_LABEL_ALIASES.get(status, status)

    @staticmethod
    def _status_key(status: str) -> str:
        return "".join(ch for ch in str(status or "").lower() if ch.isalnum())

    @staticmethod
    def _normalized_status_name(status: str) -> str:
        return str(status or "").strip().casefold()

    def _is_system_status(self, status: str) -> bool:
        key = self._normalized_status_name(status)
        return any(
            self._normalized_status_name(item) == key
            for values in SYSTEM_STATUS_CATEGORIES.values()
            for item in values
        )

    def _ensure_system_statuses(self, categories: dict[str, list[str]]) -> dict[str, list[str]]:
        # Remove system states from wrong groups, then ensure each exists in its required group.
        system_keys = {
            self._normalized_status_name(item): (group, item)
            for group, values in SYSTEM_STATUS_CATEGORIES.items()
            for item in values
        }
        cleaned = {key: [] for key in STATUS_CATEGORY_ORDER}
        seen: set[str] = set()
        for group in STATUS_CATEGORY_ORDER:
            for raw in categories.get(group, []):
                value = self._display_status_label(str(raw).strip())
                if not value:
                    continue
                normalized = self._normalized_status_name(value)
                target = system_keys.get(normalized)
                if target and target[0] != group:
                    continue
                if normalized not in seen:
                    cleaned[group].append(target[1] if target else value)
                    seen.add(normalized)
        for group in STATUS_CATEGORY_ORDER:
            for value in SYSTEM_STATUS_CATEGORIES.get(group, []):
                normalized = self._normalized_status_name(value)
                if normalized not in seen:
                    cleaned[group].append(value)
                    seen.add(normalized)
        return cleaned

    def _status_for_key(self, wanted_key: str, fallback: str) -> str:
        for status in self.status_values:
            if self._status_key(status) == wanted_key:
                return status
        fallback_label = self._display_status_label(fallback)
        for status in self.status_values:
            if self._status_key(status) == self._status_key(fallback_label):
                return status
        return fallback_label

    def _rejected_by_me_status(self) -> str:
        return self._status_for_key("rejectedbyme", "rejected by me")

    def _rejected_by_company_status(self) -> str:
        return self._status_for_key("rejectedbycompany", "rejected by company")

    def _expired_status(self) -> str:
        return self._status_for_key("expired", "expired")

    def _is_rejected_by_me_status(self, status: str) -> bool:
        return self._status_key(status) == "rejectedbyme"

    def _is_status_in_category(self, status: str, category: str) -> bool:
        key = self._status_key(status)
        return any(self._status_key(item) == key for item in self.status_categories.get(category, []))

    def _status_category(self, status: str) -> str:
        for category in STATUS_CATEGORY_ORDER:
            if self._is_status_in_category(status, category):
                return category
        return "uncolored"

    def _load_status_categories(self) -> dict[str, list[str]]:
        raw = self.user_config.get("status_categories")
        if not isinstance(raw, dict):
            legacy = self.user_config.get("status_values")
            categories = {key: list(values) for key, values in DEFAULT_STATUS_CATEGORIES.items()}
            if isinstance(legacy, list):
                known = {item for values in categories.values() for item in values}
                for item in legacy:
                    value = self._display_status_label(str(item).strip())
                    if value and value != "ignored" and value not in known:
                        categories["uncolored"].append(value)
                        known.add(value)
            return self._ensure_system_statuses(categories)

        categories: dict[str, list[str]] = {}
        seen: set[str] = set()
        for key in STATUS_CATEGORY_ORDER:
            values = raw.get(key, [])
            cleaned: list[str] = []
            if isinstance(values, list):
                for item in values:
                    value = self._display_status_label(str(item).strip())
                    if value and value != "ignored" and value not in seen:
                        cleaned.append(value)
                        seen.add(value)
            categories[key] = cleaned

        # Do not re-add old hard-coded underscore states. If a config exists,
        # the user-owned state lists are the source of truth.
        if not any(categories.values()):
            categories = {key: list(values) for key, values in DEFAULT_STATUS_CATEGORIES.items()}
        return self._ensure_system_statuses(categories)

    def _flatten_status_categories(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for key in STATUS_CATEGORY_ORDER:
            for status in self.status_categories.get(key, []):
                value = str(status).strip()
                if value and value not in seen and value != "ignored":
                    result.append(value)
                    seen.add(value)
        return result

    @staticmethod
    def _safe_tag_name(value: str) -> str:
        safe = "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower())
        return safe or "unknown"

    @staticmethod
    def _hex_to_rgb(color: str) -> tuple[int, int, int]:
        color = color.lstrip("#")
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)

    @staticmethod
    def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
        return "#" + "".join(f"{max(0, min(255, part)):02x}" for part in rgb)

    @classmethod
    def _interpolate_color(cls, start: str, end: str, index: int, total: int) -> str:
        if total <= 1:
            ratio = 0.0
        else:
            ratio = index / float(total - 1)
        a = cls._hex_to_rgb(start)
        b = cls._hex_to_rgb(end)
        rgb = tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))
        return cls._rgb_to_hex(rgb)

    def _status_color_map(self) -> dict[str, str]:
        colors: dict[str, str] = {}
        for status in self.status_categories.get("uncolored", []):
            colors[status] = DARK_THEME["panel"]

        gradients = {
            "green": ("#1f4a2f", "#4a4324"),
            "red": ("#5a3d22", "#4a2426"),
            "blue": ("#1e3a5f", "#155a66"),
        }
        for key, (start, end) in gradients.items():
            values = self.status_categories.get(key, [])
            for idx, status in enumerate(values):
                colors[status] = self._interpolate_color(start, end, idx, len(values))
        return colors

    # ------------------------------------------------------------------
    # Config / saved searches
    # ------------------------------------------------------------------

    def _load_user_config(self) -> dict:
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_user_config(self) -> None:
        self.config_path.write_text(json.dumps(self.user_config, indent=4, ensure_ascii=False), encoding="utf-8")

    def _default_search(self) -> dict[str, object]:
        return {"module": "BA", "query": "", "location": "", "radius": "40", "max_results": 25}

    def _current_search_params(self) -> dict[str, object]:
        return {
            "module": self.search_module_var.get().strip() or "BA",
            "query": self.query_var.get().strip(),
            "location": self.location_var.get().strip(),
            "radius": self.radius_var.get().strip(),
            "max_results": int(self.max_results_var.get()),
        }

    def _apply_search_params(self, params: dict[str, object]) -> None:
        self.search_module_var.set(str(params.get("module", "BA")))
        self.query_var.set(str(params.get("query", "")))
        self.location_var.set(str(params.get("location", "")))
        self.radius_var.set(str(params.get("radius", "40")))
        try:
            self.max_results_var.set(int(params.get("max_results", 25)))
        except Exception:
            self.max_results_var.set(25)

    def _apply_dark_theme(self) -> None:
        theme = DARK_THEME
        self.configure(bg=theme["bg"])
        self.option_add("*Background", theme["bg"])
        self.option_add("*Foreground", theme["fg"])
        self.option_add("*Entry.Background", theme["entry_bg"])
        self.option_add("*Entry.Foreground", theme["entry_fg"])
        self.option_add("*Text.Background", theme["entry_bg"])
        self.option_add("*Text.Foreground", theme["entry_fg"])
        self.option_add("*Text.insertBackground", theme["fg"])
        # Keep selected text readable even after the Text widget loses focus.
        self.option_add("*Text.inactiveSelectBackground", "#29476f")
        self.option_add("*Listbox.Background", theme["entry_bg"])
        self.option_add("*Listbox.Foreground", theme["entry_fg"])

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=theme["bg"], foreground=theme["fg"], fieldbackground=theme["entry_bg"])
        style.configure("TFrame", background=theme["bg"])
        style.configure("TLabelframe", background=theme["bg"], foreground=theme["fg"])
        style.configure("TLabelframe.Label", background=theme["bg"], foreground=theme["fg"])
        style.configure("TLabel", background=theme["bg"], foreground=theme["fg"])
        style.configure("TButton", background=theme["button_bg"], foreground=theme["fg"], bordercolor=theme["border"], focusthickness=1, focuscolor=theme["border"])
        style.map("TButton", background=[("active", theme["button_active"]), ("pressed", theme["panel2"])], foreground=[("disabled", theme["muted_fg"])])
        style.configure("Attention.TButton", background="#9a5b00", foreground="#ffffff", bordercolor="#d18b1f", focusthickness=1, focuscolor="#d18b1f")
        style.configure("AttentionFlash.TButton", background="#d18400", foreground="#ffffff", bordercolor="#d18b1f", focusthickness=1, focuscolor="#d18b1f")
        style.map("Attention.TButton", background=[("active", "#b36b00"), ("pressed", "#7a4800"), ("disabled", theme["button_bg"])], foreground=[("disabled", theme["muted_fg"])])
        style.map("AttentionFlash.TButton", background=[("active", "#e09a1f"), ("pressed", "#9a5b00"), ("disabled", theme["button_bg"])], foreground=[("disabled", theme["muted_fg"])])
        style.configure("TCheckbutton", background=theme["bg"], foreground=theme["fg"])
        style.map("TCheckbutton", background=[("active", theme["bg"]), ("selected", theme["bg"])])
        style.configure("TEntry", fieldbackground=theme["entry_bg"], foreground=theme["entry_fg"], insertcolor=theme["fg"], bordercolor=theme["border"], lightcolor=theme["border"], darkcolor=theme["border"])
        style.configure("TCombobox", fieldbackground=theme["entry_bg"], background=theme["button_bg"], foreground=theme["entry_fg"], arrowcolor=theme["fg"], bordercolor=theme["border"], lightcolor=theme["border"], darkcolor=theme["border"])
        style.map("TCombobox", fieldbackground=[("readonly", theme["entry_bg"])], foreground=[("readonly", theme["entry_fg"])], background=[("readonly", theme["button_bg"])])
        style.configure("TNotebook", background=theme["bg"], bordercolor=theme["border"])
        style.configure("TNotebook.Tab", background=theme["panel2"], foreground=theme["muted_fg"], padding=(9, 4))
        style.map(
            "TNotebook.Tab",
            background=[("disabled", "#090a0c"), ("selected", theme["button_bg"]), ("active", theme["button_active"])],
            foreground=[("disabled", "#3b3f46"), ("selected", theme["fg"]), ("active", theme["fg"])],
            padding=[("selected", (11, 5)), ("!selected", (9, 4))],
        )
        style.configure("TPanedwindow", background=theme["bg"])
        style.configure("Treeview", background=theme["panel"], fieldbackground=theme["panel"], foreground=theme["fg"], bordercolor=theme["border"], rowheight=24)
        style.configure("Treeview.Heading", background=theme["panel2"], foreground=theme["fg"], relief="flat")
        # Only the main job table draws selection colors with per-row tags.
        # Other Treeviews keep the normal ttk selection highlight.
        style.configure("Job.Treeview", background=theme["panel"], fieldbackground=theme["panel"], foreground=theme["fg"], bordercolor=theme["border"], rowheight=24)
        style.map("Job.Treeview", background=[], foreground=[])

        # Keep the insertion caret visible in the editable query combobox.
        style.configure(
            "Query.TCombobox",
            fieldbackground=theme["entry_bg"],
            background=theme["button_bg"],
            foreground=theme["entry_fg"],
            arrowcolor=theme["fg"],
            bordercolor=theme["border"],
            lightcolor=theme["border"],
            darkcolor=theme["border"],
            insertcolor=theme["fg"],
            insertwidth=2,
        )
        style.map(
            "Query.TCombobox",
            fieldbackground=[("focus", theme["entry_bg"]), ("readonly", theme["entry_bg"])],
            foreground=[("focus", theme["entry_fg"]), ("readonly", theme["entry_fg"])],
            background=[("readonly", theme["button_bg"])],
        )
        style.configure("Vertical.TScrollbar", background=theme["button_bg"], troughcolor=theme["panel"], bordercolor=theme["border"], arrowcolor=theme["fg"])
        style.configure("Horizontal.TScrollbar", background=theme["button_bg"], troughcolor=theme["panel"], bordercolor=theme["border"], arrowcolor=theme["fg"])

    def _load_initial_search_fields(self) -> None:
        self.saved_searches.pop("default", None)
        last = self.user_config.get("last_search") or self._default_search()
        self._apply_search_params(last)
        self._refresh_query_history_values()

    @staticmethod
    def _normalize_search_session_params(params: dict[str, object]) -> dict[str, str]:
        """Return the fields that identify one paged search session.

        ``max_results`` is deliberately excluded: changing the page size must
        not restart the current BA search at page 1.
        """
        return {
            "module": str(params.get("module", "BA")).strip(),
            "query": str(params.get("query", "")).strip(),
            "location": str(params.get("location", "")).strip(),
            "radius": str(params.get("radius", "")).strip(),
        }

    def _refresh_query_history_values(self) -> None:
        if not hasattr(self, "query_combo"):
            return
        values = sorted(
            {str(params.get("query") or "").strip() for params in self.saved_searches.values() if str(params.get("query") or "").strip()},
            key=str.casefold,
        )
        current = self.query_var.get().strip()
        if current and current not in values:
            values.insert(0, current)
        self.query_combo.configure(values=values)

    def _restore_query_caret(self, _event: object | None = None) -> None:
        """Ensure the editable search combobox shows a visible insertion caret."""
        if not hasattr(self, "query_combo"):
            return
        try:
            self.query_combo.after_idle(lambda: self.query_combo.configure(cursor="xterm"))
        except tk.TclError:
            pass

    def on_query_history_selected(self, _event: object | None = None) -> None:
        query = self.query_var.get().strip()
        for params in self.saved_searches.values():
            if str(params.get("query") or "").strip().casefold() == query.casefold():
                self._apply_search_params(params)
                break

    def _remember_current_search_automatically(self, params: dict[str, object]) -> None:
        query = str(params.get("query") or "").strip()
        if not query:
            return
        key = query.casefold()
        self.saved_searches[key] = dict(params)
        self.user_config["last_search"] = dict(params)
        self._refresh_query_history_values()
        self._save_user_config()

    def delete_current_search(self) -> None:
        query = self.query_var.get().strip()
        if not query:
            return
        key = next((name for name, params in self.saved_searches.items() if str(params.get("query") or "").strip().casefold() == query.casefold()), None)
        if key is None:
            messagebox.showinfo("Delete search", "The current query is not stored in the search history.", parent=self)
            return
        if not messagebox.askyesno("Delete search", f"Delete stored search '{query}'?", parent=self):
            return
        self.saved_searches.pop(key, None)
        defaults = self._default_search()
        self.query_var.set("")
        self.location_var.set(str(defaults.get("location", "")))
        self.radius_var.set(str(defaults.get("radius", "40")))
        self.search_module_var.set(str(defaults.get("module", "BA")))
        self.max_results_var.set(int(defaults.get("max_results", 25)))
        self.user_config["last_search"] = self._current_search_params()
        self._refresh_query_history_values()
        self._save_user_config()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=8)
        root.pack(fill=tk.BOTH, expand=True)

        toolbar_area = ttk.Frame(root)
        toolbar_area.pack(fill=tk.X, pady=(0, 8))

        top_toolbar_row = ttk.Frame(toolbar_area)
        top_toolbar_row.pack(fill=tk.X, pady=(0, 4))

        right_toolbar = ttk.Frame(top_toolbar_row)
        right_toolbar.pack(side=tk.RIGHT, anchor=tk.NE, padx=(8, 0))
        ttk.Button(right_toolbar, text="⚙ Settings", command=self.settings_dialog).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(right_toolbar, text="📤 Export", command=self.export_dialog).pack(fill=tk.X)

        searchbar = ttk.Frame(top_toolbar_row)
        searchbar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(searchbar, text="Source:").pack(side=tk.LEFT)
        self.search_module_var = tk.StringVar(value="BA")
        ttk.Combobox(
            searchbar,
            textvariable=self.search_module_var,
            values=list(self.importers.keys()),
            width=8,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(searchbar, text="Query:").pack(side=tk.LEFT)
        self.query_var = tk.StringVar(value="")
        self.query_combo = ttk.Combobox(searchbar, textvariable=self.query_var, width=32, style="Query.TCombobox")
        self.query_combo.pack(side=tk.LEFT, padx=(4, 4))
        self.query_combo.bind("<<ComboboxSelected>>", self.on_query_history_selected)
        self.query_combo.bind("<FocusIn>", self._restore_query_caret, add="+")
        self.query_combo.bind("<ButtonRelease-1>", self._restore_query_caret, add="+")
        self.query_combo.bind("<KeyRelease>", self._restore_query_caret, add="+")
        self.query_entry = self.query_combo
        self.delete_search_button = ttk.Button(searchbar, text="X", width=3, command=self.delete_current_search)
        self.delete_search_button.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(searchbar, text="Location:").pack(side=tk.LEFT)
        self.location_var = tk.StringVar(value="")
        self.location_entry = ttk.Entry(searchbar, textvariable=self.location_var, width=18)
        self.location_entry.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(searchbar, text="Radius:").pack(side=tk.LEFT)
        self.radius_var = tk.StringVar(value="40")
        radius_values = [str(value) for value in range(20, 501, 20)]
        ttk.Combobox(searchbar, textvariable=self.radius_var, values=radius_values, width=7).pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(searchbar, text="Max:").pack(side=tk.LEFT)
        self.max_results_var = tk.IntVar(value=25)
        ttk.Spinbox(searchbar, from_=1, to=100, textvariable=self.max_results_var, width=5).pack(side=tk.LEFT, padx=(4, 8))

        self.search_button = ttk.Button(searchbar, text="Search", command=self.run_search)
        self.search_button.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(searchbar, text="Reset", command=self.reset_search_session).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(searchbar, text="🤖 AI Agent", command=self.open_ai_agent_window).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(searchbar, text="🏢 Company Watch", command=self.open_company_watch_window).pack(side=tk.LEFT, padx=(18, 0))
        for variable in (self.search_module_var, self.query_var, self.location_var, self.radius_var, self.max_results_var):
            variable.trace_add("write", self._on_search_params_changed)

        filter_area = ttk.Frame(toolbar_area)
        filter_area.pack(fill=tk.X, pady=(2, 6))
        filterbar = ttk.LabelFrame(filter_area, text="Filters", padding=(8, 6, 8, 6))
        self.filterbar = filterbar
        filterbar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        task_frame = ttk.LabelFrame(filter_area, text="Background jobs", padding=(6, 4, 6, 4))
        task_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        self.background_task_frame = task_frame
        self.background_task_labels = []

        filterbar_row1 = ttk.Frame(filterbar)
        filterbar_row1.pack(fill=tk.X, pady=(0, 4))
        filterbar_row2 = ttk.Frame(filterbar)
        filterbar_row2.pack(fill=tk.X, pady=(0, 4))
        filterbar_row3 = ttk.Frame(filterbar)
        filterbar_row3.pack(fill=tk.X)

        ttk.Label(filterbar_row1, text="Status:").pack(side=tk.LEFT)
        self.status_filter_var = tk.StringVar(value="all")
        status_values = ["all"] + self.status_values
        self.status_filter_combo = ttk.Combobox(filterbar_row1, textvariable=self.status_filter_var, values=status_values, width=18, state="readonly")
        status_combo = self.status_filter_combo
        status_combo.pack(side=tk.LEFT, padx=(4, 8))
        status_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_jobs())

        self.show_only_new_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filterbar_row1, text="Show only new", variable=self.show_only_new_var, command=self.refresh_jobs).pack(side=tk.LEFT, padx=(0, 12))

        self.hide_anue_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filterbar_row1, text="Hide ANÜ", variable=self.hide_anue_var, command=self.refresh_jobs).pack(side=tk.LEFT, padx=(0, 12))

        self.hide_fixed_term_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filterbar_row1, text="Hide fixed-term", variable=self.hide_fixed_term_var, command=self.refresh_jobs).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(filterbar_row3, text="Visible state groups:").pack(side=tk.LEFT, padx=(0, 8))

        self.state_group_checkbuttons = []
        self.show_red_states_var = tk.BooleanVar(value=False)
        self.show_green_states_var = tk.BooleanVar(value=True)
        self.show_blue_states_var = tk.BooleanVar(value=True)
        self.show_uncolored_states_var = tk.BooleanVar(value=True)
        self._create_state_group_checkbutton(filterbar_row3, "Show red states", self.show_red_states_var, "#5a2426", "#35191b")
        self._create_state_group_checkbutton(filterbar_row3, "Show green states", self.show_green_states_var, "#1f5a35", "#183524")
        self._create_state_group_checkbutton(filterbar_row3, "Show blue states", self.show_blue_states_var, "#1f4f5a", "#182f35")
        self._create_state_group_checkbutton(filterbar_row3, "Show uncolored states", self.show_uncolored_states_var, "#34383f", "#25282d")
        self._refresh_state_group_checkbox_colors()

        ttk.Label(filterbar_row2, text="Include:").pack(side=tk.LEFT)
        self.include_filter_var = tk.StringVar()
        self.include_filter_var.trace_add("write", lambda *_args: self._schedule_filter_refresh())
        self.include_filter_entry = ttk.Entry(filterbar_row2, textvariable=self.include_filter_var, width=32)
        self.include_filter_entry.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(filterbar_row2, text="Exclude:").pack(side=tk.LEFT)
        self.exclude_filter_var = tk.StringVar()
        self.exclude_filter_var.trace_add("write", lambda *_args: self._schedule_filter_refresh())
        self.exclude_filter_entry = ttk.Entry(filterbar_row2, textvariable=self.exclude_filter_var, width=32)
        self.exclude_filter_entry.pack(side=tk.LEFT, padx=(4, 8))
        self.filter_search_texts_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filterbar_row2,
            text="Search texts",
            variable=self.filter_search_texts_var,
            command=lambda: (self.refresh_jobs(), self._highlight_active_include_terms()),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.clear_filter_button = ttk.Button(filterbar_row2, text="Clear", command=self.clear_filter)
        self.clear_filter_button.pack(side=tk.LEFT, padx=(0, 12))

        self._bind_search_shortcuts()

        self.status_label_var = tk.StringVar(value="Ready")
        status_row = ttk.Frame(root)
        status_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(status_row, textvariable=self.status_label_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.table_count_var = tk.StringVar(value="Visible: 0 | Selected: 0")
        ttk.Label(status_row, textvariable=self.table_count_var, anchor=tk.E).pack(side=tk.RIGHT, padx=(12, 0))

        paned = ttk.PanedWindow(root, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        table_frame = ttk.Frame(paned)
        paned.add(table_frame, weight=4)

        columns = (
            "id",
            "status",
            "ai",
            "status_changed",
            "score",
            "branch",
            "min_salary",
            "max_salary",
            "fixed_term",
            "published",
            "source",
            "title",
            "company",
            "dist",
            "time",
            "location",
            "country",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended", style="Job.Treeview")
        headings = {
            "id": "ID",
            "status": "Status",
            "ai": "AI",
            "status_changed": "State Change",
            "score": "Score",
            "branch": "Industry",
            "min_salary": "Min k€",
            "max_salary": "Max k€",
            "fixed_term": "Term",
            "published": "Published",
            "source": "Source",
            "title": "Title",
            "company": "Company",
            "dist": "Dist",
            "time": "T",
            "location": "Location",
            "country": "Country",
        }
        widths = {
            "id": 55,
            "status": 130,
            "ai": 45,
            "status_changed": 105,
            "score": 70,
            "branch": 135,
            "min_salary": 75,
            "max_salary": 75,
            "fixed_term": 60,
            "published": 100,
            "source": 80,
            "title": 390,
            "company": 270,
            "dist": 75,
            "time": 70,
            "location": 260,
            "country": 110,
        }
        for col in columns:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self.sort_by_column(c))
            self.tree.column(col, width=widths[col], anchor=tk.W, stretch=False)

        self._configure_tree_tags()
        yscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.bind("<Motion>", self._on_job_tree_motion, add="+")
        self.tree.bind("<Leave>", lambda _event: self._hide_ai_tooltip(), add="+")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        table_actionbar = ttk.Frame(table_frame)
        table_actionbar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(table_actionbar, text="Add entry", command=self.add_entry_dialog).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(table_actionbar, text="List import", command=self.list_import_dialog).pack(side=tk.LEFT, padx=(0, 8))
        self.save_selected_button = ttk.Button(table_actionbar, text="💾 Save selected in database", command=self.save_selected_in_database, state=tk.DISABLED)
        self.save_selected_button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(table_actionbar, text="Clear new*", command=self.clear_unsaved_results).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(table_actionbar, text="Calculate routes", command=self.calculate_routes_for_selection_or_visible).pack(side=tk.RIGHT, padx=(8, 0))

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self.on_select_job)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_table_count_label(), add="+")
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.tree.bind("<Delete>", self.delete_selected_jobs)
        self.tree.bind("<Control-a>", self.select_all_jobs)
        self.tree.bind("<Control-A>", self.select_all_jobs)

        self.context_menu = tk.Menu(self, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        self._rebuild_context_menu()

        detail_frame = ttk.Frame(paned, padding=(0, 8, 0, 0))
        paned.add(detail_frame, weight=2)

        # Hidden legacy edit controls.
        # Status/industry changes now happen through the context menu.
        # The widgets still exist because older helper methods reference them.
        editbar = ttk.Frame(detail_frame)

        ttk.Label(editbar, text="Status:").pack(side=tk.LEFT)
        self.edit_status_var = tk.StringVar(value="new")
        self.status_combo = ttk.Combobox(editbar, textvariable=self.edit_status_var, values=self.status_values, width=20, state="readonly")
        self.status_combo.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(editbar, text="Score:").pack(side=tk.LEFT)
        self.score_var = tk.StringVar()
        self.score_entry = ttk.Entry(editbar, textvariable=self.score_var, width=6)
        self.score_entry.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Label(editbar, text="Industry:").pack(side=tk.LEFT)
        self.company_rating_var = tk.StringVar()
        self.company_rating_combo = ttk.Combobox(
            editbar,
            textvariable=self.company_rating_var,
            values=self.db.list_company_ratings(),
            width=18,
        )
        self.company_rating_combo.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Button(editbar, text="Save", command=self.save_selection).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(editbar, text="Reject selected", command=self.reject_selected_jobs).pack(side=tk.LEFT, padx=(24, 0))

        detail_split = ttk.PanedWindow(detail_frame, orient=tk.HORIZONTAL)
        detail_split.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(detail_split)
        detail_split.add(left, weight=3)
        self.description_notebook = ttk.Notebook(left)
        self.description_notebook.pack(fill=tk.BOTH, expand=True)

        description_tab = ttk.Frame(self.description_notebook)
        ai_summary_tab = ttk.Frame(self.description_notebook)
        self.description_notebook.add(description_tab, text="Job description")
        self.description_notebook.add(ai_summary_tab, text="AI summary")

        desc_header = ttk.Frame(description_tab)
        desc_header.pack(fill=tk.X)
        ttk.Button(desc_header, text="Copy job description", command=self.copy_job_description).pack(side=tk.RIGHT)
        description_frame = ttk.Frame(description_tab)
        description_frame.pack(fill=tk.BOTH, expand=True)
        self.description_text = tk.Text(
            description_frame,
            wrap=tk.WORD,
            height=10,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        description_scroll = ttk.Scrollbar(description_frame, orient=tk.VERTICAL, command=self.description_text.yview)
        self.description_text.configure(yscrollcommand=description_scroll.set)
        self.description_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        description_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        ai_summary_frame = ttk.Frame(ai_summary_tab)
        ai_summary_frame.pack(fill=tk.BOTH, expand=True)
        self.ai_summary_text = tk.Text(
            ai_summary_frame,
            wrap=tk.WORD,
            height=10,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        ai_summary_scroll = ttk.Scrollbar(ai_summary_frame, orient=tk.VERTICAL, command=self.ai_summary_text.yview)
        self.ai_summary_text.configure(yscrollcommand=ai_summary_scroll.set)
        self.ai_summary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ai_summary_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        right = ttk.Frame(detail_split)
        detail_split.add(right, weight=1)
        self.notes_notebook = ttk.Notebook(right)
        self.notes_notebook.pack(fill=tk.BOTH, expand=True)

        notes_tab = ttk.Frame(self.notes_notebook)
        ai_rating_tab = ttk.Frame(self.notes_notebook)
        self.notes_notebook.add(notes_tab, text="Notes")
        self.notes_notebook.add(ai_rating_tab, text="AI rating")

        notes_header = ttk.Frame(notes_tab)
        notes_header.pack(fill=tk.X)
        self.save_notes_button = ttk.Button(notes_header, text="Save notes", command=self.save_notes_for_selection, state=tk.DISABLED)
        self.save_notes_button.pack(side=tk.RIGHT)
        notes_frame = ttk.Frame(notes_tab)
        notes_frame.pack(fill=tk.BOTH, expand=True)
        self.notes_text = tk.Text(
            notes_frame,
            wrap=tk.WORD,
            height=4,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        notes_scroll = ttk.Scrollbar(notes_frame, orient=tk.VERTICAL, command=self.notes_text.yview)
        self.notes_text.configure(yscrollcommand=notes_scroll.set)
        self.notes_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        notes_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.notes_text.bind("<<Modified>>", self._on_notes_modified)

        ai_rating_frame = ttk.Frame(ai_rating_tab)
        ai_rating_frame.pack(fill=tk.BOTH, expand=True)
        self.ai_rating_text = tk.Text(
            ai_rating_frame,
            wrap=tk.WORD,
            height=4,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        ai_rating_scroll = ttk.Scrollbar(ai_rating_frame, orient=tk.VERTICAL, command=self.ai_rating_text.yview)
        self.ai_rating_text.configure(yscrollcommand=ai_rating_scroll.set)
        self.ai_rating_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ai_rating_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._configure_markdown_text_widget(self.description_text)
        self._configure_markdown_text_widget(self.ai_summary_text)
        self._configure_markdown_text_widget(self.ai_rating_text)
        self._bind_detail_text_context_menus()
        self._set_ai_tabs_available(False)


    def _create_state_group_checkbutton(self, parent, text: str, variable: tk.BooleanVar, on_bg: str, off_bg: str) -> None:
        def changed() -> None:
            self._refresh_state_group_checkbox_colors()
            self.refresh_jobs()
        button = tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=changed,
            bg=on_bg if variable.get() else off_bg,
            fg=DARK_THEME["fg"],
            activebackground=on_bg,
            activeforeground=DARK_THEME["fg"],
            selectcolor="#111214",
            relief=tk.FLAT,
            padx=6,
            pady=2,
            highlightthickness=0,
        )
        button.pack(side=tk.LEFT, padx=(0, 10))
        self.state_group_checkbuttons.append((button, variable, on_bg, off_bg))

    def _refresh_state_group_checkbox_colors(self) -> None:
        for button, variable, on_bg, off_bg in getattr(self, "state_group_checkbuttons", []):
            try:
                bg = on_bg if variable.get() else off_bg
                button.configure(bg=bg, activebackground=bg)
            except Exception:
                pass

    def _set_ai_tabs_available(self, available: bool) -> None:
        state = "normal" if available else "disabled"
        try:
            self.description_notebook.tab(1, state=state)
            self.notes_notebook.tab(1, state=state)
        except Exception:
            return
        bg = DARK_THEME["entry_bg"] if available else "#111214"
        fg = DARK_THEME["entry_fg"] if available else "#6f747d"
        for widget in (getattr(self, "ai_summary_text", None), getattr(self, "ai_rating_text", None)):
            if widget is not None:
                try:
                    widget.configure(background=bg, foreground=fg)
                except Exception:
                    pass

    def _on_notes_modified(self, _event=None) -> None:
        widget = getattr(self, "notes_text", None)
        if widget is None:
            return
        try:
            if not widget.edit_modified():
                return
            widget.edit_modified(False)
        except Exception:
            pass
        if getattr(self, "_is_loading_selection", False):
            return
        iid = self.selected_iid
        if not iid:
            return
        # Tkinter can emit a delayed modified event after programmatic loading.
        # Only mark notes dirty when the visible text really differs from the
        # currently loaded job notes. This also prevents stale Notes* markers
        # after save/refresh cycles.
        try:
            current_notes = widget.get("1.0", tk.END).strip()
        except Exception:
            current_notes = ""
        job = self._get_display_job(iid)
        saved_notes = (job.notes or "") if job is not None else ""
        if current_notes == saved_notes:
            self.notes_dirty_iids.discard(iid)
        else:
            self.notes_dirty_iids.add(iid)
        self._update_dirty_indicators_for_iid(iid)
        self._update_attention_buttons()

    def _is_iid_dirty(self, iid: str | None) -> bool:
        if not iid:
            return False
        return iid in self.temp_ai_evaluations_by_iid or iid in self.temp_industries_by_iid or iid in self.notes_dirty_iids or iid in self.temp_formatted_descriptions_by_iid

    def _display_id(self, job: DisplayJob) -> str:
        if job.db_id is not None:
            base = str(job.db_id)
        else:
            import_index = self.pending_import_index_by_iid.get(job.iid)
            base = f"new* ({import_index})" if import_index is not None else "new*"
        if self._is_iid_dirty(job.iid) and "*" not in base:
            base += "*"
        return base

    def _display_score(self, job: DisplayJob) -> str:
        evaluation = self.temp_ai_evaluations_by_iid.get(job.iid)
        if isinstance(evaluation, dict) and isinstance(evaluation.get("score"), int):
            return str(evaluation.get("score"))
        screening_score = self.temp_ai_screening_scores_by_iid.get(job.iid)
        if isinstance(screening_score, int):
            return str(screening_score)
        return "" if job.manual_score is None else str(job.manual_score)

    def _update_dirty_indicators_for_iid(self, iid: str | None = None) -> None:
        iid = iid or self.selected_iid
        if iid and hasattr(self, "tree") and self.tree.exists(iid):
            job = self._get_display_job(iid)
            if job is not None:
                values = list(self.tree.item(iid, "values"))
                if values:
                    values[0] = self._display_id(job)
                    # Keep this aligned with the Treeview column order. The AI
                    # column was inserted after Status in 0.6.8.4.
                    column_index = {name: index for index, name in enumerate(self.tree["columns"])}
                    for name, value in (
                        ("score", self._display_score(job)),
                        ("branch", job.branch),
                        ("fixed_term", job.fixed_term or "-"),
                        ("title", self._display_title(job)),
                    ):
                        index = column_index.get(name)
                        if index is not None and index < len(values):
                            values[index] = value
                    self.tree.item(iid, values=values, tags=self._row_tags(job))
        self._update_detail_tab_labels()

    def _update_detail_tab_labels(self) -> None:
        try:
            summary_dirty = bool(self.selected_iid and self.selected_iid in self.temp_ai_evaluations_by_iid)
            rating_dirty = summary_dirty
            notes_dirty = bool(self.selected_iid and self.selected_iid in self.notes_dirty_iids)
            self.description_notebook.tab(0, text="Job description")
            self.description_notebook.tab(1, text="AI summary*" if summary_dirty else "AI summary")
            self.notes_notebook.tab(0, text="Notes*" if notes_dirty else "Notes")
            self.notes_notebook.tab(1, text="AI rating*" if rating_dirty else "AI rating")
        except Exception:
            pass

    def _set_text_content(self, widget: tk.Text, text: str, readonly: bool = True, preserve_view: bool = False) -> None:
        yview = widget.yview()[0] if preserve_view else 0.0
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        try:
            widget.edit_modified(False)
        except Exception:
            pass
        if readonly:
            widget.configure(state=tk.DISABLED)
        if preserve_view:
            try:
                widget.yview_moveto(yview)
            except tk.TclError:
                pass

    def _configure_markdown_text_widget(self, widget: tk.Text) -> None:
        widget.tag_configure("h1", foreground="#f3f4f6", font=("Segoe UI", 12, "bold"), spacing1=8, spacing3=4)
        widget.tag_configure("h2", foreground="#f3f4f6", font=("Segoe UI", 11, "bold"), spacing1=6, spacing3=3)
        widget.tag_configure("speaker", foreground="#8ab4f8", font=("Segoe UI", 10, "bold"))
        widget.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        widget.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        widget.tag_configure("bullet", lmargin1=18, lmargin2=32, spacing1=1, spacing3=1)

    def _set_markdownish_content(self, widget: tk.Text, text: str, readonly: bool = True, preserve_view: bool = False) -> None:
        yview = widget.yview()[0] if preserve_view else 0.0
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        self._insert_markdownish_text(widget, str(text or ""))
        if readonly:
            widget.configure(state=tk.DISABLED)
        if preserve_view:
            try:
                widget.yview_moveto(yview)
            except tk.TclError:
                pass

    def _load_ai_details_for_job(self, job: DisplayJob, preserve_view: bool = False) -> bool:
        evaluation = self.temp_ai_evaluations_by_iid.get(job.iid)
        if evaluation is None and job.db_id is not None:
            evaluation = self.db.get_latest_ai_evaluation(job.db_id)
        if not evaluation:
            screening = self.temp_ai_screening_details_by_iid.get(job.iid)
            if screening:
                decision = str(screening.get("decision") or "-")
                score = screening.get("score")
                reason = str(screening.get("reject_reason") or screening.get("reason") or "").strip()
                tags = screening.get("rejection_tags") if isinstance(screening.get("rejection_tags"), list) else []
                confidence = str(screening.get("confidence") or "-")
                signals = screening.get("interesting_signals") if isinstance(screening.get("interesting_signals"), list) else []
                summary_lines = [
                    "## Quick screening (preliminary, 0..25)",
                    f"- **Decision:** {decision}",
                    f"- **Screening score:** {score if score is not None else '-'} / 25",
                ]
                if decision == "unlikely":
                    summary_lines.append(f"- **Rejection reason:** {reason or 'not specified'}")
                    summary_lines.append(f"- **Would use rejection tags:** {', '.join(tags) if tags else 'none'}")
                elif reason:
                    summary_lines.append(f"- **Reason:** {reason}")
                if signals:
                    summary_lines.append(f"- **Interesting signals:** {', '.join(signals)}")
                summary_lines.append(f"- **Confidence:** {confidence}")
                rating_lines = [
                    "## No detailed rating performed",
                    "This job was only evaluated by the preliminary quick table screening; no successful 0..100 detail analysis exists yet.",
                    "The full job description was not analyzed, so this is not a complete fit assessment.",
                    "",
                    f"Screening decision: {decision}",
                    f"Screening score: {score if score is not None else '-'} / 25",
                ]
                if reason:
                    rating_lines.append(f"Screening reason: {reason}")
                if tags:
                    rating_lines.append(f"Rejection tags: {', '.join(tags)}")
                self._set_markdownish_content(self.ai_summary_text, "\n".join(summary_lines), preserve_view=preserve_view)
                self._set_markdownish_content(self.ai_rating_text, "\n".join(rating_lines), preserve_view=preserve_view)
                self._set_ai_tabs_available(True)
                return True
            self._set_ai_tabs_available(False)
            self._set_text_content(self.ai_summary_text, "No AI evaluation available yet.", preserve_view=preserve_view)
            self._set_text_content(self.ai_rating_text, "No AI evaluation available yet.", preserve_view=preserve_view)
            return False

        summary = str(evaluation.get("summary") or "").strip() or "No AI summary stored."
        temporary = " (temporary, not saved)" if job.iid in self.temp_ai_evaluations_by_iid else ""
        rating_parts = [
            f"AI: {evaluation.get('ai_name') or '-'}{temporary}",
            f"Provider/model: {evaluation.get('provider') or '-'} / {evaluation.get('model') or '-'}",
            f"Created: {evaluation.get('created_at') or '-'}",
            f"Score: {evaluation.get('score') if evaluation.get('score') is not None else '-'}",
            f"Decision: {evaluation.get('decision') or '-'}",
            "",
            str(evaluation.get("rating") or "No AI rating stored."),
        ]
        self._set_markdownish_content(self.ai_summary_text, summary, preserve_view=preserve_view)
        self._set_markdownish_content(self.ai_rating_text, "\n".join(rating_parts), preserve_view=preserve_view)
        self._set_ai_tabs_available(True)
        return True

    @classmethod
    def _lighten_color(cls, color: str, amount: float = 0.30) -> str:
        base = cls._hex_to_rgb(color)
        rgb = tuple(round(channel + (255 - channel) * amount) for channel in base)
        return cls._rgb_to_hex(rgb)

    def _configure_tree_tags(self) -> None:
        normal_colors: dict[str, str] = {}
        normal_colors.update(STATUS_DARK_COLORS)
        normal_colors.update({f"status_custom_{self._safe_tag_name(status)}": color for status, color in self._status_color_map().items()})
        normal_colors.update({
            "ai_screen_potential": "#1f5a35",
            "ai_screen_maybe": "#70451f",
            "ai_screen_unlikely": "#5a2426",
        })
        for tag, color in normal_colors.items():
            self.tree.tag_configure(tag, background=color, foreground=DARK_THEME["fg"])
            # Keep the underlying status/AI color visible for selected rows.
            # Uncolored rows use neutral light gray instead of blue so selection
            # cannot be mistaken for a blue application-status color.
            if tag in {"status_new", "status_custom_new"}:
                selected_bg = "#b8b8b8"
                selected_fg = "#111111"
            else:
                selected_bg = self._lighten_color(color)
                selected_fg = "#111111"
            self.tree.tag_configure(f"selected_{tag}", background=selected_bg, foreground=selected_fg)

    @staticmethod
    def _date_only_display(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if "T" in value:
            return value.split("T", 1)[0]
        if " " in value:
            return value.split(" ", 1)[0]
        return value[:10] if len(value) >= 10 else value

    @staticmethod
    def _published_display(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        try:
            published = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return value
        age_days = (date.today() - published).days
        if age_days < 0:
            return value
        return f"{value} ({age_days}d)"

    def _remember_detail_tab_selection(self) -> tuple[str, str]:
        left = ""
        right = ""
        try:
            left = self.description_notebook.select()
        except Exception:
            pass
        try:
            right = self.notes_notebook.select()
        except Exception:
            pass
        return left, right

    def _restore_detail_tab_selection(self, selection: tuple[str, str]) -> None:
        left, right = selection
        try:
            if left:
                self.description_notebook.select(left)
        except Exception:
            pass
        try:
            if right:
                self.notes_notebook.select(right)
        except Exception:
            pass

    def refresh_jobs_preserving_detail_tabs(self) -> None:
        selected_tabs = self._remember_detail_tab_selection()
        self.refresh_jobs()
        self._restore_detail_tab_selection(selected_tabs)

    @staticmethod
    def _salary_display(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f}"

    @staticmethod
    def _distance_display(value: float | None, quality: str = "") -> str:
        if value is None:
            return "-"
        suffix = "*" if quality == "estimated" else ""
        return f"{value:.1f} km{suffix}"

    @staticmethod
    def _duration_display(value: float | None, quality: str = "") -> str:
        if value is None:
            return "-"
        total_minutes = int(round(value))
        hours = total_minutes // 60
        minutes = total_minutes % 60
        suffix = "*" if quality == "estimated" else ""
        return f"{hours}:{minutes:02d} h{suffix}"

    def _status_tag(self, status: str) -> str:
        color_map = self._status_color_map()
        if status in color_map:
            return f"status_custom_{self._safe_tag_name(status)}"
        display_status = self._display_status_label(status)
        if display_status in color_map:
            return f"status_custom_{self._safe_tag_name(display_status)}"
        return "status_new"

    # ------------------------------------------------------------------
    # Search / import
    # ------------------------------------------------------------------

    @staticmethod
    def _select_all_entry_text(event: tk.Event) -> str:
        widget = event.widget
        try:
            widget.select_range(0, tk.END)
            widget.icursor(tk.END)
        except tk.TclError:
            pass
        return "break"

    def _bind_search_shortcuts(self) -> None:
        for entry in (self.query_entry, self.location_entry):
            entry.bind("<Return>", lambda _event: self.run_search())
            entry.bind("<Control-a>", self._select_all_entry_text)
            entry.bind("<Control-A>", self._select_all_entry_text)
        for entry in (self.include_filter_entry, self.exclude_filter_entry):
            entry.bind("<Control-a>", self._select_all_entry_text)
            entry.bind("<Control-A>", self._select_all_entry_text)

    def _search_session_label(self) -> str:
        try:
            size = int(self.max_results_var.get())
        except Exception:
            size = 25
        return f"Next {size}" if self.search_session_page > 0 else "Search"

    def _update_search_button_label(self) -> None:
        if hasattr(self, "search_button"):
            self.search_button.configure(text=self._search_session_label())

    def _on_search_params_changed(self, *_args: object) -> None:
        if not hasattr(self, "search_button"):
            return
        try:
            current = self._normalize_search_session_params(self._current_search_params())
        except Exception:
            self.search_button.configure(text="Search")
            return
        if self.search_session_params is not None and current == self.search_session_params and self.search_session_page > 0:
            self._update_search_button_label()
        else:
            self.search_button.configure(text="Search")

    def reset_search_session(self) -> None:
        self.search_session_params = None
        self.search_session_page = 0
        self.search_session_buffer.clear()
        self.pending_by_iid.clear()
        self.pending_import_index_by_iid.clear()
        self.temp_routes_by_iid.clear()
        self._update_search_button_label()
        self.refresh_jobs()
        self.status_label_var.set("Search session reset.")

    def _schedule_filter_refresh(self, delay_ms: int = 1000) -> None:
        """Debounce expensive table filtering while the user is typing."""
        pending = getattr(self, "_filter_refresh_after_id", None)
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        self._filter_refresh_after_id = self.after(delay_ms, self._run_debounced_filter_refresh)
        self._update_attention_buttons()

    def _run_debounced_filter_refresh(self) -> None:
        self._filter_refresh_after_id = None
        self.refresh_jobs()
        self._update_attention_buttons()

    def clear_filter(self) -> None:
        self.include_filter_var.set("")
        self.exclude_filter_var.set("")
        self._update_attention_buttons()

    def run_search(self) -> None:
        source = self.search_module_var.get().strip() or "BA"
        query = self.query_var.get().strip()
        location = self.location_var.get().strip()
        try:
            radius = int(self.radius_var.get())
            max_results = int(self.max_results_var.get())
        except ValueError:
            messagebox.showwarning("Invalid input", "Radius and Max must be integers.")
            return
        if radius <= 0:
            messagebox.showwarning("Invalid radius", "Radius must be greater than 0.")
            return
        if not query or not location:
            messagebox.showwarning("Missing input", "Please enter query and location.")
            return

        params = self._current_search_params()
        self._remember_current_search_automatically(params)
        normalized = self._normalize_search_session_params(params)
        same_session = self.search_session_params == normalized and self.search_session_page > 0
        append_pending = same_session
        if not same_session:
            self.search_session_page = 0
            self.search_session_buffer.clear()
            self.search_session_page_size = max_results

        # The current query is already stored automatically above.  The old
        # named-preset bookkeeping was removed together with the former
        # Saved Search controls and must not be referenced here.
        self.user_config["last_search"] = dict(params)
        self._save_user_config()

        if source != "BA":
            messagebox.showinfo("Not implemented", f"Search source '{source}' is not implemented yet.")
            return

        next_text = self._search_session_label() if same_session else "Search"
        self.status_label_var.set(f"Running {next_text.lower()}...")
        threading.Thread(
            target=self._run_search_worker,
            args=(query, location, radius, max_results, append_pending, normalized),
            daemon=True,
        ).start()

    def _run_search_worker(
        self,
        query: str,
        location: str,
        radius: int,
        max_results: int,
        append_pending: bool,
        normalized_params: dict[str, str],
    ) -> None:
        try:
            # Keep a fixed BA page size for the lifetime of a search session.
            # If the user changes Max during a session, we still fetch BA pages
            # without overlap and only display the requested amount; surplus
            # candidates stay in search_session_buffer for the next click.
            display_candidates: list[JobCandidate] = []
            fetched_pages = 0
            next_page = self.search_session_page + 1 if append_pending else 1
            page_size = max(1, int(self.search_session_page_size or max_results or 25))

            if append_pending and self.search_session_buffer:
                display_candidates.extend(self.search_session_buffer[:max_results])
                self.search_session_buffer = self.search_session_buffer[max_results:]

            while len(display_candidates) < max_results:
                candidates = self.ba_importer.search_page(query, location, radius, page=next_page, size=page_size)
                fetched_pages += 1
                next_page += 1
                if not candidates:
                    break
                need = max_results - len(display_candidates)
                display_candidates.extend(candidates[:need])
                if len(candidates) > need:
                    self.search_session_buffer.extend(candidates[need:])
                    break
                if len(candidates) < page_size:
                    break

            last_fetched_page = next_page - 1 if fetched_pages else self.search_session_page
            self.after(0, lambda: self._show_search_results(
                display_candidates,
                append_pending=append_pending,
                page=last_fetched_page,
                normalized_params=normalized_params,
            ))
        except Exception as exc:
            error_message = str(exc)
            self.after(0, lambda: messagebox.showerror("Search failed", error_message))
            self.after(0, lambda: self.status_label_var.set("Search failed"))

    def _show_search_results(
        self,
        candidates: list[JobCandidate],
        append_pending: bool = False,
        page: int | None = None,
        normalized_params: dict[str, str] | None = None,
    ) -> list[str]:
        added_iids: list[str] = []
        if not append_pending:
            self.pending_by_iid.clear()
            self.temp_routes_by_iid.clear()

        # Prepare a cheap local Direct router once so unsaved BA search results
        # with multiple locations can immediately select the nearest location.
        # This does not call OSRM/ORS; it only needs the home coordinates and
        # the BA-provided target coordinates. If home geocoding fails, we simply
        # keep the first BA location as before.
        direct_router = None
        direct_start = None
        try:
            settings = self.db.get_geo_settings()
            home_address = str(settings.get("home_address", "")).strip()
            if home_address:
                direct_router = create_router(
                    "direct",
                    request_delay_seconds=0.0,
                    ors_env_var=str(settings.get("ors_env_var", "ORS_API_KEY")),
                    geocoding_provider=str(settings.get("geocoding_provider", "nominatim")),
                )
                direct_start = self._geocode_with_cache(home_address, direct_router, str(settings.get("geocoding_provider", "nominatim")))
        except Exception:
            direct_router = None
            direct_start = None

        known = 0
        new = 0
        hidden_blacklisted = 0
        blacklisted_rejected = 0
        duplicate_pending = 0
        # Exact source IDs are authoritative. Keep one temporary row for each
        # (source, source_job_id), including duplicates returned by different
        # BA queries/pages during the same agent run.
        pending_source_keys = {
            (str(cand.source or "").strip().casefold(), str(cand.source_job_id or "").strip())
            for cand in self.pending_by_iid.values()
            if str(cand.source_job_id or "").strip()
        }
        for import_index, candidate in enumerate(candidates, start=1):
            source_key = (
                str(candidate.source or "").strip().casefold(),
                str(candidate.source_job_id or "").strip(),
            )
            if source_key[1] and source_key in pending_source_keys:
                duplicate_pending += 1
                continue
            existing = self.db.find_job_by_source_id(candidate.source, candidate.source_job_id)
            if existing is not None:
                known += 1
                continue

            # Company blacklist is authoritative and deliberately runs before
            # the more expensive fuzzy duplicate detection. Blacklisted jobs are
            # persisted as rejected so future searches still know they were seen.
            profile = self.db.get_company_profile(candidate.company)
            if profile["blacklisted"]:
                self._save_blacklisted_candidate(candidate)
                blacklisted_rejected += 1
                known += 1
                if not self.show_red_states_var.get():
                    hidden_blacklisted += 1
                continue

            duplicate_action, duplicate_matches = self._apply_duplicate_detection(candidate)
            if duplicate_action == "duplicate":
                known += 1
                continue
            self.pending_counter += 1
            iid = f"tmp{self.pending_counter}"
            self.pending_by_iid[iid] = candidate
            added_iids.append(iid)
            if duplicate_matches:
                self.pending_duplicate_matches_by_iid[iid] = duplicate_matches
            if source_key[1]:
                pending_source_keys.add(source_key)

            if direct_router is not None and direct_start is not None:
                display_job = DisplayJob(
                    iid=iid,
                    db_id=None,
                    candidate=candidate,
                    record=None,
                    status="new",
                    manual_score=None,
                    branch=str(profile["branch"] or ""),
                    notes="",
                )
                self._auto_select_nearest_location(display_job, direct_router, direct_start)

            new += 1
        if page is not None and normalized_params is not None:
            self.search_session_page = int(page)
            self.search_session_params = normalized_params
            self._update_search_button_label()
        self.refresh_jobs()
        extras = []
        if blacklisted_rejected:
            extras.append(f"{blacklisted_rejected} blacklisted rejected")
        if hidden_blacklisted:
            extras.append(f"{hidden_blacklisted} blacklisted hidden")
        if duplicate_pending:
            extras.append(f"{duplicate_pending} exact duplicate(s) skipped")
        extra = (", " + ", ".join(extras)) if extras else ""
        page_text = f" page {page}" if page is not None else ""
        self.status_label_var.set(f"Search{page_text} result: {new} unsaved new, {known} already known{extra}.")
        return added_iids

    def _manual_source_display_values(self) -> list[str]:
        values = []
        for source in self.manual_sources:
            clean = str(source).strip().rstrip("*")
            if clean:
                values.append(f"{clean}*")
        return values or ["Manual*"]

    @staticmethod
    def _source_storage_name(source: str) -> str:
        clean = str(source).strip() or "Manual"
        return clean if clean.endswith("*") else f"{clean}*"

    def _enabled_ai_model_configs(self) -> list[AiModelConfig]:
        return [config for config in self.ai_models if config.enabled]

    def _ai_model_config_by_name(self, name: str) -> AiModelConfig | None:
        for config in self._enabled_ai_model_configs():
            if config.name == name:
                return config
        return self._default_ai_model_config()

    def _make_manual_source_id(self, source: str, title: str, company: str, location: str, published: str = "") -> str:
        seed = "|".join([source.strip().lower(), title.strip().lower(), company.strip().lower(), location.strip().lower(), published.strip().lower()])
        digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"manual-{digest}"


    def _ensure_unique_source_job_id(self, source: str, source_job_id: str) -> str:
        base = str(source_job_id or "manual").strip() or "manual"
        existing_pending = {cand.source_job_id for cand in self.pending_by_iid.values() if cand.source == source}
        if self.db.find_job_by_source_id(source, base) is None and base not in existing_pending:
            return base
        idx = 2
        while True:
            candidate_id = f"{base}-{idx}"
            if self.db.find_job_by_source_id(source, candidate_id) is None and candidate_id not in existing_pending:
                return candidate_id
            idx += 1

    def _append_pending_candidate(self, candidate: JobCandidate, import_index: int | None = None) -> str:
        self.pending_counter += 1
        iid = f"tmp{self.pending_counter}"
        self.pending_by_iid[iid] = candidate
        if import_index is not None:
            self.pending_import_index_by_iid[iid] = int(import_index)
        return iid

    @staticmethod
    def _coerce_salary_k(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            number = float(str(value).replace(",", "."))
        except ValueError:
            return None
        if number > 1000:
            number = number / 1000.0
        return round(number, 1)

    def list_import_dialog(self) -> None:
        models = self._enabled_ai_model_configs()
        if not models:
            messagebox.showwarning("List import", "No enabled AI model is configured in Settings → AI.")
            return
        dialog = tk.Toplevel(self)
        dialog.title("List import")
        dialog.geometry("900x720")
        dialog.transient(self)

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(frame)
        top.pack(fill=tk.X, pady=(0, 8))
        source_var = tk.StringVar(value=(self._manual_source_display_values()[0] if self._manual_source_display_values() else "LN*"))
        ttk.Label(top, text="AI model:").pack(side=tk.LEFT)
        default_model = self._default_ai_model_config("list_parser") or models[0]
        model_var = tk.StringVar(value=default_model.name)
        model_combo = ttk.Combobox(top, textvariable=model_var, values=[m.name for m in models], state="readonly", width=32)
        model_combo.pack(side=tk.LEFT, padx=(4, 0))
        model_combo.bind("<<ComboboxSelected>>", lambda _event: self._remember_ai_model_choice(model_var.get(), "list_parser"))

        ttk.Label(frame, text="Paste copied job list below:").pack(anchor="w")
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        import_text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            font=("Segoe UI", 10),
        )
        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=import_text.yview)
        import_text.configure(yscrollcommand=scroll.set)
        import_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        dialog.after_idle(import_text.focus_set)

        status_var = tk.StringVar(value="Idle")
        ttk.Label(frame, textvariable=status_var).pack(fill=tk.X, pady=(0, 6))

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        import_button = ttk.Button(buttons, text="Import with AI")
        import_button.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Close", command=dialog.withdraw).pack(side=tk.RIGHT, padx=(0, 8))
        source_combo = ttk.Combobox(buttons, textvariable=source_var, values=self._manual_source_display_values(), state="readonly", width=20)
        source_combo.pack(side=tk.RIGHT, padx=(4, 8))
        ttk.Label(buttons, text="Source:").pack(side=tk.RIGHT)

        def start_import() -> None:
            raw_text = import_text.get("1.0", tk.END).strip()
            if not raw_text:
                messagebox.showwarning("List import", "Paste a job list first.", parent=dialog)
                return
            model_config = self._ai_model_config_by_name(model_var.get())
            if model_config is None:
                messagebox.showwarning("List import", "Selected AI model is not available.", parent=dialog)
                return
            source = self._source_storage_name(source_var.get().strip() or "Manual*")
            import_button.configure(state=tk.DISABLED)
            source_combo.configure(state=tk.DISABLED)
            model_combo.configure(state=tk.DISABLED)
            status_var.set("AI is parsing pasted list...")
            self.status_label_var.set("AI list import running...")
            task_id = self._start_background_ai_task("list_import", [], "List import")
            threading.Thread(target=self._list_import_worker, args=(dialog, raw_text, source, model_config, status_var, import_button, source_combo, model_combo, task_id), daemon=True).start()

        import_button.configure(command=start_import)

    def _list_import_worker(self, dialog, raw_text: str, source: str, model_config: AiModelConfig, status_var: tk.StringVar, import_button, source_combo, model_combo, task_id: int) -> None:
        try:
            result = self.ai_service.parse_jobs_from_list_text(raw_text, source, model_config, self.user_config)
            self.after(0, lambda result=result: self._finish_list_import(dialog, result, source, status_var, import_button, source_combo, model_combo, task_id))
        except Exception as exc:
            error_message = str(exc)
            self.after(0, lambda error_message=error_message: self._fail_list_import(error_message, status_var, import_button, source_combo, model_combo, task_id))

    def _finish_list_import(self, dialog, result: dict, source: str, status_var: tk.StringVar, import_button, source_combo, model_combo, task_id: int) -> None:
        items = result.get("items", []) if isinstance(result, dict) else []
        candidates: list[JobCandidate] = []
        skipped = 0
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            company = str(item.get("company") or "").strip()
            location = str(item.get("location") or "").strip()
            country = str(item.get("country") or "").strip()
            published_raw = str(item.get("published_date") or "").strip()
            published = self._normalize_relative_published_date(published_raw)
            if not title or not company:
                skipped += 1
                continue
            source_job_id = self._make_manual_source_id(source, title, company, location, published)
            # Do not silently skip duplicate-looking list entries here. The explicit
            # duplicate dialog below decides whether to add or skip.
            raw_payload = {
                "list_import": True,
                "source": source,
                "ai_model": getattr(self._ai_model_config_by_name(str(result.get("model") or "")), "name", ""),
                "parsed_item": item,
                "country": country,
                "published_raw": published_raw,
                "raw_excerpt": item.get("raw_excerpt", ""),
            }
            candidate = JobCandidate(
                source=source,
                source_job_id=source_job_id,
                title=title,
                company=company,
                location=location,
                country=country,
                url=str(item.get("url") or "").strip(),
                description=str(item.get("description") or "").strip(),
                published_date=published,
                raw_json=json.dumps(raw_payload, ensure_ascii=False, indent=2),
                min_salary_k=self._coerce_salary_k(item.get("min_salary_k")),
                max_salary_k=self._coerce_salary_k(item.get("max_salary_k")),
            )
            candidates.append(candidate)

        added = 0
        blacklisted_rejected = 0
        duplicate_skipped = 0
        for import_index, candidate in enumerate(candidates, start=1):
            if self.db.get_company_profile(candidate.company)["blacklisted"]:
                self._save_blacklisted_candidate(candidate)
                blacklisted_rejected += 1
                continue
            action, matches = self._apply_duplicate_detection(candidate)
            if action == "duplicate":
                duplicate_skipped += 1
                continue
            candidate.source_job_id = self._ensure_unique_source_job_id(candidate.source, candidate.source_job_id)
            iid = self._append_pending_candidate(candidate, import_index=import_index)
            if matches:
                self.pending_duplicate_matches_by_iid[iid] = matches
            added += 1
        self.refresh_jobs()
        chat_text = str(result.get("chat_text") or "").strip()
        status = f"Imported {added} unsaved jobs from list"
        extras = []
        if skipped:
            extras.append(f"{skipped} skipped")
        if blacklisted_rejected:
            extras.append(f"{blacklisted_rejected} blacklisted rejected")
        if duplicate_skipped:
            extras.append(f"{duplicate_skipped} duplicate-like skipped")
        if extras:
            status += " (" + ", ".join(extras) + ")"
        status_var.set(status)
        self.status_label_var.set(status + (f". {chat_text}" if chat_text else "."))
        import_button.configure(state=tk.NORMAL)
        source_combo.configure(state="readonly")
        model_combo.configure(state="readonly")
        self._finish_background_ai_task(task_id, True)
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except Exception:
            pass

    @staticmethod
    def _normalize_duplicate_text(value: str) -> str:
        value = str(value or "").strip().casefold()
        value = re.sub(r"\((?:m|w|d|f|x)(?:\s*[/|,-]\s*(?:m|w|d|f|x)){1,4}\)", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\b(?:m|w|d|f|x)(?:\s*[/|,-]\s*(?:m|w|d|f|x)){1,4}\b", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*\((?:hybrid|vor ort|remote|on[- ]?site|hq|office)\)\s*", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"[^\wäöüß+]+", " ", value, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _normalize_duplicate_company(cls, value: str) -> str:
        text = cls._normalize_duplicate_text(value)
        suffixes = {"gmbh", "ag", "se", "kg", "mbh", "llc", "ltd", "limited", "inc", "corp", "corporation", "plc", "co"}
        return " ".join(part for part in text.split() if part not in suffixes).strip()

    @classmethod
    def _normalize_duplicate_location(cls, value: str) -> str:
        text = cls._normalize_duplicate_text(value)
        replacements = {"munich": "münchen", "nuremberg": "nürnberg", "cologne": "köln"}
        for old, new_value in replacements.items():
            text = re.sub(rf"\b{re.escape(old)}\b", new_value, text)
        text = re.sub(r"\b(?:deutschland|germany|bayern|bavaria)\b", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _normalize_duplicate_description(cls, value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", str(value or ""))
        return cls._normalize_duplicate_text(text)

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()

    @staticmethod
    def _normalize_duplicate_url(value: str) -> str:
        try:
            parsed = urlparse(str(value or "").strip())
            host = parsed.netloc.casefold().removeprefix("www.")
            path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
            return f"{host}{path}"
        except Exception:
            return str(value or "").strip().casefold().rstrip("/")

    def _score_duplicate_pair(self, candidate: JobCandidate, record) -> dict | None:
        c_desc = self._normalize_duplicate_description(candidate.description)
        e_desc = self._normalize_duplicate_description(record.description)
        exact_description = bool(c_desc and e_desc and c_desc == e_desc)
        exact_url = bool(candidate.url and record.url and self._normalize_duplicate_url(candidate.url) == self._normalize_duplicate_url(record.url))
        c_company = self._normalize_duplicate_company(candidate.company)
        e_company = self._normalize_duplicate_company(record.company)
        company_sim = self._similarity(c_company, e_company)
        if not exact_description and not exact_url and company_sim < 0.82:
            return None
        c_title = self._normalize_duplicate_text(candidate.title)
        e_title = self._normalize_duplicate_text(record.title)
        title_sim = self._similarity(c_title, e_title)
        c_location = self._normalize_duplicate_location(candidate.location)
        e_location = self._normalize_duplicate_location(record.location)
        location_sim = self._similarity(c_location, e_location) if c_location and e_location else 0.0
        if title_sim < 0.55 and location_sim < 0.90:
            return None
        description_sim = 0.0
        if exact_description or exact_url:
            score = 100.0
        else:
            if c_desc and e_desc and (title_sim >= 0.70 or location_sim >= 0.80):
                description_sim = self._similarity(c_desc, e_desc)
            available = [(title_sim, 0.34), (company_sim, 0.16)]
            if c_desc and e_desc:
                available.append((description_sim, 0.45))
            if c_location and e_location:
                available.append((location_sim, 0.05))
            weight = sum(w for _v, w in available) or 1.0
            score = 100.0 * sum(v * w for v, w in available) / weight
            if c_title == e_title and c_company == e_company and c_location and c_location == e_location:
                score = max(score, 96.0)
        if score < 80.0:
            return None
        return {
            "id": record.id, "title": record.title, "company": record.company, "location": record.location,
            "country": record.country, "status": record.status, "published": record.published_date,
            "source": record.source, "description": record.description, "url": record.url,
            "score": round(score, 1), "title_similarity": title_sim, "company_similarity": company_sim,
            "location_similarity": location_sim, "description_similarity": description_sim,
        }

    def _find_duplicate_jobs_for_candidate(self, candidate: JobCandidate) -> list[dict]:
        matches = []
        try:
            records = self.db.list_jobs(hide_rejected=False)
        except Exception:
            records = []
        for record in records:
            match = self._score_duplicate_pair(candidate, record)
            if match:
                matches.append(match)
        matches.sort(key=lambda item: item.get("score", 0), reverse=True)
        return matches

    def _save_blacklisted_candidate(self, candidate: JobCandidate) -> int:
        """Persist a blacklisted-company job without running duplicate detection."""
        candidate.duplicate_score = 0.0
        candidate.duplicate_of_job_id = None
        job_id, _is_new, _changed = self.db.add_or_update_job(candidate)
        self.db.save_user_fields(
            job_id,
            self._rejected_by_me_status(),
            None,
            "",
            "",
            reject_reason="blacklisted company",
        )
        return job_id

    def _apply_duplicate_detection(self, candidate: JobCandidate) -> tuple[str, list[dict]]:
        matches = self._find_duplicate_jobs_for_candidate(candidate)
        if not matches:
            candidate.duplicate_score = 0.0
            candidate.duplicate_of_job_id = None
            return "new", []
        best = matches[0]
        candidate.duplicate_score = float(best["score"])
        candidate.duplicate_of_job_id = int(best["id"])
        if candidate.duplicate_score >= 100.0:
            job_id, _new, _changed = self.db.add_or_update_job(candidate)
            self.db.save_user_fields(job_id, self._rejected_by_me_status(), None, "", "", reject_reason="duplicate")
            self.db.set_duplicate_info(job_id, 100.0, int(best["id"]))
            for match in matches:
                self.db.save_duplicate_relation(job_id, int(match["id"]), float(match["score"]), "duplicate" if int(match["id"]) == int(best["id"]) else "candidate")
            return "duplicate", matches
        return "candidate", matches

    def _ask_duplicate_import_decision(self, candidate: JobCandidate, matches: list[dict], index: int, total: int, parent) -> str:
        dialog = tk.Toplevel(parent)
        dialog.title("Potential duplicate")
        dialog.transient(parent)
        dialog.grab_set()
        result = {"value": "cancel"}

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"Potential match {index} / {total}", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))

        best = matches[0]
        columns = ("field", "existing", "imported")
        compare = ttk.Treeview(frame, columns=columns, show="headings", height=7)
        compare.heading("field", text="Field")
        compare.heading("existing", text="Existing job")
        compare.heading("imported", text="Imported job")
        compare.column("field", width=110, stretch=False)
        compare.column("existing", width=300, stretch=True)
        compare.column("imported", width=300, stretch=True)
        rows = [
            ("Title", best.get("title") or "-", candidate.title or "-"),
            ("Company", best.get("company") or "-", candidate.company or "-"),
            ("Location", best.get("location") or "-", candidate.location or "-"),
            ("Published", best.get("published") or "-", candidate.published_date or "-"),
            ("Status", best.get("status") or "-", "new"),
            ("Source", best.get("source") or "-", candidate.source or "-"),
        ]
        for row in rows:
            compare.insert("", tk.END, values=row)
        compare.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        similarity = (
            f"Similarity — Title: {best.get('title_similarity', 0):.0%}, "
            f"Company: {best.get('company_similarity', 0):.0%}, "
            f"Location: {best.get('location_similarity', 0):.0%}"
        )
        ttk.Label(frame, text=similarity).pack(anchor="w", pady=(0, 8))
        if len(matches) > 1:
            ttk.Label(frame, text=f"{len(matches) - 1} additional possible match(es) found.").pack(anchor="w", pady=(0, 8))

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        def choose(value: str) -> None:
            result["value"] = value
            dialog.destroy()
        ttk.Button(buttons, text="Skip match", command=lambda: choose("skip")).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Add as new", command=lambda: choose("add")).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Button(buttons, text="Skip matches", command=lambda: choose("skip_all")).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Add all as new", command=lambda: choose("add_all")).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Button(buttons, text="Cancel import", command=lambda: choose("cancel")).pack(side=tk.RIGHT)
        self._center_child_window(dialog, width=820, height=420)
        self.wait_window(dialog)
        return str(result.get("value") or "cancel")

    @staticmethod
    def _normalize_relative_published_date(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        lower = text.lower()
        today = date.today()
        m = re.search(r"vor\s+(\d+)\s+(minute|minuten|stunde|stunden|tag|tagen|woche|wochen|monat|monaten)", lower)
        if not m:
            return text
        amount = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("minute") or unit.startswith("stunde"):
            return today.isoformat()
        if unit.startswith("tag"):
            return (today - timedelta(days=amount)).isoformat()
        if unit.startswith("woche"):
            return (today - timedelta(days=amount * 7)).isoformat()
        if unit.startswith("monat"):
            return (today - timedelta(days=amount * 30)).isoformat()
        return text

    @staticmethod
    def _coerce_ai_score(value) -> int | None:
        if value is None or value == "":
            return None
        try:
            number = float(str(value).strip().replace(",", "."))
        except ValueError:
            return None
        # Detail analysis explicitly uses a 0..100 scale. Do not silently
        # multiply values <= 10: that turned clearly unsuitable jobs with a
        # model score of 8 or 10 into green 80/100 or 100/100 results.
        number = round(number)
        if number < 0 or number > 100:
            return None
        return int(number)

    @staticmethod
    def _coerce_ai_screening_score(value) -> int | None:
        """Screening scores are intentionally 0..25: potential worth checking details."""
        if value is None or value == "":
            return None
        try:
            number = float(str(value).strip().replace(",", "."))
        except ValueError:
            return None
        number = round(number)
        if number < 0:
            number = 0
        if number > 25:
            number = 25
        return int(number)

    def _screening_decision_from_score(self, score: int | None) -> str | None:
        if score is None:
            return None
        if score >= 17:
            return "potential"
        if score >= 9:
            return "maybe"
        return "unlikely"

    def _apply_ai_detected_flags(self, iid: str, item: dict, job: DisplayJob | None) -> None:
        """Apply obvious AI-detected flags from table screening to visible/pending jobs.

        This is intentionally conservative: only explicit true values are applied. For unsaved
        jobs the raw_json/candidate fields are updated so the information is persisted when the
        job is later saved. For stored DB rows this only updates the current in-memory display.
        """
        if job is None or not isinstance(item, dict):
            return
        changed = False
        raw_data = self._load_raw_entry_for_job(job)
        if not isinstance(raw_data, dict):
            raw_data = {}

        if item.get("is_anue") is True or item.get("anue") is True or item.get("is_arbeitnehmerueberlassung") is True:
            if raw_data.get("istArbeitnehmerUeberlassung") is not True:
                raw_data["istArbeitnehmerUeberlassung"] = True
                changed = True

        if item.get("is_fixed_term") is True or item.get("fixed_term") is True or item.get("befristet") is True:
            if raw_data.get("vertragsdauer") != "BEFRISTET":
                raw_data["vertragsdauer"] = "BEFRISTET"
                changed = True
            if job.candidate is not None and (not job.candidate.fixed_term or job.candidate.fixed_term == "-"):
                job.candidate.fixed_term = "FT"
                changed = True
            elif job.record is not None and (not job.record.fixed_term or job.record.fixed_term == "-"):
                job.record.fixed_term = "FT"
                changed = True

        if changed:
            if job.candidate is not None:
                try:
                    job.candidate.raw_json = json.dumps(raw_data, ensure_ascii=False)
                except Exception:
                    pass
            elif job.db_id is not None:
                self.raw_entry_cache[job.db_id] = raw_data

    def _apply_ai_industry_suggestion(self, iid: str, item: dict, job: DisplayJob | None) -> None:
        if job is None or str(job.branch or "").strip():
            return
        allowed = {str(v).strip(): str(v).strip() for v in self.db.list_company_ratings() if str(v).strip()}
        suggested = str(item.get("industry") or item.get("suggested_industry") or "").strip()
        if suggested in allowed:
            self.temp_industries_by_iid[iid] = suggested
            job.branch = suggested
            self._update_dirty_indicators_for_iid(iid)

    def _fail_list_import(self, error_message: str, status_var: tk.StringVar, import_button, source_combo=None, model_combo=None, task_id: int | None = None) -> None:
        status_var.set(f"AI list import failed: {error_message}")
        self.status_label_var.set("AI list import failed.")
        try:
            import_button.configure(state=tk.NORMAL)
            if source_combo is not None:
                source_combo.configure(state="readonly")
            if model_combo is not None:
                model_combo.configure(state="readonly")
        except tk.TclError:
            pass
        if task_id is not None:
            self._finish_background_ai_task(task_id, False)


    def _score_for_edit_dialog(self, job: DisplayJob) -> int | None:
        current = self.temp_ai_evaluations_by_iid.get(job.iid) if hasattr(self, "temp_ai_evaluations_by_iid") else None
        if isinstance(current, dict) and isinstance(current.get("score"), int):
            return current.get("score")
        return job.manual_score

    def edit_selected_entry_dialog(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if len(jobs) != 1:
            self.status_label_var.set("Edit entry requires exactly one selected job.")
            return
        self.edit_entry_dialog(jobs[0])

    def edit_entry_dialog(self, job: DisplayJob) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Edit entry")
        dialog.geometry("780x820")
        dialog.transient(self)

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        fields: dict[str, tk.StringVar] = {
            "Title": tk.StringVar(value=job.title),
            "Company": tk.StringVar(value=job.company),
            "Location": tk.StringVar(value=job.location),
            "Country": tk.StringVar(value=self._country_display(job.country)),
            "URL": tk.StringVar(value=job.url),
            "Published": tk.StringVar(value=job.published_date),
            "Source": tk.StringVar(value=job.source if job.source.endswith("*") else f"{job.source}*"),
            "Score": tk.StringVar(value="" if self._score_for_edit_dialog(job) is None else str(self._score_for_edit_dialog(job))),
            "Min salary (k€)": tk.StringVar(value="" if job.min_salary_k is None else str(job.min_salary_k)),
            "Max salary (k€)": tk.StringVar(value="" if job.max_salary_k is None else str(job.max_salary_k)),
            "Fixed term / duration": tk.StringVar(value="" if not job.fixed_term or job.fixed_term == "-" else str(job.fixed_term)),
            "Industry": tk.StringVar(value=job.branch),
            "Status": tk.StringVar(value=job.status or "new"),
        }
        row = 0
        for label, var in fields.items():
            ttk.Label(frame, text=f"{label}:").grid(row=row, column=0, sticky="w", pady=3)
            if label == "Status":
                widget = ttk.Combobox(frame, textvariable=var, values=self.status_values, state="readonly", width=32)
            elif label == "Industry":
                widget = ttk.Combobox(frame, textvariable=var, values=self.db.list_company_ratings(), width=32)
            elif label == "Source":
                widget = ttk.Combobox(frame, textvariable=var, values=self._manual_source_display_values(), state="readonly", width=32)
                if job.db_id is not None:
                    widget.configure(state="disabled")
            else:
                widget = ttk.Entry(frame, textvariable=var, width=58)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            row += 1

        raw_data = self._load_raw_entry_for_job(job)
        anue_var = tk.BooleanVar(value=bool(raw_data.get("istArbeitnehmerUeberlassung")))
        pav_var = tk.BooleanVar(value=bool(raw_data.get("istPrivateArbeitsvermittlung")))
        mini_var = tk.BooleanVar(value=bool(raw_data.get("istGeringfuegigeBeschaeftigung")))
        dis_var = tk.BooleanVar(value=bool(raw_data.get("istBehinderungGefordert")))
        exact_location_var = tk.StringVar()
        if isinstance(raw_data, dict):
            exact_location_var.set(str(raw_data.get("exact_location") or raw_data.get("exact_address") or ""))
        locs = raw_data.get("stellenlokationen") if isinstance(raw_data, dict) else None
        if not exact_location_var.get().strip() and isinstance(locs, list) and locs:
            try:
                selected = locs[int(job.selected_location_index or 0)] if int(job.selected_location_index or 0) < len(locs) else locs[0]
                lat = selected.get("breite")
                lon = selected.get("laenge")
                if lat is not None and lon is not None:
                    exact_location_var.set(f"{lon}, {lat}")
            except Exception:
                pass
        feature_frame = ttk.LabelFrame(frame, text="Flags / exact location", padding=6)
        feature_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        flags_row = ttk.Frame(feature_frame)
        flags_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Checkbutton(flags_row, text="ANÜ", variable=anue_var).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(flags_row, text="Private Vermittlung", variable=pav_var).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(flags_row, text="Mini job", variable=mini_var).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(flags_row, text="Behinderung gefordert", variable=dis_var).pack(side=tk.LEFT, padx=(0, 14))
        exact_row = ttk.Frame(feature_frame)
        exact_row.pack(fill=tk.X)
        ttk.Label(exact_row, text="Exact location (exact comma-separated address or geo-coordinates: lon, lat):").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(exact_row, textvariable=exact_location_var, width=46).pack(side=tk.LEFT, fill=tk.X, expand=True)
        row += 1

        if job.record is not None and (job.record.applied_at or job.record.application_expected_salary or job.record.application_available_from or job.record.application_via or job.record.application_info or job.record.application_notes):
            app_frame = ttk.LabelFrame(frame, text="Application (read-only)", padding=6)
            app_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6))
            application_rows = [
                ("Applied", job.record.applied_at),
                ("Expected salary", job.record.application_expected_salary),
                ("Available from", job.record.application_available_from),
                ("Applied via", job.record.application_via),
                ("Additional info / URL", job.record.application_info),
                ("Application notes", job.record.application_notes),
            ]
            for app_row, (label, value) in enumerate(application_rows):
                ttk.Label(app_frame, text=f"{label}:").grid(row=app_row, column=0, sticky="nw", padx=(0, 8), pady=2)
                ttk.Label(app_frame, text=str(value or "-"), wraplength=500, justify=tk.LEFT).grid(row=app_row, column=1, sticky="w", pady=2)
            app_frame.columnconfigure(1, weight=1)
            row += 1

        ttk.Label(frame, text="Description:").grid(row=row, column=0, sticky="nw", pady=3)
        desc_frame = ttk.Frame(frame)
        desc_frame.grid(row=row, column=1, sticky="nsew", pady=3)
        description = tk.Text(desc_frame, height=10, wrap=tk.WORD, background=DARK_THEME["entry_bg"], foreground=DARK_THEME["entry_fg"], insertbackground=DARK_THEME["fg"], font=("Segoe UI", 10))
        desc_scroll = ttk.Scrollbar(desc_frame, orient=tk.VERTICAL, command=description.yview)
        description.configure(yscrollcommand=desc_scroll.set)
        description.insert("1.0", job.description or "")
        description.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        desc_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        row += 1

        ttk.Label(frame, text="Notes:").grid(row=row, column=0, sticky="nw", pady=3)
        notes_frame = ttk.Frame(frame)
        notes_frame.grid(row=row, column=1, sticky="nsew", pady=3)
        notes = tk.Text(notes_frame, height=5, wrap=tk.WORD, background=DARK_THEME["entry_bg"], foreground=DARK_THEME["entry_fg"], insertbackground=DARK_THEME["fg"], font=("Segoe UI", 10))
        notes_scroll = ttk.Scrollbar(notes_frame, orient=tk.VERTICAL, command=notes.yview)
        notes.configure(yscrollcommand=notes_scroll.set)
        notes.insert("1.0", job.notes or "")
        notes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        notes_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        row += 1
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(row - 2, weight=1)

        def save_edit() -> None:
            title = fields["Title"].get().strip()
            company = fields["Company"].get().strip()
            location = fields["Location"].get().strip()
            country = fields["Country"].get().strip()
            if not title or not company:
                messagebox.showwarning("Missing input", "Title and company are required.", parent=dialog)
                return
            raw_score = fields["Score"].get().strip()
            score = None
            if raw_score:
                try:
                    score = int(raw_score)
                except ValueError:
                    messagebox.showwarning("Invalid score", "Score must be empty or an integer.", parent=dialog)
                    return
            raw_min_salary = fields["Min salary (k€)"].get().strip()
            raw_max_salary = fields["Max salary (k€)"].get().strip()
            min_salary_k = self._coerce_salary_k(raw_min_salary)
            max_salary_k = self._coerce_salary_k(raw_max_salary)
            if raw_min_salary and min_salary_k is None:
                messagebox.showwarning("Invalid salary", "Minimum salary must be empty or a number in k€.", parent=dialog)
                return
            if raw_max_salary and max_salary_k is None:
                messagebox.showwarning("Invalid salary", "Maximum salary must be empty or a number in k€.", parent=dialog)
                return
            if min_salary_k is not None and max_salary_k is not None and min_salary_k > max_salary_k:
                messagebox.showwarning("Invalid salary", "Minimum salary must not exceed maximum salary.", parent=dialog)
                return
            fixed_term_text = fields["Fixed term / duration"].get().strip() or "-"
            exact_location_text = exact_location_var.get().strip()

            def _build_raw_payload(source_value: str, source_job_id_value: str | None = None) -> dict:
                payload = {
                    "manual_edit": True,
                    "source": source_value,
                    "title": title,
                    "company": company,
                    "location": location,
                    "country": country,
                    "url": fields["URL"].get().strip(),
                    "description": description.get("1.0", tk.END).strip(),
                    "published_date": fields["Published"].get().strip(),
                    "score": score,
                    "min_salary_k": min_salary_k,
                    "max_salary_k": max_salary_k,
                    "fixed_term": fixed_term_text,
                    "vertragsdauer": "BEFRISTET" if fixed_term_text != "-" else "UNBEFRISTET",
                    "manual_fixed_term": fixed_term_text,
                    "industry": fields["Industry"].get().strip(),
                    "status": fields["Status"].get().strip() or job.status or "new",
                    "notes": notes.get("1.0", tk.END).strip(),
                    "exact_location": exact_location_text,
                    "istArbeitnehmerUeberlassung": anue_var.get(),
                    "istPrivateArbeitsvermittlung": pav_var.get(),
                    "istGeringfuegigeBeschaeftigung": mini_var.get(),
                    "istBehinderungGefordert": dis_var.get(),
                }
                if source_job_id_value:
                    payload["source_job_id"] = source_job_id_value
                parsed = self._parse_exact_location_input(exact_location_text)
                if parsed and parsed[0] == "coords":
                    payload["stellenlokationen"] = [{"adresse": {"ort": location}, "breite": parsed[1], "laenge": parsed[2]}]
                elif exact_location_text:
                    payload["exact_address"] = exact_location_text
                return payload

            if job.db_id is None and job.candidate is not None:
                source = self._source_storage_name(fields["Source"].get().strip() or job.source or "Manual*")
                job.candidate.source = source
                job.candidate.title = title
                job.candidate.company = company
                job.candidate.location = location
                job.candidate.country = country
                job.candidate.url = fields["URL"].get().strip()
                job.candidate.description = description.get("1.0", tk.END).strip()
                job.candidate.published_date = fields["Published"].get().strip()
                job.candidate.min_salary_k = min_salary_k
                job.candidate.max_salary_k = max_salary_k
                job.candidate.fixed_term = fixed_term_text
                job.candidate.source_job_id = self._make_manual_source_id(source, title, company, location, job.candidate.published_date)
                raw_payload = _build_raw_payload(source)
                job.candidate.raw_json = json.dumps(raw_payload, ensure_ascii=False, indent=2)
                # Preserve temporary user fields on the DisplayJob instance.
                job.status = fields["Status"].get().strip() or "new"
                job.manual_score = score
                job.branch = fields["Industry"].get().strip()
                job.notes = notes.get("1.0", tk.END).strip()
            elif job.db_id is not None:
                source = job.source
                source_job_id = job.source_job_id or self._make_manual_source_id(source, title, company, location, fields["Published"].get().strip())
                candidate = JobCandidate(
                    source=source,
                    source_job_id=source_job_id,
                    title=title,
                    company=company,
                    location=location,
                    country=country,
                    url=fields["URL"].get().strip(),
                    description=description.get("1.0", tk.END).strip(),
                    published_date=fields["Published"].get().strip(),
                    raw_json=json.dumps(_build_raw_payload(source, source_job_id), ensure_ascii=False, indent=2),
                    min_salary_k=min_salary_k,
                    max_salary_k=max_salary_k,
                    fixed_term=fixed_term_text,
                )
                location_changed = (location != job.location) or (country.casefold() != self._country_display(job.country).casefold())
                self.db.add_or_update_job(candidate)
                # Keep the raw-entry cache in sync with the newly persisted version.
                # refresh_jobs() intentionally preserves this cache for performance, so
                # leaving the old value here made flags such as ANÜ appear unsaved.
                try:
                    self.raw_entry_cache[job.db_id] = json.loads(candidate.raw_json)
                except Exception:
                    self.raw_entry_cache.pop(job.db_id, None)
                self.db.save_job_country(job.db_id, country)
                if location_changed:
                    self.db.clear_job_route(job.db_id)
                self.db.save_user_fields(job.db_id, fields["Status"].get().strip() or job.status, score, fields["Industry"].get().strip(), notes.get("1.0", tk.END).strip())
            dialog.destroy()
            self.refresh_jobs()

        buttonbar = ttk.Frame(frame)
        buttonbar.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttonbar, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttonbar, text="Save", command=save_edit).pack(side=tk.RIGHT, padx=(0, 8))

    def add_entry_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Add entry")
        dialog.geometry("700x700")
        dialog.transient(self)

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        fields: dict[str, tk.StringVar] = {
            "Title": tk.StringVar(),
            "Company": tk.StringVar(),
            "Location": tk.StringVar(),
            "Country": tk.StringVar(value="Germany"),
            "URL": tk.StringVar(),
            "Published": tk.StringVar(),
            "Source": tk.StringVar(value=(self._manual_source_display_values()[0] if self._manual_source_display_values() else "Manual*")),
            "Score": tk.StringVar(),
            "Fixed term / duration": tk.StringVar(),
            "Industry": tk.StringVar(),
            "Status": tk.StringVar(value="new"),
        }

        row = 0
        for label, var in fields.items():
            ttk.Label(frame, text=f"{label}:").grid(row=row, column=0, sticky="w", pady=3)
            if label == "Status":
                widget = ttk.Combobox(frame, textvariable=var, values=self.status_values, state="readonly", width=30)
            elif label == "Industry":
                widget = ttk.Combobox(frame, textvariable=var, values=self.db.list_company_ratings(), width=30)
            elif label == "Source":
                widget = ttk.Combobox(frame, textvariable=var, values=self._manual_source_display_values(), state="readonly", width=30)
            else:
                widget = ttk.Entry(frame, textvariable=var, width=55)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            row += 1

        anue_var = tk.BooleanVar(value=False)
        flags_frame = ttk.LabelFrame(frame, text="Flags", padding=6)
        flags_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        ttk.Checkbutton(flags_frame, text="ANÜ", variable=anue_var).pack(side=tk.LEFT)
        row += 1

        ttk.Label(frame, text="Description:").grid(row=row, column=0, sticky="nw", pady=3)
        description_frame = ttk.Frame(frame)
        description_frame.grid(row=row, column=1, sticky="nsew", pady=3)
        description = tk.Text(description_frame, height=9, wrap=tk.WORD)
        description_scroll = ttk.Scrollbar(description_frame, orient=tk.VERTICAL, command=description.yview)
        description.configure(yscrollcommand=description_scroll.set)
        description.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        description_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        row += 1

        ttk.Label(frame, text="Notes:").grid(row=row, column=0, sticky="nw", pady=3)
        notes_frame = ttk.Frame(frame)
        notes_frame.grid(row=row, column=1, sticky="nsew", pady=3)
        notes = tk.Text(notes_frame, height=5, wrap=tk.WORD)
        notes_scroll = ttk.Scrollbar(notes_frame, orient=tk.VERTICAL, command=notes.yview)
        notes.configure(yscrollcommand=notes_scroll.set)
        notes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        notes_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        row += 1

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(row - 2, weight=1)

        def do_add() -> None:
            title = fields["Title"].get().strip()
            company = fields["Company"].get().strip()
            location = fields["Location"].get().strip()
            country = fields["Country"].get().strip()
            if not title or not company:
                messagebox.showwarning("Missing input", "Title and company are required.", parent=dialog)
                return
            fixed_term_text = fields["Fixed term / duration"].get().strip() or "-"
            stable_id = f"{title.lower()}|{company.lower()}|{location.lower()}"
            candidate = JobCandidate(
                source=self._source_storage_name(fields["Source"].get().strip() or "Manual*"),
                source_job_id=stable_id,
                title=title,
                company=company,
                location=location,
                country=country,
                url=fields["URL"].get().strip(),
                description=description.get("1.0", tk.END).strip(),
                published_date=fields["Published"].get().strip(),
                fixed_term=fixed_term_text,
                raw_json=json.dumps({
                    "manual_entry": True,
                    "source": self._source_storage_name(fields["Source"].get().strip() or "Manual*"),
                    "title": title,
                    "company": company,
                    "location": location,
                    "country": country,
                    "url": fields["URL"].get().strip(),
                    "description": description.get("1.0", tk.END).strip(),
                    "istArbeitnehmerUeberlassung": anue_var.get(),
                    "fixed_term": fixed_term_text,
                    "vertragsdauer": "BEFRISTET" if fixed_term_text != "-" else "UNBEFRISTET",
                    "manual_fixed_term": fixed_term_text,
                }, ensure_ascii=False),
            )
            job_id, _is_new, _changed = self.db.add_or_update_job(candidate)
            raw_score = fields["Score"].get().strip()
            score = None
            if raw_score:
                try:
                    score = int(raw_score)
                except ValueError:
                    messagebox.showwarning("Invalid score", "Score must be empty or an integer.", parent=dialog)
                    return
            self.db.save_user_fields(
                job_id=job_id,
                status=fields["Status"].get(),
                score=score,
                company_rating=fields["Industry"].get().strip(),
                notes=notes.get("1.0", tk.END).strip(),
            )
            dialog.destroy()
            self.refresh_jobs()

        buttonbar = ttk.Frame(frame)
        buttonbar.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttonbar, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttonbar, text="Add", command=do_add).pack(side=tk.RIGHT, padx=(0, 8))

    # ------------------------------------------------------------------
    # Display / filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _set_widget_redraw(widget: tk.Widget, enabled: bool) -> None:
        """Temporarily suppress native Windows redraw during large Treeview rebuilds."""
        if os.name != "nt":
            return
        try:
            import ctypes
            ctypes.windll.user32.SendMessageW(int(widget.winfo_id()), 0x000B, int(bool(enabled)), 0)
            if enabled:
                widget.update_idletasks()
                ctypes.windll.user32.RedrawWindow(int(widget.winfo_id()), None, None, 0x0085)
        except Exception:
            pass

    def refresh_jobs(self) -> None:
        previous_order = list(self.tree.get_children("")) if hasattr(self, "tree") else []
        selected_ids = list(self.tree.selection()) if hasattr(self, "tree") else []

        stored_records = self.db.list_jobs(
            status_filter=self.status_filter_var.get(),
            text_filter="",
            hide_rejected=False,
            show_only_new=self.show_only_new_var.get(),
        )
        display_jobs: list[DisplayJob] = [
            DisplayJob(
                iid=str(job.id),
                db_id=job.id,
                candidate=None,
                record=job,
                status=job.status,
                manual_score=job.manual_score,
                branch=self.temp_industries_by_iid.get(str(job.id), job.company_rating or ""),
                notes=job.notes or "",
            )
            for job in stored_records
        ]
        self._prefetch_raw_entries(display_jobs)
        display_jobs = [job for job in display_jobs if self._passes_extra_visibility_filters(job)]
        display_jobs = [job for job in display_jobs if self._passes_text_filters(job)]
        display_jobs.extend(self._filtered_pending_display_jobs())
        self.current_jobs = self._preserve_or_apply_user_sort(display_jobs, previous_order)
        self._populate_known_routes_from_cache()

        self._set_widget_redraw(self.tree, False)
        try:
            self.tree.delete(*self.tree.get_children())
            for job in self.current_jobs:
                self.tree.insert(
                    "",
                    tk.END,
                    iid=job.iid,
                    values=(
                        self._display_id(job),
                        job.status,
                        self._ai_status_symbol(job),
                        self._date_only_display(job.status_changed_at),
                        self._display_score(job),
                        job.branch,
                        self._salary_display(job.min_salary_k),
                        self._salary_display(job.max_salary_k),
                        job.fixed_term or "-",
                        self._published_display(job.published_date),
                        job.source,
                        self._display_title(job),
                        job.company,
                        self._distance_display(job.route_distance_km, job.route_quality),
                        self._duration_display(job.route_duration_min, job.route_quality),
                        self._display_location(job),
                        self._country_display(job.country),
                    ),
                    tags=self._row_tags(job),
                )
        finally:
            self._set_widget_redraw(self.tree, True)

        existing_selected = [item for item in selected_ids if self.tree.exists(item)]
        if existing_selected:
            self.tree.selection_set(existing_selected)
            self.tree.see(existing_selected[0])
            self._last_tree_selection = set()
            self._refresh_tree_selection_tags()
        else:
            self.selected_iid = None
            self._clear_detail_fields()

        self._update_save_selected_button_state()
        self.status_label_var.set(f"Showing {len(self.current_jobs)} jobs ({len(self.pending_by_iid)} unsaved search results)")
        self._update_filter_frame_title(len(self.current_jobs))
        self._update_table_count_label()

    def _update_table_count_label(self) -> None:
        if not hasattr(self, "table_count_var") or not hasattr(self, "tree"):
            return
        self.table_count_var.set(f"Visible: {len(self.tree.get_children(''))} | Selected: {len(self.tree.selection())}")

    def _filters_are_active(self) -> bool:
        if getattr(self, "status_filter_var", None) is not None and self.status_filter_var.get() != "all":
            return True
        if getattr(self, "show_only_new_var", None) is not None and self.show_only_new_var.get():
            return True
        if getattr(self, "hide_anue_var", None) is not None and self.hide_anue_var.get():
            return True
        if getattr(self, "hide_fixed_term_var", None) is not None and self.hide_fixed_term_var.get():
            return True
        if getattr(self, "include_filter_var", None) is not None and self.include_filter_var.get().strip():
            return True
        if getattr(self, "exclude_filter_var", None) is not None and self.exclude_filter_var.get().strip():
            return True
        expected = {"red": False, "green": True, "blue": True, "uncolored": True}
        for key, default in expected.items():
            variable = getattr(self, f"show_{key}_states_var", None)
            if variable is not None and bool(variable.get()) != default:
                return True
        return False

    def _update_filter_frame_title(self, shown_count: int) -> None:
        frame = getattr(self, "filterbar", None)
        if frame is None:
            return
        if not self._filters_are_active():
            frame.configure(text="Filters")
            return
        try:
            total_count = len(self.db.list_jobs(status_filter="all", text_filter="", hide_rejected=False, show_only_new=False)) + len(self.pending_by_iid)
        except Exception:
            total_count = shown_count
        frame.configure(text=f"Filters ({shown_count} of {total_count} shown)")

    def _filtered_pending_display_jobs(self) -> list[DisplayJob]:
        result: list[DisplayJob] = []
        status_filter = self.status_filter_var.get()
        show_only_new = self.show_only_new_var.get()
        for iid, candidate in self.pending_by_iid.items():
            profile = self.db.get_company_profile(candidate.company)
            status = self._rejected_by_me_status() if profile["blacklisted"] else "new"
            branch = self.temp_industries_by_iid.get(iid, str(profile["branch"] or ""))
            if status_filter != "all" and status != status_filter:
                continue
            if show_only_new and status != "new":
                continue
            temp_job = DisplayJob(iid=iid, db_id=None, candidate=candidate, record=None, status=status, manual_score=None, branch=branch, notes="")
            if not self._passes_extra_visibility_filters(temp_job):
                continue
            if not self._passes_text_filters(temp_job):
                continue
            route = self.temp_routes_by_iid.get(iid)
            if route is not None:
                candidate.route_distance_km, candidate.route_duration_min, candidate.route_address_text, candidate.route_quality = route
            result.append(temp_job)
        return result

    @staticmethod
    def _split_filter_terms(text: str) -> list[str]:
        raw = text.replace(",", " ").split()
        return [term.strip().lower() for term in raw if term.strip()]

    def _job_filter_haystack(self, job: DisplayJob) -> str:
        values = [
            job.title,
            self._display_title(job),
            job.company,
            self._display_location(job),
            job.branch,
            job.status,
            job.source,
            job.fixed_term,
        ]
        if getattr(self, "filter_search_texts_var", None) is not None and self.filter_search_texts_var.get():
            values.extend([job.description, job.formatted_description, job.notes])
            evaluation = self.temp_ai_evaluations_by_iid.get(job.iid, {})
            if not evaluation and job.db_id is not None:
                evaluation = self.db.get_latest_ai_evaluation(job.db_id) or {}
            if isinstance(evaluation, dict):
                values.extend([evaluation.get("summary", ""), evaluation.get("rating", "")])
        return "\n".join(str(value or "") for value in values).lower()

    def _passes_text_filters(self, job: DisplayJob) -> bool:
        include_text = self.include_filter_var.get() if hasattr(self, "include_filter_var") else ""
        exclude_text = self.exclude_filter_var.get() if hasattr(self, "exclude_filter_var") else ""
        include_terms = self._split_filter_terms(include_text)
        exclude_terms = self._split_filter_terms(exclude_text)
        if not include_terms and not exclude_terms:
            return True
        haystack = self._job_filter_haystack(job)
        if include_terms and not all(term in haystack for term in include_terms):
            return False
        if exclude_terms and any(term in haystack for term in exclude_terms):
            return False
        return True

    def _passes_extra_visibility_filters(self, job: DisplayJob) -> bool:
        """Apply GUI-only filters that are not part of the database query."""
        if getattr(self, "hide_anue_var", None) is not None and self.hide_anue_var.get():
            if self._job_is_anue(job):
                return False

        if getattr(self, "hide_fixed_term_var", None) is not None and self.hide_fixed_term_var.get():
            if job.fixed_term and job.fixed_term != "-":
                return False

        group_visibility_vars = {
            "red": getattr(self, "show_red_states_var", None),
            "green": getattr(self, "show_green_states_var", None),
            "blue": getattr(self, "show_blue_states_var", None),
            "uncolored": getattr(self, "show_uncolored_states_var", None),
        }
        category = self._status_category(job.status)
        visibility_var = group_visibility_vars.get(category)
        if visibility_var is not None and not visibility_var.get():
            return False

        return True

    def _job_is_anue(self, job: DisplayJob) -> bool:
        data = self._load_raw_entry_for_job(job)
        return isinstance(data, dict) and data.get("istArbeitnehmerUeberlassung") is True

    def _preserve_or_apply_user_sort(self, jobs: list[DisplayJob], previous_order: list[str]) -> list[DisplayJob]:
        if self.current_sort_column is not None:
            return self._sort_display_jobs(jobs, self.current_sort_column, self.current_sort_reverse)
        if not previous_order:
            return jobs
        old_index = {iid: index for index, iid in enumerate(previous_order)}
        return sorted(jobs, key=lambda job: old_index.get(job.iid, len(old_index) + (job.db_id or 999999)))

    def _get_display_job(self, iid: str | None) -> DisplayJob | None:
        if iid is None:
            return None
        for job in self.current_jobs:
            if job.iid == iid:
                return job
        return None

    def _clear_detail_fields(self) -> None:
        self._set_text_content(self.description_text, "")
        self._set_text_content(self.ai_summary_text, "No AI evaluation available yet.")
        self._set_text_content(self.notes_text, "", readonly=False)
        self._set_text_content(self.ai_rating_text, "No AI evaluation available yet.")
        self._set_ai_tabs_available(False)
        self.edit_status_var.set("")
        self.score_var.set("")
        self.company_rating_var.set("")
        self._update_detail_tab_labels()

    def on_select_job(self, _event: object) -> None:
        previous_selected_iid = self.selected_iid
        selection = self.tree.selection()
        if not selection:
            self._refresh_tree_selection_tags()
            self.selected_iid = None
            self._update_save_selected_button_state()
            self._clear_detail_fields()
            return
        self.selected_iid = selection[0]
        preserve_text_views = previous_selected_iid == self.selected_iid
        self._update_save_selected_button_state()
        if len(selection) > 1:
            self._refresh_tree_selection_tags()
            self._load_multi_selection(len(selection))
            return

        job = self._get_display_job(self.selected_iid)
        if job is None:
            return
        previous_tabs = self._remember_detail_tab_selection()
        has_ai_details = False
        self._is_loading_selection = True
        try:
            self.status_combo.configure(state="readonly")
            self.score_entry.configure(state="normal")
            self.company_rating_combo.configure(state="normal")
            self.notes_text.configure(state="normal")
            self.edit_status_var.set(job.status)
            self.score_var.set("" if job.manual_score is None else str(job.manual_score))
            self.company_rating_var.set(job.branch or "")
            description = self.temp_formatted_descriptions_by_iid.get(job.iid) or job.formatted_description or job.description or "No description available."
            self._set_markdownish_content(self.description_text, description, preserve_view=preserve_text_views)
            self._set_text_content(self.notes_text, job.notes or "", readonly=False, preserve_view=preserve_text_views)
            has_ai_details = self._load_ai_details_for_job(job, preserve_view=preserve_text_views)
            self._highlight_active_include_terms()
            try:
                self.after_idle(lambda: self.notes_text.edit_modified(False))
            except Exception:
                pass
        finally:
            self._is_loading_selection = False
        self._restore_detail_tab_selection(previous_tabs)
        # When the ordinary text tab has nothing useful to show, prefer the
        # available AI tab for this particular job.
        if has_ai_details and not str(job.formatted_description or job.description or "").strip():
            try:
                self.description_notebook.select(1)
            except Exception:
                pass
        if has_ai_details and not str(job.notes or "").strip():
            try:
                self.notes_notebook.select(1)
            except Exception:
                pass
        self._refresh_tree_selection_tags()
        self._update_attention_buttons()

    def _load_multi_selection(self, count: int) -> None:
        self.status_combo.configure(state="readonly")
        self.score_var.set("")
        self.company_rating_var.set("")
        self.score_entry.configure(state="disabled")
        self.company_rating_combo.configure(state="disabled")
        self.notes_text.configure(state="disabled")
        self._set_text_content(self.description_text, f"{count} jobs selected. Use context menu actions, or Delete to reject/remove.")
        self._set_text_content(self.notes_text, "Score, industry and notes are preserved individually for multi-select.")
        self._set_text_content(self.ai_summary_text, "No AI evaluation shown for multi-select.")
        self._set_text_content(self.ai_rating_text, "No AI evaluation shown for multi-select.")
        self._set_ai_tabs_available(False)
        self._update_detail_tab_labels()

    # ------------------------------------------------------------------
    # Save / reject
    # ------------------------------------------------------------------

    def _save_pending_job(self, iid: str, status: str, score: int | None = None, branch: str = "", notes: str = "", reject_reason: str = "") -> int | None:
        candidate = self.pending_by_iid.get(iid)
        if candidate is None:
            return None
        if iid in self.temp_formatted_descriptions_by_iid:
            candidate.formatted_description = self.temp_formatted_descriptions_by_iid.get(iid, "")
        job_id, _is_new, _changed = self.db.add_or_update_job(candidate)
        self.db.save_user_fields(job_id=job_id, status=status, score=score, company_rating=branch, notes=notes, reject_reason=reject_reason)
        for match in self.pending_duplicate_matches_by_iid.pop(iid, []):
            self.db.save_duplicate_relation(job_id, int(match["id"]), float(match["score"]), "candidate")
        if getattr(candidate, "duplicate_score", 0.0):
            self.db.set_duplicate_info(job_id, float(candidate.duplicate_score), getattr(candidate, "duplicate_of_job_id", None))
        self._persist_temp_ai_evaluation(iid, job_id, status_override=status)
        self.temp_industries_by_iid.pop(iid, None)
        self.temp_formatted_descriptions_by_iid.pop(iid, None)
        self.notes_dirty_iids.discard(iid)
        self.pending_by_iid.pop(iid, None)
        self.pending_duplicate_matches_by_iid.pop(iid, None)
        return job_id

    def _persist_temp_ai_evaluation(self, iid: str, job_id: int, status_override: str | None = None) -> bool:
        evaluation = self.temp_ai_evaluations_by_iid.pop(iid, None)
        if not evaluation:
            return False
        score = evaluation.get("score") if isinstance(evaluation.get("score"), int) else None
        self.db.save_ai_evaluation(
            job_id=job_id,
            ai_name=str(evaluation.get("ai_name") or "AI"),
            provider=str(evaluation.get("provider") or ""),
            model=str(evaluation.get("model") or ""),
            summary=str(evaluation.get("summary") or ""),
            rating=str(evaluation.get("rating") or ""),
            score=score,
            decision=str(evaluation.get("decision") or ""),
            raw_response=str(evaluation.get("raw_response") or ""),
            reject_reason=str(evaluation.get("reject_reason") or ""),
            rejection_tags=[str(tag).strip() for tag in evaluation.get("rejection_tags", []) if str(tag).strip()] if isinstance(evaluation.get("rejection_tags"), list) else [],
        )
        if score is not None:
            job = self._get_display_job(iid)
            if job is not None:
                try:
                    self.db.save_user_fields(job_id=job_id, status=(status_override or job.status), score=score, company_rating=job.branch, notes=job.notes)
                except Exception:
                    pass
        self.notes_dirty_iids.discard(iid)
        self._update_dirty_indicators_for_iid(iid)
        return True

    def _update_attention_buttons(self) -> None:
        filters_active = bool(self.include_filter_var.get().strip() or self.exclude_filter_var.get().strip()) if hasattr(self, "include_filter_var") else False
        if hasattr(self, "clear_filter_button"):
            self.clear_filter_button.configure(style="Attention.TButton" if filters_active else "TButton")
        selection = list(self.tree.selection()) if hasattr(self, "tree") else []
        saveable_selected = any(
            (self._get_display_job(iid) is not None)
            and (self._get_display_job(iid).db_id is None or self._is_iid_dirty(iid))
            for iid in selection
        )
        if hasattr(self, "save_selected_button"):
            self.save_selected_button.configure(
                state=tk.NORMAL if saveable_selected else tk.DISABLED,
                style="Attention.TButton" if saveable_selected else "TButton",
            )
        selected_dirty = bool(self.selected_iid and self.selected_iid in self.notes_dirty_iids)
        selected_job = self._get_display_job(self.selected_iid) if self.selected_iid else None
        can_save_notes = bool(selected_dirty and selected_job is not None and selected_job.db_id is not None)
        if hasattr(self, "save_notes_button"):
            self.save_notes_button.configure(
                state=tk.NORMAL if can_save_notes else tk.DISABLED,
                style="Attention.TButton" if can_save_notes else "TButton",
            )

    def _update_save_selected_button_state(self) -> None:
        self._update_attention_buttons()

    def save_selected_in_database(self) -> None:
        selection = list(self.tree.selection())
        if not selection:
            return
        saved_count = 0
        updated_count = 0
        ai_saved_count = 0
        for iid in selection:
            job = self._get_display_job(iid)
            if job is None:
                continue
            if job.db_id is None:
                had_temp_ai = iid in self.temp_ai_evaluations_by_iid
                job_id = self._save_pending_job(iid, job.status or "new", branch=job.branch, notes=job.notes)
                if job_id is not None:
                    saved_count += 1
                    if had_temp_ai:
                        ai_saved_count += 1
            else:
                had_existing_changes = False
                if iid in self.temp_industries_by_iid:
                    job.branch = self.temp_industries_by_iid.pop(iid, job.branch)
                    self.db.save_user_fields(job_id=job.db_id, status=job.status, score=job.manual_score, company_rating=job.branch, notes=job.notes)
                    had_existing_changes = True
                if iid in self.temp_formatted_descriptions_by_iid:
                    self.db.save_job_formatted_description(job.db_id, self.temp_formatted_descriptions_by_iid.pop(iid, ""))
                    had_existing_changes = True
                if iid in self.notes_dirty_iids:
                    job.notes = self.notes_text.get("1.0", tk.END).strip() if iid == self.selected_iid else job.notes
                    self.db.save_user_fields(job_id=job.db_id, status=job.status, score=job.manual_score, company_rating=job.branch, notes=job.notes)
                    self.notes_dirty_iids.discard(iid)
                    had_existing_changes = True
                if self._persist_temp_ai_evaluation(iid, job.db_id):
                    ai_saved_count += 1
                    had_existing_changes = True
                if had_existing_changes:
                    updated_count += 1
                self._update_dirty_indicators_for_iid(iid)
        self.refresh_jobs_preserving_detail_tabs()
        self.status_label_var.set(
            f"Saved {saved_count} new* jobs and updates for {updated_count} existing jobs "
            f"({ai_saved_count} AI evaluations)."
        )

    def _application_details_dialog(self, parent=None) -> dict[str, str] | None:
        dialog = tk.Toplevel(parent or self)
        dialog.title("Application details")
        dialog.geometry("580x470")
        dialog.transient(parent or self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        expected_salary = tk.StringVar()
        available_from = tk.StringVar()
        applied_via = tk.StringVar(value="Homepage")
        additional_info = tk.StringVar()
        rows = [
            ("Expected salary:", ttk.Entry(frame, textvariable=expected_salary, width=44)),
            ("Available from:", ttk.Entry(frame, textvariable=available_from, width=44)),
            ("Applied via:", ttk.Combobox(frame, textvariable=applied_via, values=["Homepage", "LinkedIn", "E-mail", "Recruiter", "Other"], width=41)),
            ("Additional info / URL:", ttk.Entry(frame, textvariable=additional_info, width=44)),
        ]
        for row, (label, widget) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            widget.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Application notes:").grid(row=4, column=0, sticky="nw", pady=4)
        notes = tk.Text(frame, height=9, wrap=tk.WORD, background=DARK_THEME["entry_bg"], foreground=DARK_THEME["entry_fg"], insertbackground=DARK_THEME["fg"], font=("Segoe UI", 10))
        notes.grid(row=4, column=1, sticky="nsew", pady=4)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        result: dict[str, str] | None = None
        def accept() -> None:
            nonlocal result
            result = {
                "expected_salary": expected_salary.get().strip(),
                "available_from": available_from.get().strip(),
                "applied_via": applied_via.get().strip(),
                "additional_info": additional_info.get().strip(),
                "application_notes": notes.get("1.0", tk.END).strip(),
            }
            dialog.destroy()
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="OK", command=accept).pack(side=tk.RIGHT, padx=(0, 8))
        self._center_child_window(dialog, parent or self) if hasattr(self, "_center_child_window") else None
        dialog.wait_window()
        return result

    def _is_applied_status(self, status: str) -> bool:
        return self._status_key(status) == "applied"

    def _save_application_details_for_job(self, job_id: int, details: dict[str, str] | None) -> None:
        if not details:
            return
        self.db.save_application_details(
            job_id,
            details.get("expected_salary", ""),
            details.get("available_from", ""),
            details.get("applied_via", ""),
            details.get("additional_info", ""),
            details.get("application_notes", ""),
        )

    def save_selection(self) -> None:
        selection = list(self.tree.selection())
        if not selection:
            return
        new_status = self.edit_status_var.get()
        if len(selection) > 1:
            reason = ""
            application_details = None
            if self._is_rejected_by_me_status(new_status):
                reason = self.ask_reject_reason(initial_reasons=self._suggested_reject_reasons_for_iids(selection))
                if reason is None:
                    return
            if self._is_applied_status(new_status):
                transitioning = any(
                    (job := self._get_display_job(iid)) is not None and not self._is_applied_status(job.status)
                    for iid in selection
                )
                if transitioning:
                    application_details = self._application_details_dialog(self)
                    if application_details is None:
                        return
            for iid in selection:
                job = self._get_display_job(iid)
                if job is None:
                    continue
                if job.db_id is None:
                    job_id = self._save_pending_job(iid, new_status, branch=job.branch, notes=job.notes, reject_reason=reason)
                    if job_id is not None:
                        self._save_application_details_for_job(job_id, application_details)
                else:
                    self.db.update_jobs_status([job.db_id], new_status, reason)
                    self._save_application_details_for_job(job.db_id, application_details)
            self.refresh_jobs()
            return
        self.save_current_job(selection[0])

    def save_current_job(self, iid: str | None = None) -> None:
        if iid is None:
            iid = self.selected_iid
        if iid is None:
            return
        display_job = self._get_display_job(iid)
        if display_job is None:
            return

        raw_score = self.score_var.get().strip()
        if not raw_score:
            score = None
        else:
            try:
                score = int(raw_score)
            except ValueError:
                messagebox.showwarning("Invalid score", "Score must be empty or an integer.")
                return
            if score < 0 or score > 100:
                messagebox.showwarning("Invalid score", "Score should be between 0 and 100.")
                return

        status = self.edit_status_var.get()
        reject_reason = ""
        application_details = None
        if self._is_applied_status(status) and not self._is_applied_status(display_job.status):
            application_details = self._application_details_dialog(self)
            if application_details is None:
                return
        if self._is_rejected_by_me_status(status) and not self._is_rejected_by_me_status(display_job.status):
            reason = self.ask_reject_reason(initial_reasons=self._suggested_reject_reasons_for_iids([iid]))
            if reason is None:
                return
            reject_reason = reason

        branch = self.company_rating_var.get().strip()
        notes = self.notes_text.get("1.0", tk.END).strip()
        if display_job.db_id is None:
            saved_job_id = self._save_pending_job(iid, status, score=score, branch=branch, notes=notes, reject_reason=reject_reason)
            if saved_job_id is not None:
                self._save_application_details_for_job(saved_job_id, application_details)
        else:
            self.db.save_user_fields(job_id=display_job.db_id, status=status, score=score, company_rating=branch, notes=notes, reject_reason=reject_reason)
            self._save_application_details_for_job(display_job.db_id, application_details)
            display_job.status = status
            display_job.manual_score = score
            display_job.branch = branch
            display_job.notes = notes
            self.notes_dirty_iids.discard(iid)
        self.company_rating_combo.configure(values=self.db.list_company_ratings())
        self._update_dirty_indicators_for_iid(iid)
        self.refresh_jobs_preserving_detail_tabs()

    def _suggested_reject_reasons_for_iids(self, iids: list[str]) -> list[str]:
        """Return configured rejection reasons suggested by AI for the selected jobs.

        Matching is deliberately tolerant of capitalization, punctuation and common
        separators because LLMs may return an allowed tag with slightly different
        formatting. Only configured reasons are ever selected.
        """
        def normalized_key(value: object) -> str:
            text = str(value or "").strip().casefold()
            text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
            return " ".join(text.split())

        configured_by_key = {
            normalized_key(reason): str(reason)
            for reason in self.reject_reasons
            if normalized_key(reason)
        }
        selected: list[str] = []
        seen: set[str] = set()

        def add_candidate(value: object) -> None:
            text = str(value or "").strip()
            if not text:
                return
            key = normalized_key(text)
            configured = configured_by_key.get(key)
            if configured and configured.casefold() not in seen:
                seen.add(configured.casefold())
                selected.append(configured)

        def add_candidates_from_reason(value: object) -> None:
            # reject_reason is usually prose, but models sometimes place one or more
            # configured tags here. Try the complete value and then common separators.
            text = str(value or "").strip()
            if not text:
                return
            add_candidate(text)
            for part in re.split(r"[,;|/\n]+|\s+and\s+|\s+und\s+", text, flags=re.IGNORECASE):
                add_candidate(part)

        for iid in iids:
            screening = self.temp_ai_screening_details_by_iid.get(iid, {})
            tags = screening.get("rejection_tags") if isinstance(screening, dict) else []
            if isinstance(tags, list):
                for tag in tags:
                    add_candidate(tag)
            if isinstance(screening, dict):
                add_candidates_from_reason(screening.get("reject_reason"))

            evaluation = self.temp_ai_evaluations_by_iid.get(iid, {})
            if not evaluation:
                job = self._get_display_job(iid)
                if job is not None and job.db_id is not None:
                    evaluation = self.db.get_latest_ai_evaluation(job.db_id) or {}
            if isinstance(evaluation, dict):
                tags = evaluation.get("rejection_tags")
                if isinstance(tags, list):
                    for tag in tags:
                        add_candidate(tag)
                add_candidates_from_reason(evaluation.get("reject_reason"))

                # Backward compatibility for evaluations saved before structured
                # rejection fields existed. Extract only configured tags from the
                # human-readable texts; never use arbitrary prose as a selection.
                combined_text = "\n".join(
                    str(evaluation.get(field) or "")
                    for field in ("summary", "rating")
                )
                normalized_combined = f" {normalized_key(combined_text)} "
                for key, configured in configured_by_key.items():
                    if key and f" {key} " in normalized_combined and configured.casefold() not in seen:
                        seen.add(configured.casefold())
                        selected.append(configured)

        return selected

    def _removal_scroll_anchor(self, removed_iids: set[str] | list[str]) -> tuple[str | None, str | None]:
        """Return the preferred row to place at the top after rows disappear.

        The first choice is the first surviving row below the last removed row.  This
        keeps the user's reading position stable when a selected block is rejected.
        If there is no following row, the nearest surviving row above the block is
        used as a fallback.
        """
        removed = {str(iid) for iid in removed_iids}
        children = list(self.tree.get_children(""))
        positions = [index for index, iid in enumerate(children) if iid in removed]
        if not positions:
            return None, None

        first_index = min(positions)
        last_index = max(positions)
        following = next((iid for iid in children[last_index + 1:] if iid not in removed), None)
        preceding = next((iid for iid in reversed(children[:first_index]) if iid not in removed), None)
        return following, preceding

    def _restore_removal_scroll_position(self, preferred_iid: str | None, fallback_iid: str | None = None) -> None:
        """Place the row following a removed selection at the top of the Treeview."""
        children = list(self.tree.get_children(""))
        if not children:
            return
        anchor = preferred_iid if preferred_iid and self.tree.exists(preferred_iid) else fallback_iid
        if not anchor or not self.tree.exists(anchor):
            return
        try:
            index = children.index(anchor)
        except ValueError:
            return

        # Treeview.yview_moveto uses a fraction of the complete scrollable content.
        # Using the item's index therefore positions it as the first visible row
        # (except close to the very end, where Tk clamps to the maximum scroll).
        self.tree.update_idletasks()
        self.tree.yview_moveto(index / max(1, len(children)))
        self.tree.update_idletasks()

    def _reject_iids_with_reasons(self, reasons_by_iid: dict[str, str]) -> tuple[int, int]:
        saved_pending = 0
        rejected_saved = 0
        for iid, reason in reasons_by_iid.items():
            job = self._get_display_job(iid)
            if job is None:
                continue
            notes = job.notes
            if iid == self.selected_iid:
                try:
                    notes = self.notes_text.get("1.0", tk.END).strip()
                except Exception:
                    pass
            if job.db_id is None:
                if self._save_pending_job(iid, self._rejected_by_me_status(), branch=job.branch, notes=notes, reject_reason=reason) is not None:
                    saved_pending += 1
            else:
                self.db.update_jobs_status([job.db_id], self._rejected_by_me_status(), reason)
                rejected_saved += 1
        return rejected_saved, saved_pending

    def reject_selected_jobs(self) -> None:
        selection = list(self.tree.selection())
        if not selection:
            messagebox.showinfo("No selection", "Please select at least one job.")
            return

        rejected_iids: set[str] = set()

        if len(selection) == 1:
            reasons = self._suggested_reject_reasons_for_iids(selection)
            reason = self.ask_reject_reason(initial_reasons=reasons)
            if reason is None:
                return
            rejected_saved, saved_pending = self._reject_iids_with_reasons({selection[0]: reason})
            rejected_iids.add(selection[0])
        else:
            per_job = {iid: self._suggested_reject_reasons_for_iids([iid]) for iid in selection}
            with_ai = {iid: reasons for iid, reasons in per_job.items() if reasons}
            without_ai = [iid for iid, reasons in per_job.items() if not reasons]
            distinct = sorted({reason for reasons in with_ai.values() for reason in reasons}, key=str.casefold)
            if not with_ai:
                reason = self.ask_reject_reason(initial_reasons=[])
                if reason is None:
                    return
                rejected_saved, saved_pending = self._reject_iids_with_reasons({iid: reason for iid in selection})
                rejected_iids.update(selection)
                preferred_anchor, fallback_anchor = self._removal_scroll_anchor(rejected_iids)
                self.refresh_jobs()
                self._restore_removal_scroll_position(preferred_anchor, fallback_anchor)
                self.status_label_var.set(f"Rejected {rejected_saved + saved_pending} jobs. Saved {saved_pending} new* jobs as rejected.")
                return
            prompt = (
                f"AI rejection reasons are available for {len(with_ai)} of {len(selection)} selected jobs.\n"
                f"They contain {len(distinct)} different reasons in total.\n"
                f"{len(without_ai)} jobs have no AI rejection reasons.\n\n"
                "Apply the AI suggestions individually to each job?\n\n"
                "Yes: reject jobs with their individual AI reasons.\n"
                "No: choose one shared set of reasons for all selected jobs."
            )
            answer = messagebox.askyesnocancel("Reject selected jobs", prompt, parent=self)
            if answer is None:
                return
            rejected_saved = saved_pending = 0
            if answer is False:
                reason = self.ask_reject_reason(initial_reasons=[])
                if reason is None:
                    return
                rejected_saved, saved_pending = self._reject_iids_with_reasons({iid: reason for iid in selection})
                rejected_iids.update(selection)
            else:
                if with_ai:
                    ai_map = {iid: ", ".join(reasons) for iid, reasons in with_ai.items()}
                    r, p = self._reject_iids_with_reasons(ai_map)
                    rejected_saved += r
                    saved_pending += p
                    rejected_iids.update(ai_map)
                if without_ai:
                    choose = messagebox.askyesno(
                        "Jobs without AI reasons",
                        f"{len(without_ai)} jobs have no AI rejection reasons.\n\n"
                        "Choose shared reasons for these remaining jobs?\n"
                        "No leaves them unchanged.",
                        parent=self,
                    )
                    if choose:
                        reason = self.ask_reject_reason(initial_reasons=[])
                        if reason is not None:
                            remaining_map = {iid: reason for iid in without_ai}
                            r, p = self._reject_iids_with_reasons(remaining_map)
                            rejected_saved += r
                            saved_pending += p
                            rejected_iids.update(remaining_map)

        preferred_anchor, fallback_anchor = self._removal_scroll_anchor(rejected_iids)
        self.refresh_jobs()
        self._restore_removal_scroll_position(preferred_anchor, fallback_anchor)
        self.status_label_var.set(f"Rejected {rejected_saved + saved_pending} jobs. Saved {saved_pending} new* jobs as rejected.")

    def delete_selected_jobs(self, event: tk.Event | None = None) -> str:
        selection = list(self.tree.selection())
        if not selection:
            return "break"

        pending_iids: list[str] = []
        stored_ids: list[int] = []
        for iid in selection:
            job = self._get_display_job(iid)
            if job is None:
                continue
            if job.db_id is None:
                pending_iids.append(iid)
            else:
                stored_ids.append(job.db_id)

        if pending_iids and stored_ids:
            proceed = messagebox.askokcancel(
                "Mixed selection",
                "The selection contains unsaved search results and saved jobs.\n\n"
                "Unsaved entries will only be removed from the current list.\n"
                "Saved jobs will be marked as rejected.\n\n"
                "Continue?",
                parent=self,
            )
            if not proceed:
                return "break"

        preferred_anchor, fallback_anchor = self._removal_scroll_anchor(set(pending_iids) | set(selection))

        reason = ""
        if stored_ids:
            chosen_reason = self.ask_reject_reason()
            if chosen_reason is None:
                return "break"
            reason = chosen_reason

        for iid in pending_iids:
            self.pending_by_iid.pop(iid, None)
            self.temp_routes_by_iid.pop(iid, None)
            self.temp_ai_evaluations_by_iid.pop(iid, None)

        if stored_ids:
            self.db.update_jobs_status(stored_ids, self._rejected_by_me_status(), reason)

        self.refresh_jobs()
        self._restore_removal_scroll_position(preferred_anchor, fallback_anchor)
        return "break"

    def ask_reject_reason(self, selection_required: bool = True, initial_reasons: list[str] | None = None) -> str | None:
        dialog = tk.Toplevel(self)
        dialog.title("Reject reason" if selection_required else "Edit reject reasons")
        dialog.geometry("820x560")
        dialog.transient(self)
        self._center_child_window(dialog, width=820, height=560)
        dialog.grab_set()
        result: dict[str, str | None] = {"value": None}

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text=("Select one or more rejection reasons. Categories are only used for organization." if selection_required else "Manage rejection reasons, explanations and categories."),
        ).pack(anchor=tk.W)
        ttk.Label(frame, text="* System-defined entry; required by JobRadar and cannot be changed.").pack(anchor=tk.W, pady=(2, 0))

        main_area = ttk.Frame(frame)
        main_area.pack(fill=tk.BOTH, expand=True, pady=(8, 8))

        tree = ttk.Treeview(
            main_area,
            columns=("description",),
            show="tree headings",
            selectmode="extended",
        )
        tree.heading("#0", text="Category / rejection tag", anchor=tk.W)
        tree.heading("description", text="Explanation for AI", anchor=tk.W)
        tree.column("#0", width=250, minwidth=180, stretch=False)
        tree.column("description", width=500, minwidth=250, stretch=True)
        yscroll = ttk.Scrollbar(main_area, orient=tk.VERTICAL, command=tree.yview)
        xscroll = ttk.Scrollbar(main_area, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        main_area.rowconfigure(0, weight=1)
        main_area.columnconfigure(0, weight=1)

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X)
        left_buttons = ttk.Frame(button_row)
        left_buttons.pack(side=tk.LEFT)
        right_buttons = ttk.Frame(button_row)
        right_buttons.pack(side=tk.RIGHT)

        def normalize_categories() -> None:
            categories = []
            seen = set()
            for raw in self.reject_reason_categories:
                value = str(raw or "").strip()
                if not value or value.casefold() in seen:
                    continue
                seen.add(value.casefold())
                categories.append(value)
            categories = [
                item for item in categories
                if item not in {"Uncategorized", SYSTEM_REJECT_REASON_CATEGORY}
            ]
            categories.sort(key=str.casefold)
            self.reject_reason_categories[:] = ["Uncategorized", SYSTEM_REJECT_REASON_CATEGORY, *categories]
            valid = set(self.reject_reason_categories)
            for reason in self.reject_reasons:
                if reason in SYSTEM_REJECT_REASONS:
                    self.reject_reason_category_by_name[reason] = SYSTEM_REJECT_REASON_CATEGORY
                    continue
                category = str(self.reject_reason_category_by_name.get(reason, "") or "").strip()
                if category not in valid:
                    self.reject_reason_category_by_name[reason] = "Uncategorized"

        reason_name_by_iid: dict[str, str] = {}
        category_name_by_iid: dict[str, str] = {}

        def selected_reason_names() -> list[str]:
            values = []
            for iid in tree.selection():
                tags = set(tree.item(iid, "tags"))
                if "reason" in tags and iid in reason_name_by_iid:
                    values.append(reason_name_by_iid[iid])
            return values

        def selected_category_name() -> str | None:
            selection = tree.selection()
            if len(selection) != 1:
                return None
            iid = selection[0]
            if "category" not in set(tree.item(iid, "tags")):
                return None
            return category_name_by_iid.get(iid)

        def reload_tree(select_reasons: list[str] | None = None) -> None:
            normalize_categories()
            desired = set(select_reasons or [])
            tree.delete(*tree.get_children())
            reason_name_by_iid.clear()
            category_name_by_iid.clear()
            category_iids: dict[str, str] = {}
            for category in self.reject_reason_categories:
                is_system_category = category == SYSTEM_REJECT_REASON_CATEGORY
                category_iid = tree.insert(
                    "",
                    tk.END,
                    text=f"{category} *" if is_system_category else category,
                    values=("",),
                    open=True,
                    tags=("category", "system") if is_system_category else ("category",),
                )
                category_iids[category] = category_iid
                category_name_by_iid[category_iid] = category
            for reason in sorted(set(self.reject_reasons), key=str.casefold):
                category = str(self.reject_reason_category_by_name.get(reason, "Uncategorized") or "Uncategorized")
                if category not in category_iids:
                    category = "Uncategorized"
                    self.reject_reason_category_by_name[reason] = category
                is_system_reason = reason in SYSTEM_REJECT_REASONS
                iid = tree.insert(
                    category_iids[category],
                    tk.END,
                    text=f"{reason} *" if is_system_reason else reason,
                    values=(str(self.reject_reason_descriptions.get(reason, "") or ""),),
                    tags=("reason", "system") if is_system_reason else ("reason",),
                )
                reason_name_by_iid[iid] = reason
                if reason in desired:
                    tree.selection_add(iid)
                    tree.see(iid)

        def reason_editor(title: str, initial_name: str = "", initial_description: str = "") -> tuple[str, str] | None:
            editor = tk.Toplevel(dialog)
            editor.title(title)
            editor.geometry("620x340")
            editor.transient(dialog)
            editor.grab_set()
            self._center_child_window(editor, width=620, height=340)
            value: dict[str, tuple[str, str] | None] = {"result": None}
            body = ttk.Frame(editor, padding=12)
            body.pack(fill=tk.BOTH, expand=True)
            ttk.Label(body, text="Rejection tag:").pack(anchor=tk.W)
            name_var = tk.StringVar(value=initial_name)
            name_entry = ttk.Entry(body, textvariable=name_var)
            name_entry.pack(fill=tk.X, pady=(4, 12))
            ttk.Label(body, text="Explanation for AI (optional):").pack(anchor=tk.W)
            description = tk.Text(body, height=9, wrap=tk.WORD)
            description.pack(fill=tk.BOTH, expand=True, pady=(4, 12))
            description.insert("1.0", initial_description)
            buttons = ttk.Frame(body)
            buttons.pack(fill=tk.X)

            def save() -> None:
                name = name_var.get().strip()
                if not name:
                    messagebox.showwarning("Missing tag", "Please enter a rejection tag.", parent=editor)
                    return
                value["result"] = (name, description.get("1.0", tk.END).strip())
                editor.destroy()

            ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT)
            ttk.Button(buttons, text="Cancel", command=editor.destroy).pack(side=tk.RIGHT, padx=(0, 8))
            def save_on_enter(event: tk.Event):
                if event.state & 0x0001:  # Shift+Enter inserts a line break in the explanation.
                    return None
                save()
                return "break"

            editor.bind("<Return>", save_on_enter)
            description.bind("<Return>", save_on_enter)
            description.bind("<Shift-Return>", lambda _event: None)
            editor.protocol("WM_DELETE_WINDOW", editor.destroy)
            if not initial_name.strip():
                name_entry.focus_set()
            elif not initial_description.strip():
                description.focus_set()
                description.mark_set(tk.INSERT, "1.0")
            else:
                name_entry.focus_set()
                name_entry.selection_range(0, tk.END)
            self.wait_window(editor)
            return value["result"]

        def add_category() -> None:
            name = simpledialog.askstring("Add category", "Category name:", parent=dialog)
            if not name or not name.strip():
                return
            name = name.strip()
            if name.casefold() == "uncategorized" or any(name.casefold() == c.casefold() for c in self.reject_reason_categories):
                messagebox.showwarning("Category exists", "That category already exists.", parent=dialog)
                return
            self.reject_reason_categories.append(name)
            self._save_user_config()
            reload_tree()

        def edit_category() -> None:
            old = selected_category_name()
            if not old:
                messagebox.showinfo("Select category", "Select exactly one category first.", parent=dialog)
                return
            if old in {"Uncategorized", SYSTEM_REJECT_REASON_CATEGORY}:
                messagebox.showinfo("Fixed category", f"{old} cannot be renamed.", parent=dialog)
                return
            new = simpledialog.askstring("Edit category", "Category name:", initialvalue=old, parent=dialog)
            if not new or not new.strip():
                return
            new = new.strip()
            if any(new.casefold() == c.casefold() and c != old for c in self.reject_reason_categories):
                messagebox.showwarning("Category exists", "That category already exists.", parent=dialog)
                return
            self.reject_reason_categories[self.reject_reason_categories.index(old)] = new
            for reason, category in list(self.reject_reason_category_by_name.items()):
                if category == old:
                    self.reject_reason_category_by_name[reason] = new
            self._save_user_config()
            reload_tree()

        def remove_category() -> None:
            category = selected_category_name()
            if not category:
                messagebox.showinfo("Select category", "Select exactly one category first.", parent=dialog)
                return
            if category in {"Uncategorized", SYSTEM_REJECT_REASON_CATEGORY}:
                messagebox.showinfo("Fixed category", f"{category} cannot be deleted.", parent=dialog)
                return
            if not messagebox.askyesno(
                "Delete category",
                f'Delete category "{category}"?\n\nIts rejection reasons will be moved to Uncategorized.',
                parent=dialog,
            ):
                return
            self.reject_reason_categories.remove(category)
            for reason, assigned in list(self.reject_reason_category_by_name.items()):
                if assigned == category:
                    self.reject_reason_category_by_name[reason] = "Uncategorized"
            self._save_user_config()
            reload_tree()

        def add_reason() -> None:
            result_value = reason_editor("Add rejection reason")
            if result_value is None:
                return
            name, description = result_value
            if any(name.casefold() == item.casefold() for item in self.reject_reasons):
                messagebox.showwarning("Tag exists", "That rejection tag already exists.", parent=dialog)
                return
            category = selected_category_name() or "Uncategorized"
            self.reject_reasons.append(name)
            self.reject_reason_descriptions[name] = description
            self.reject_reason_category_by_name[name] = category
            self._save_user_config()
            reload_tree([name])

        def edit_reason() -> None:
            reasons = selected_reason_names()
            if len(reasons) != 1:
                messagebox.showinfo("Select reason", "Select exactly one rejection reason first.", parent=dialog)
                return
            old = reasons[0]
            if old in SYSTEM_REJECT_REASONS:
                messagebox.showinfo("Fixed rejection reason", f'"{old}" is required by JobRadar and cannot be edited.', parent=dialog)
                return
            result_value = reason_editor(
                "Edit rejection reason",
                old,
                str(self.reject_reason_descriptions.get(old, "") or ""),
            )
            if result_value is None:
                return
            new, description = result_value
            if any(new.casefold() == item.casefold() and item != old for item in self.reject_reasons):
                messagebox.showwarning("Tag exists", "That rejection tag already exists.", parent=dialog)
                return
            index = self.reject_reasons.index(old)
            self.reject_reasons[index] = new
            category = self.reject_reason_category_by_name.pop(old, "Uncategorized")
            self.reject_reason_category_by_name[new] = category
            self.reject_reason_descriptions.pop(old, None)
            self.reject_reason_descriptions[new] = description
            self._save_user_config()
            reload_tree([new])

        def remove_reason() -> None:
            reasons = selected_reason_names()
            if not reasons:
                messagebox.showinfo("Select reason", "Select at least one rejection reason.", parent=dialog)
                return
            fixed = [reason for reason in reasons if reason in SYSTEM_REJECT_REASONS]
            if fixed:
                messagebox.showinfo(
                    "Fixed rejection reason",
                    "The following system-defined rejection reason(s) cannot be deleted:\n\n" + "\n".join(fixed),
                    parent=dialog,
                )
                return
            if not messagebox.askyesno(
                "Delete rejection reasons",
                f"Delete {len(reasons)} selected rejection reason(s)?",
                parent=dialog,
            ):
                return
            for reason in reasons:
                if reason in self.reject_reasons:
                    self.reject_reasons.remove(reason)
                self.reject_reason_descriptions.pop(reason, None)
                self.reject_reason_category_by_name.pop(reason, None)
            self._save_user_config()
            reload_tree()

        def move_reasons_to(category: str) -> None:
            reasons = selected_reason_names()
            if not reasons:
                return
            fixed = [reason for reason in reasons if reason in SYSTEM_REJECT_REASONS]
            if fixed:
                messagebox.showinfo(
                    "Fixed rejection reason",
                    "System-defined rejection reasons stay in the General category.",
                    parent=dialog,
                )
                return
            for reason in reasons:
                self.reject_reason_category_by_name[reason] = category
            self._save_user_config()
            reload_tree(reasons)

        def on_tree_left_click(event: tk.Event):
            """Toggle rejection reasons without requiring Ctrl.

            Category rows keep normal single-selection behaviour so category
            management remains predictable. Clicking the expand/collapse
            indicator is left to the Treeview itself.
            """
            iid = tree.identify_row(event.y)
            if not iid:
                return None
            element = tree.identify_element(event.x, event.y)
            if element in {"Treeitem.indicator", "indicator"}:
                return None
            tags = set(tree.item(iid, "tags"))
            if "reason" not in tags:
                return None
            tree.focus(iid)
            if iid in tree.selection():
                tree.selection_remove(iid)
            else:
                tree.selection_add(iid)
            return "break"

        def on_tree_mousewheel(event: tk.Event):
            delta = getattr(event, "delta", 0)
            if delta:
                tree.yview_scroll(-3 if delta > 0 else 3, "units")
            return "break"

        tree.bind("<Button-1>", on_tree_left_click, add="+")
        tree.bind("<MouseWheel>", on_tree_mousewheel, add="+")
        tree.bind("<Button-4>", lambda _event: (tree.yview_scroll(-3, "units"), "break")[1], add="+")
        tree.bind("<Button-5>", lambda _event: (tree.yview_scroll(3, "units"), "break")[1], add="+")

        context_menu = tk.Menu(tree, tearoff=False)
        move_menu = tk.Menu(context_menu, tearoff=False)
        context_menu.add_command(label="Edit entry", command=edit_reason)
        context_menu.add_separator()
        context_menu.add_cascade(label="Move to", menu=move_menu)

        def rebuild_context_menu() -> None:
            move_menu.delete(0, tk.END)
            for category in self.reject_reason_categories:
                move_menu.add_command(label=category, command=lambda c=category: move_reasons_to(c))

        def show_context_menu(event: tk.Event) -> None:
            iid = tree.identify_row(event.y)
            if iid:
                if iid not in tree.selection():
                    tags = set(tree.item(iid, "tags"))
                    if "reason" in tags:
                        tree.selection_add(iid)
                    else:
                        tree.selection_set(iid)
                rebuild_context_menu()
                if selected_reason_names():
                    context_menu.tk_popup(event.x_root, event.y_root)
            return None

        def on_double_click(_event: tk.Event) -> None:
            if selected_reason_names():
                edit_reason()
            elif selected_category_name():
                edit_category()

        def ok() -> None:
            if not selection_required:
                result["value"] = None
                dialog.destroy()
                return
            selected = selected_reason_names()
            if not selected:
                messagebox.showinfo("No rejection reason", "Select at least one rejection reason.", parent=dialog)
                return
            result["value"] = "; ".join(selected)
            dialog.destroy()

        def cancel() -> None:
            result["value"] = None
            dialog.destroy()

        ttk.Button(left_buttons, text="Add category", command=add_category).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(left_buttons, text="Edit category", command=edit_category).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(left_buttons, text="Delete category", command=remove_category).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Button(left_buttons, text="Add reason", command=add_reason).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(left_buttons, text="Edit reason", command=edit_reason).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(left_buttons, text="Delete reason", command=remove_reason).pack(side=tk.LEFT)
        ttk.Button(right_buttons, text="Cancel", command=cancel).pack(side=tk.RIGHT)
        ttk.Button(right_buttons, text=("OK" if selection_required else "Close"), command=ok).pack(side=tk.RIGHT, padx=(0, 8))

        tree.bind("<Button-3>", show_context_menu)
        tree.bind("<Double-1>", on_double_click)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        reload_tree(initial_reasons)
        self.wait_window(dialog)
        return result["value"]


    def _unsaved_work_items(self) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for iid, candidate in self.pending_by_iid.items():
            label = f"{candidate.title} — {candidate.company}".strip(" —")
            items.append((iid, "job", label or iid))
            seen.add(iid)
        dirty_iids = set(self.temp_ai_evaluations_by_iid) | set(self.temp_industries_by_iid) | set(self.temp_formatted_descriptions_by_iid) | set(self.notes_dirty_iids)
        for iid in dirty_iids:
            if iid in seen:
                continue
            job = self._get_display_job(iid)
            if job is None:
                continue
            kinds = []
            if iid in self.temp_ai_evaluations_by_iid:
                kinds.append("AI result")
            if iid in self.temp_industries_by_iid:
                kinds.append("industry")
            if iid in self.temp_formatted_descriptions_by_iid:
                kinds.append("formatted description")
            if iid in self.notes_dirty_iids:
                kinds.append("notes")
            label = f"{', '.join(kinds)}: {job.title} — {job.company}".strip(" —")
            items.append((iid, "changes", label or iid))
        return items

    def _save_all_unsaved_work(self) -> tuple[int, int]:
        saved_jobs = 0
        saved_ai = 0
        for iid in list(self.pending_by_iid.keys()):
            job = self._get_display_job(iid)
            candidate = self.pending_by_iid.get(iid)
            if candidate is None:
                continue
            profile = self.db.get_company_profile(candidate.company)
            status = self._rejected_by_me_status() if profile["blacklisted"] else "new"
            branch = str(profile.get("branch") or "")
            had_ai = iid in self.temp_ai_evaluations_by_iid
            job_id = self._save_pending_job(iid, status, branch=branch, notes=(job.notes if job else ""))
            if job_id is not None:
                saved_jobs += 1
                if had_ai:
                    saved_ai += 1
        dirty_iids = set(self.temp_ai_evaluations_by_iid) | set(self.temp_industries_by_iid) | set(self.temp_formatted_descriptions_by_iid) | set(self.notes_dirty_iids)
        for iid in list(dirty_iids):
            job = self._get_display_job(iid)
            if job is None or job.db_id is None:
                continue
            if iid in self.temp_industries_by_iid:
                job.branch = self.temp_industries_by_iid.pop(iid, job.branch)
                self.db.save_user_fields(job_id=job.db_id, status=job.status, score=job.manual_score, company_rating=job.branch, notes=job.notes)
            if iid in self.temp_formatted_descriptions_by_iid:
                self.db.save_job_formatted_description(job.db_id, self.temp_formatted_descriptions_by_iid.pop(iid, ""))
            if iid in self.notes_dirty_iids:
                if iid == self.selected_iid:
                    job.notes = self.notes_text.get("1.0", tk.END).strip()
                self.db.save_user_fields(job_id=job.db_id, status=job.status, score=job.manual_score, company_rating=job.branch, notes=job.notes)
                self.notes_dirty_iids.discard(iid)
            if self._persist_temp_ai_evaluation(iid, job.db_id):
                saved_ai += 1
        return saved_jobs, saved_ai

    def on_close(self) -> None:
        unsaved = self._unsaved_work_items()
        if unsaved:
            preview = "\n".join(f"- {label}" for _iid, _kind, label in unsaved[:15])
            if len(unsaved) > 15:
                preview += f"\n- ... and {len(unsaved) - 15} more"
            choice = messagebox.askyesnocancel(
                "Unsaved JobRadar entries",
                "There are unsaved new* jobs or temporary changes.\n\n"
                "Save them before closing?\n\n"
                f"{preview}",
                parent=self,
            )
            if choice is None:
                return
            if choice is True:
                try:
                    saved_jobs, saved_ai = self._save_all_unsaved_work()
                    self.status_label_var.set(f"Saved {saved_jobs} jobs and {saved_ai} AI results before closing.")
                except Exception as exc:
                    messagebox.showerror("Save before close failed", str(exc), parent=self)
                    return
        try:
            if getattr(self, "agent_controller", None) is not None and self.agent_controller.is_running:
                self.agent_controller.request_stop()
        except Exception:
            pass
        try:
            self._save_user_config()
        except Exception:
            pass
        for task in list(getattr(self, "background_tasks", [])):
            self._destroy_background_task_window(task)
        self.destroy()

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def _numeric_score_for_sort(self, job: DisplayJob) -> int | None:
        score_text = self._display_score(job).strip()
        try:
            return int(score_text)
        except Exception:
            return None

    def _status_sort_key(self, status: str) -> tuple[int, int, str]:
        category = self._status_category(status)
        category_rank = {"green": 0, "red": 1, "blue": 2, "uncolored": 3}.get(category, 3)
        values = self.status_categories.get(category, [])
        status_key = self._status_key(status)
        try:
            pos = [self._status_key(v) for v in values].index(status_key)
        except ValueError:
            pos = 999
        return (category_rank, pos, str(status).lower())

    def sort_by_column(self, column: str) -> None:
        if self.current_sort_column == column:
            self.current_sort_reverse = not self.current_sort_reverse
        else:
            self.current_sort_column = column
            self.current_sort_reverse = False
        self.current_jobs = self._sort_display_jobs(self.current_jobs, column, self.current_sort_reverse)
        self._reorder_tree_to_current_jobs()

    def _sort_display_jobs(self, jobs: list[DisplayJob], column: str, reverse: bool) -> list[DisplayJob]:
        def value(job: DisplayJob) -> object:
            mapping = {
                "id": job.db_id if job.db_id is not None else self.pending_import_index_by_iid.get(job.iid, 999999),
                "status": self._status_sort_key(job.status),
                "status_changed": job.status_changed_at or "",
                "score": -1 if self._numeric_score_for_sort(job) is None else self._numeric_score_for_sort(job),
                "branch": job.branch,
                "min_salary": -1.0 if job.min_salary_k is None else job.min_salary_k,
                "max_salary": -1.0 if job.max_salary_k is None else job.max_salary_k,
                "published": job.published_date,
                "source": job.source,
                "title": job.title,
                "company": job.company,
                "dist": 999999.0 if job.route_distance_km is None else job.route_distance_km,
                "time": 999999.0 if job.route_duration_min is None else job.route_duration_min,
                "location": job.location,
            }
            result = mapping.get(column, "")
            return result.lower() if isinstance(result, str) else result
        return sorted(jobs, key=value, reverse=reverse)

    def _reorder_tree_to_current_jobs(self) -> None:
        for index, job in enumerate(self.current_jobs):
            if self.tree.exists(job.iid):
                self.tree.move(job.iid, "", index)

    # ------------------------------------------------------------------
    # Geo / routing
    # ------------------------------------------------------------------

    def _resolve_profile_path_for_edit(self, profile_path_value: str | None = None) -> Path:
        value = str(profile_path_value if profile_path_value is not None else self.user_config.get("ai_profile_path") or "templates/profile_template.md").strip() or "templates/profile_template.md"
        path = Path(value)
        if path.is_absolute():
            return path
        repository_root = Path(__file__).resolve().parent.parent
        candidates = [Path.cwd() / path, repository_root / path, Path(__file__).resolve().parent / path]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        # For a missing relative profile path, create/edit it relative to the
        # repository/application root rather than inside the Python package.
        return repository_root / path

    def open_profile_editor(self, profile_path_var: tk.StringVar | None = None, parent=None) -> None:
        path_value = profile_path_var.get().strip() if profile_path_var is not None else str(self.user_config.get("ai_profile_path") or "templates/profile_template.md")
        path = self._resolve_profile_path_for_edit(path_value)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            original_text = path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception as exc:
            messagebox.showerror("Edit profile", f"Could not read profile file:\n\n{path}\n\n{exc}", parent=self)
            return

        dialog = tk.Toplevel(parent or self)
        dialog.title(f"Edit profile.md — {path}")
        dialog.geometry("900x700")
        dialog.configure(bg=DARK_THEME["bg"])
        dialog.transient(parent or self)
        dialog.grab_set()
        self._center_child_window(dialog, width=900, height=700)
        dirty = {"value": False}
        saved = {"value": False}

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=str(path), foreground=DARK_THEME["muted_fg"]).pack(fill=tk.X, pady=(0, 6))
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        editor = tk.Text(
            text_frame,
            wrap=tk.WORD,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            font=("Consolas", 10),
            undo=True,
        )
        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=editor.yview)
        editor.configure(yscrollcommand=scroll.set)
        editor.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        editor.insert("1.0", original_text)
        editor.edit_modified(False)

        status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=status_var, foreground=DARK_THEME["muted_fg"]).pack(fill=tk.X, pady=(6, 0))

        def mark_dirty(_event=None) -> None:
            if not editor.edit_modified():
                return
            editor.edit_modified(False)
            dirty["value"] = True
            status_var.set("Unsaved changes")

        editor.bind("<<Modified>>", mark_dirty)

        def save() -> bool:
            try:
                path.write_text(editor.get("1.0", tk.END).rstrip() + "\n", encoding="utf-8")
            except Exception as exc:
                messagebox.showerror("Save profile", f"Could not save profile file:\n\n{path}\n\n{exc}", parent=dialog)
                return False
            dirty["value"] = False
            saved["value"] = True
            status_var.set("Saved. Future AI requests will use this version.")
            if profile_path_var is not None:
                try:
                    profile_path_var.set(str(path.resolve().relative_to(Path.cwd().resolve())))
                except Exception:
                    profile_path_var.set(str(path))
            self.user_config["ai_profile_path"] = profile_path_var.get().strip() if profile_path_var is not None else str(path)
            self._save_user_config()
            return True

        def save_and_close_profile() -> None:
            if save():
                dialog.destroy()

        def close() -> None:
            if dirty["value"]:
                answer = messagebox.askyesnocancel("Unsaved profile", "Profile has unsaved changes. Save before closing?", parent=dialog)
                if answer is None:
                    return
                if answer is True and not save():
                    return
            dialog.destroy()

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(button_row, text="Cancel", command=close).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_row, text="Save", command=save_and_close_profile).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", close)
        editor.focus_set()


    # ------------------------------------------------------------------
    # AI model default helpers
    # ------------------------------------------------------------------

    AI_TASKS = [
        ("agent", "Agent"),
        ("chat", "Chat"),
        ("screening", "Screening"),
        ("detailed_analysis", "Detailed analysis"),
        ("list_parser", "List parser"),
        ("reformat", "Reformat"),
    ]

    def _ai_task_defaults(self) -> dict:
        defaults = self.user_config.setdefault("ai_task_defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
            self.user_config["ai_task_defaults"] = defaults
        return defaults

    def _ai_default_labels_for_model(self, model_name: str) -> str:
        defaults = self._ai_task_defaults()
        labels = []
        for key, label in self.AI_TASKS:
            if str(defaults.get(key) or "") == model_name:
                labels.append(label)
        return "; ".join(labels)

    def _first_default_ai_model_name(self) -> str:
        enabled_names = {config.name for config in self._enabled_ai_model_configs()}
        defaults = self._ai_task_defaults()
        for key, _label in self.AI_TASKS:
            name = str(defaults.get(key) or "").strip()
            if name in enabled_names:
                return name
        legacy = str(self.user_config.get("default_ai_model") or "").strip()
        if legacy in enabled_names:
            return legacy
        return ""

    def _default_ai_model_config(self, task: str | None = None) -> AiModelConfig | None:
        enabled = [config for config in self.ai_models if config.enabled]
        if not enabled:
            return None
        candidates = []
        task_key = str(task or "").strip()
        if task_key:
            candidates.append(str(self.last_ai_model_by_task.get(task_key) or "").strip())
            candidates.append(str(self._ai_task_defaults().get(task_key) or "").strip())
        candidates.append(str(getattr(self, "last_ai_model_name", "") or "").strip())
        candidates.append(self._first_default_ai_model_name())
        candidates.append(str(self.user_config.get("default_ai_model") or "").strip())
        for wanted in candidates:
            if wanted:
                for config in enabled:
                    if config.name == wanted:
                        return config
        return enabled[0]

    def _remember_ai_model_choice(self, model_name: str, task: str | None = None) -> None:
        model_name = str(model_name or "").strip()
        if model_name:
            self.last_ai_model_name = model_name
            if task:
                self.last_ai_model_by_task[str(task)] = model_name

    def _unique_ai_model_copy_name(self, base_name: str) -> str:
        existing = {config.name for config in self.ai_models}
        base = str(base_name or "AI model").strip() or "AI model"
        idx = 2
        while True:
            candidate = f"{base} ({idx})"
            if candidate not in existing:
                return candidate
            idx += 1

    @staticmethod
    def _provider_defaults(provider: str) -> dict:
        provider = str(provider or "").strip().lower()
        if provider == "openai":
            return {"base_url": "https://api.openai.com/v1/responses", "api_key_env": "OPENAI_API_KEY"}
        if provider == "mistral":
            return {"base_url": "https://api.mistral.ai/v1/chat/completions", "api_key_env": "MISTRAL_API_KEY"}
        if provider == "ollama":
            return {"base_url": "http://127.0.0.1:11434/v1/chat/completions", "api_key_env": ""}
        return {"base_url": "", "api_key_env": ""}

    @staticmethod
    def _parse_iso_date_only(value: str):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").date()
            except ValueError:
                return None

    @staticmethod
    def _normalize_job_title_for_stats(title: str) -> str:
        text = str(title or "").casefold()
        text = re.sub(r"\([^)]*(?:m\s*/?\s*w|f\s*/?\s*m|d\s*/?\s*f|w\s*/?\s*m)[^)]*\)", " ", text)
        text = re.sub(r"\b(m|w|d|f)(?:\s*/\s*(m|w|d|f))+\b", " ", text)
        text = re.sub(r"[^\w+#.-]+", " ", text, flags=re.UNICODE)
        return " ".join(text.split()).strip() or str(title or "").strip()

    def _arbeitsamt_export_data(self, start_date: date, end_date: date):
        records = self.db.list_jobs(status_filter="all", text_filter="", hide_rejected=False, show_only_new=False)
        applications = []
        rejected = []
        histories: dict[int, list[dict[str, str]]] = {}

        def history_for(record):
            if record.id not in histories:
                histories[record.id] = self.db.list_status_history(record.id)
            return histories[record.id]

        for record in records:
            history = history_for(record)
            applied_date = self._parse_iso_date_only(record.applied_at)
            if applied_date is None:
                for event in history:
                    if self._status_key(event.get("new_status", "")) == "applied":
                        applied_date = self._parse_iso_date_only(event.get("changed_at", ""))
                        if applied_date:
                            break

            current_is_blue = self._status_category(record.status) == "blue"
            current_is_rejected_by_company = self._status_key(record.status) == "rejectedbycompany"
            ever_applied = applied_date is not None

            # The application overview must only contain actual application processes:
            # jobs that were applied to at least once, are currently in a blue workflow
            # state, or were rejected by the company. Purely screened/rejected jobs are excluded.
            if ever_applied or current_is_blue or current_is_rejected_by_company:
                export_date = applied_date
                if export_date is None and current_is_rejected_by_company:
                    export_date = self._parse_iso_date_only(record.rejected_by_company_at)
                if export_date is None and current_is_blue:
                    export_date = self._parse_iso_date_only(record.status_changed_at)
                if export_date and start_date <= export_date <= end_date:
                    applications.append((export_date, record))

            rejected_date = self._parse_iso_date_only(record.rejected_by_me_at)
            if rejected_date and start_date <= rejected_date <= end_date:
                rejected.append(record)

        applications.sort(key=lambda item: (item[0], item[1].company.casefold(), item[1].title.casefold()))
        return records, applications, rejected, histories

    def _count_interviewed_applications(self, applications, histories: dict[int, list[dict[str, str]]], start_date: date, end_date: date) -> int:
        interviewed_job_ids: set[int] = set()
        for _application_date, record in applications:
            history = histories.get(record.id) or self.db.list_status_history(record.id)
            exact_interview_events = [
                event for event in history
                if self._status_key(event.get("new_status", "")) == "interview"
                and (d := self._parse_iso_date_only(event.get("changed_at", "")))
                and start_date <= d <= end_date
            ]
            if exact_interview_events:
                interviewed_job_ids.add(record.id)
                continue
            # Backward-compatible fallback for older rows without status history.
            if not history:
                interview_date = self._parse_iso_date_only(record.interview_at)
                if interview_date and start_date <= interview_date <= end_date:
                    interviewed_job_ids.add(record.id)
        return len(interviewed_job_ids)

    def _export_location(self, record: JobRecord) -> str:
        location = str(record.location or "").strip()
        country = self._country_display(record.country).strip()
        if not country or country.casefold() in location.casefold():
            return location or "-"
        return f"{location}, {country}" if location else country

    def _export_interview_date(self, record: JobRecord, histories: dict[int, list[dict[str, str]]]):
        dates = []
        for event in histories.get(record.id) or []:
            if self._status_key(event.get("new_status", "")) == "interview":
                value = self._parse_iso_date_only(event.get("changed_at", ""))
                if value is not None:
                    dates.append(value)
        if dates:
            return min(dates)
        return self._parse_iso_date_only(record.interview_at)

    @staticmethod
    def _tsv_cell(value) -> str:
        return str(value if value is not None else "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()

    def _build_arbeitsamt_export(self, start_date: date, end_date: date, options: dict[str, bool]) -> str:
        records, applied, rejected, histories = self._arbeitsamt_export_data(start_date, end_date)
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            "JobRadar Export",
            f"Zeitraum: {start_date.isoformat()} bis {end_date.isoformat()}",
            f"Erstellt: {generated}",
            "",
            f"Bewerbungen ({len(applied)})",
            "",
        ]
        headers = ["Nr", "Bewerbungsdatum", "Titel", "Firma", "Ort", "Aktueller Status", "Interviewdatum"]
        if options.get("salary"):
            headers.append("Gehalt")
        if options.get("distance"):
            headers.append("Entfernung")
        if options.get("time"):
            headers.append("Fahrzeit")
        if options.get("industry"):
            headers.append("Industry")
        if options.get("published"):
            headers.append("Veröffentlicht")
        headers.append("Letzte Statusänderung")
        lines.append(", ".join(headers))
        if not applied:
            lines.append("Keine Bewerbungen im gewählten Zeitraum.")
        for number, (application_date, record) in enumerate(applied, start=1):
            interview_date = self._export_interview_date(record, histories)
            state_date = self._parse_iso_date_only(record.status_changed_at)
            parts = [
                str(number),
                application_date.isoformat(),
                record.title,
                record.company,
                self._export_location(record),
                record.status,
                interview_date.isoformat() if interview_date else "",
            ]
            if options.get("salary"):
                lo = "-" if record.min_salary_k is None else f"{record.min_salary_k:.1f} k€"
                hi = "-" if record.max_salary_k is None else f"{record.max_salary_k:.1f} k€"
                parts.append(f"{lo} bis {hi}")
            if options.get("distance"):
                parts.append("-" if record.route_distance_km is None else f"{record.route_distance_km:.1f} km")
            if options.get("time"):
                if record.route_duration_min is None:
                    duration = "-"
                else:
                    total = int(round(record.route_duration_min))
                    duration = f"{total // 60}:{total % 60:02d} h"
                parts.append(duration)
            if options.get("industry"):
                parts.append(record.company_rating or "-")
            if options.get("published"):
                parts.append(record.published_date or "-")
            parts.append(state_date.isoformat() if state_date else "")
            lines.append(", ".join(str(part).replace("\n", " ").strip() for part in parts))

        from collections import Counter
        title_counter = Counter(self._normalize_job_title_for_stats(record.title) for record in rejected)
        reason_counter = Counter()
        for record in rejected:
            for line in str(record.notes or "").splitlines():
                if line.strip().casefold().startswith("reject reason:"):
                    reason = line.split(":", 1)[1].strip()
                    if reason:
                        reason_counter[reason] += 1
        # Count applications/jobs with at least one interview, not individual interview status transitions.
        interview_count = self._count_interviewed_applications(applied, histories, start_date, end_date)
        contract_count = len({record.id for record in records if (d := self._parse_iso_date_only(record.contract_at)) and start_date <= d <= end_date})
        lines.extend([
            "",
            "Statistik",
            f"Bewerbungen gesendet: {len(applied)}",
            f"Interviews: {interview_count}",
            f"Verträge/Angebote: {contract_count}",
            f"Aussortierte Jobs: {len(rejected)}",
            "",
            "Häufigste abgelehnte Jobtitel:",
        ])
        if title_counter:
            lines.extend(f"- {title} (x{count})" for title, count in title_counter.most_common(5))
        else:
            lines.append("- Keine")
        lines.extend(["", "Häufigste Ablehnungsgründe:"])
        if reason_counter:
            lines.extend(f"- {reason} (x{count})" for reason, count in reason_counter.most_common(5))
        else:
            lines.append("- Keine erfassten Gründe")
        return "\n".join(lines).strip()

    def _build_arbeitsamt_excel_tsv(self, start_date: date, end_date: date, options: dict[str, bool]) -> str:
        records, applied, rejected, histories = self._arbeitsamt_export_data(start_date, end_date)
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows: list[list[str]] = [
            ["JobRadar Export"],
            ["Zeitraum", f"{start_date.isoformat()} bis {end_date.isoformat()}"],
            ["Erstellt", generated],
            [],
        ]
        headers = ["Nr", "Bewerbungsdatum", "Titel", "Firma", "Ort", "Aktueller Status", "Interviewdatum"]
        if options.get("salary"):
            headers.extend(["Gehalt min.", "Gehalt max."])
        if options.get("distance"):
            headers.append("Entfernung")
        if options.get("time"):
            headers.append("Fahrzeit")
        if options.get("industry"):
            headers.append("Industry")
        if options.get("published"):
            headers.append("Published")
        headers.append("Letzte Statusänderung")
        rows.append(headers)
        for number, (application_date, record) in enumerate(applied, start=1):
            interview_date = self._export_interview_date(record, histories)
            state_date = self._parse_iso_date_only(record.status_changed_at)
            row = [
                str(number), application_date.isoformat(), record.title, record.company,
                self._export_location(record), record.status,
                interview_date.isoformat() if interview_date else "",
            ]
            if options.get("salary"):
                row.extend([
                    "-" if record.min_salary_k is None else f"{record.min_salary_k:.1f} k€",
                    "-" if record.max_salary_k is None else f"{record.max_salary_k:.1f} k€",
                ])
            if options.get("distance"):
                row.append("-" if record.route_distance_km is None else f"{record.route_distance_km:.1f} km")
            if options.get("time"):
                if record.route_duration_min is None:
                    row.append("-")
                else:
                    total = int(round(record.route_duration_min))
                    row.append(f"{total // 60}:{total % 60:02d} h")
            if options.get("industry"):
                row.append(record.company_rating or "-")
            if options.get("published"):
                row.append(record.published_date or "-")
            row.append(state_date.isoformat() if state_date else "")
            rows.append(row)

        from collections import Counter
        title_counter = Counter(self._normalize_job_title_for_stats(record.title) for record in rejected)
        reason_counter = Counter()
        for record in rejected:
            for line in str(record.notes or "").splitlines():
                if line.strip().casefold().startswith("reject reason:"):
                    reason = line.split(":", 1)[1].strip()
                    if reason:
                        reason_counter[reason] += 1
        interview_count = self._count_interviewed_applications(applied, histories, start_date, end_date)
        contract_count = len({record.id for record in records if (d := self._parse_iso_date_only(record.contract_at)) and start_date <= d <= end_date})
        rows.extend([
            [],
            ["Statistik"],
            ["Bewerbungen gesendet", str(len(applied))],
            ["Interviews", str(interview_count)],
            ["Verträge/Angebote", str(contract_count)],
            ["Aussortierte Jobs", str(len(rejected))],
            [],
            ["Häufigste abgelehnte Jobtitel", "Anzahl"],
        ])
        if title_counter:
            rows.extend([[title, str(count)] for title, count in title_counter.most_common(5)])
        else:
            rows.append(["Keine", "0"])
        rows.extend([[], ["Häufigste Ablehnungsgründe", "Anzahl"]])
        if reason_counter:
            rows.extend([[reason, str(count)] for reason, count in reason_counter.most_common(5)])
        else:
            rows.append(["Keine erfassten Gründe", "0"])
        return "\n".join("\t".join(self._tsv_cell(cell) for cell in row) for row in rows)

    def export_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Arbeitsamt export")
        dialog.geometry("1000x700")
        dialog.transient(self)
        today = date.today()
        first = today.replace(day=1)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)
        top = ttk.Frame(dialog, padding=10)
        top.pack(fill=tk.X)
        start_var = tk.StringVar(value=first.isoformat())
        end_var = tk.StringVar(value=last.isoformat())
        ttk.Label(top, text="Start date (YYYY-MM-DD):").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=start_var, width=13).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(top, text="End date (YYYY-MM-DD):").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=end_var, width=13).grid(row=0, column=3, padx=(4, 12))
        option_vars = {
            "salary": tk.BooleanVar(value=False),
            "distance": tk.BooleanVar(value=False),
            "time": tk.BooleanVar(value=False),
            "industry": tk.BooleanVar(value=False),
            "published": tk.BooleanVar(value=False),
        }
        excel_var = tk.BooleanVar(value=True)
        option_frame = ttk.LabelFrame(dialog, text="Optional fields", padding=8)
        option_frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        labels = [("salary", "Salary range"), ("distance", "Distance"), ("time", "Travel time"), ("industry", "Industry"), ("published", "Published date")]
        for col, (key, label) in enumerate(labels):
            ttk.Checkbutton(option_frame, text=label, variable=option_vars[key]).grid(row=0, column=col, sticky="w", padx=(0, 16))
        text_frame = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        text_frame.pack(fill=tk.BOTH, expand=True)
        output = tk.Text(text_frame, wrap=tk.WORD, background=DARK_THEME["entry_bg"], foreground=DARK_THEME["entry_fg"], insertbackground=DARK_THEME["fg"], font=("Segoe UI", 10))
        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=output.yview)
        output.configure(yscrollcommand=scroll.set)
        output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def parse_range():
            try:
                start = datetime.strptime(start_var.get().strip(), "%Y-%m-%d").date()
                end = datetime.strptime(end_var.get().strip(), "%Y-%m-%d").date()
            except ValueError:
                messagebox.showwarning("Invalid date", "Please use YYYY-MM-DD for both dates.", parent=dialog)
                return None
            if end < start:
                messagebox.showwarning("Invalid range", "End date must not be before start date.", parent=dialog)
                return None
            return start, end

        def generate() -> None:
            parsed = parse_range()
            if parsed is None:
                return
            start, end = parsed
            text_value = self._build_arbeitsamt_export(start, end, {key: var.get() for key, var in option_vars.items()})
            output.delete("1.0", tk.END)
            output.insert("1.0", text_value)

        def copy_text() -> None:
            parsed = parse_range()
            if parsed is None:
                return
            start, end = parsed
            options = {key: var.get() for key, var in option_vars.items()}
            if excel_var.get():
                value = self._build_arbeitsamt_excel_tsv(start, end, options)
            else:
                value = output.get("1.0", tk.END).strip()
                if not value:
                    value = self._build_arbeitsamt_export(start, end, options)
            self.clipboard_clear()
            self.clipboard_append(value)
            self.status_label_var.set("Export copied to clipboard in Excel format." if excel_var.get() else "Export copied to clipboard.")

        ttk.Button(top, text="Generate Export", command=generate).grid(row=0, column=4, padx=(8, 0))
        bottom = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        bottom.pack(fill=tk.X)
        ttk.Button(bottom, text="Copy to Clipboard", command=copy_text).pack(side=tk.RIGHT)
        ttk.Checkbutton(bottom, text="Copy in Excel format", variable=excel_var).pack(side=tk.RIGHT, padx=(0, 10))
        self._center_child_window(dialog, self) if hasattr(self, "_center_child_window") else None

    @staticmethod
    def _detect_career_system(career_url: str) -> str:
        """Best-effort platform detection based on the configured career URL."""
        value = str(career_url or "").strip().casefold()
        host = urlparse(value).netloc.casefold()
        combined = f"{host} {value}"
        signatures = (
            ("personio", ("personio.", "jobs.personio", "personio.de")),
            ("greenhouse", ("greenhouse.io", "boards.greenhouse")),
            ("workday", ("myworkdayjobs.com", "workdayjobs.com", "workday.com")),
            ("smartrecruiters", ("smartrecruiters.com",)),
            ("successfactors", ("successfactors", "career5.successfactors", "career2.successfactors", "jobs.dlr.de", "recruitment.draeger.jobs", "jobs.volkswagen-group.com", "jobs.ams-osram.com")),
            ("recruitee", ("recruitee.com",)),
            ("lever", ("jobs.lever.co",)),
            ("join", ("join.com", "join.jobs")),
            ("ashby", ("ashbyhq.com", "ashby_jid")),
            ("b-ite", ("b-ite.com", "jobs-api/loader-v1")),
        )
        for system, markers in signatures:
            if any(marker in combined for marker in markers):
                return system
        return "unknown"

    def _inspect_career_system(self, career_url: str) -> tuple[str, str]:
        """Inspect redirects and page markup when the URL itself is not conclusive."""
        direct = self._detect_career_system(career_url)
        if direct != "unknown":
            return direct, ""
        try:
            response = requests.get(
                str(career_url or "").strip(),
                timeout=15,
                allow_redirects=True,
                headers={"User-Agent": "JobRadar/0.6.9.28 (+company-watch)"},
            )
            response.raise_for_status()
            raw_markup = response.text[:1000000]
            # Career widgets are often embedded in iframes or stored inside JSON/script
            # payloads with escaped slashes/Unicode escapes (common on Wix pages).
            normalized_markup = html.unescape(raw_markup)
            normalized_markup = normalized_markup.replace("\\/", "/")
            normalized_markup = normalized_markup.replace("\\u002f", "/").replace("\\u002F", "/")
            normalized_markup = normalized_markup.replace("\\u003a", ":").replace("\\u003A", ":")
            normalized_markup = unquote(normalized_markup)
            sample = f"{response.url} {normalized_markup}".casefold()
            markers = (
                ("personio", ("personio", "personio.de/xml", "jobs.personio")),
                ("greenhouse", ("greenhouse.io", "boards.greenhouse", "greenhouse-job-board")),
                ("workday", ("myworkdayjobs", "workdayjobs", "wd5.myworkday", "workday")),
                ("smartrecruiters", ("smartrecruiters.com", "jobs.smartrecruiters")),
                ("successfactors", ("successfactors", "sap-successfactors")),
                ("recruitee", ("recruitee.com", "recruitee-careers")),
                ("lever", ("jobs.lever.co", "lever.co")),
                ("join", ("join.com", "join.jobs", "join-widget")),
                ("ashby", ("ashbyhq.com", "ashby_jid", "posting-api/job-board")),
                ("b-ite", ("b-ite.com", "data-bite-jobs-api-listing", "jobs-api/loader-v1")),
            )
            for system, values in markers:
                if any(value in sample for value in values):
                    return system, ""
            return "unknown", ""
        except Exception as exc:
            return "unknown", str(exc)

    def _company_watchlist_entry_dialog(
        self,
        parent: tk.Misc,
        entry: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        entry = dict(entry or {})
        dialog = tk.Toplevel(parent)
        dialog.title("Edit company watchlist entry" if entry else "Add company watchlist entry")
        dialog.geometry("760x570")
        dialog.configure(bg=DARK_THEME["bg"])
        dialog.transient(parent)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(7, weight=1)

        name_var = tk.StringVar(value=str(entry.get("company_name") or ""))
        homepage_var = tk.StringVar(value=str(entry.get("homepage_url") or ""))
        career_var = tk.StringVar(value=str(entry.get("career_url") or ""))
        system_var = tk.StringVar(value=str(entry.get("career_system") or "unknown"))
        priority_var = tk.IntVar(value=int(entry.get("priority") or 3))
        enabled_var = tk.BooleanVar(value=bool(entry.get("enabled", True)))
        locations_var = tk.StringVar(value=str(entry.get("locations") or ""))

        widgets = [
            ("Company name:*", ttk.Entry(body, textvariable=name_var)),
            ("Homepage URL:", ttk.Entry(body, textvariable=homepage_var)),
            ("Career URL:*", ttk.Entry(body, textvariable=career_var)),
            ("Career system:", ttk.Combobox(
                body,
                textvariable=system_var,
                values=["unknown", "personio", "greenhouse", "workday", "smartrecruiters", "successfactors", "recruitee", "lever", "join", "ashby", "b-ite"],
            )),
            ("Priority:", ttk.Spinbox(body, from_=1, to=99, textvariable=priority_var, width=8)),
            ("Locations:", ttk.Entry(body, textvariable=locations_var)),
        ]
        for row, (label, widget) in enumerate(widgets):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=5)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Checkbutton(body, text="Enabled", variable=enabled_var).grid(row=6, column=1, sticky="w", pady=5)
        ttk.Label(body, text="Notes:").grid(row=7, column=0, sticky="nw", padx=(0, 10), pady=5)
        notes_text = tk.Text(
            body,
            height=10,
            wrap=tk.WORD,
            bg=DARK_THEME["entry_bg"],
            fg=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["entry_fg"],
        )
        notes_text.grid(row=7, column=1, sticky="nsew", pady=5)
        notes_text.insert("1.0", str(entry.get("notes") or ""))
        result: dict[str, object] = {}

        def detect() -> None:
            career_url = career_var.get().strip()
            if not career_url:
                messagebox.showinfo("Detect career system", "Enter a career URL first.", parent=dialog)
                return
            system_var.set("detecting...")
            dialog.update_idletasks()

            def worker() -> None:
                system, error = self._inspect_career_system(career_url)
                def finish() -> None:
                    system_var.set(system)
                    if error:
                        messagebox.showwarning("Detect career system", error, parent=dialog)
                self.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(body, text="Detect system", command=detect).grid(row=3, column=2, padx=(8, 0), pady=5)

        def save() -> None:
            name = name_var.get().strip()
            career_url = career_var.get().strip()
            if not name or not career_url:
                messagebox.showwarning("Company watchlist", "Company name and career URL are required.", parent=dialog)
                return
            try:
                priority = max(1, int(priority_var.get()))
            except (TypeError, ValueError):
                messagebox.showwarning("Company watchlist", "Priority must be a positive integer.", parent=dialog)
                return
            result.update({
                "company_name": name,
                "homepage_url": homepage_var.get().strip(),
                "career_url": career_url,
                "career_system": system_var.get().strip() or "unknown",
                "priority": priority,
                "locations": locations_var.get().strip(),
                "notes": notes_text.get("1.0", tk.END).strip(),
                "enabled": enabled_var.get(),
            })
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()
        return result or None

    def _import_company_watchlist_csv(self, parent: tk.Misc, reload_callback=None) -> None:
        path = filedialog.askopenfilename(
            parent=parent,
            title="Import company watchlist",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        imported = updated = skipped = 0
        errors: list[str] = []
        try:
            current = self.db.list_company_watchlist()
            existing_by_key = {
                (
                    str(item.get("company_name") or "").strip().casefold(),
                    str(item.get("career_url") or "").strip().casefold(),
                ): item
                for item in current
            }
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                headers = {str(name or "").strip().casefold(): name for name in (reader.fieldnames or [])}

                def field(row, *names, default=""):
                    for name in names:
                        actual = headers.get(name.casefold())
                        if actual is not None and str(row.get(actual) or "").strip():
                            return str(row.get(actual) or "").strip()
                    return default

                if not any(key in headers for key in ("company_name", "company")):
                    raise ValueError("Required CSV column missing: company_name")
                if not any(key in headers for key in ("career_url", "jobs_url", "job_url")):
                    raise ValueError("Required CSV column missing: career_url")

                for line_no, row in enumerate(reader, start=2):
                    try:
                        name = field(row, "company_name", "company")
                        career_url = field(row, "career_url", "jobs_url", "job_url")
                        if not name or not career_url:
                            skipped += 1
                            continue
                        priority_text = field(row, "priority", "priority_tier_1_best", default="3")
                        try:
                            priority = max(1, int(float(priority_text)))
                        except ValueError:
                            priority = 3
                        system = field(row, "career_system", "ats_type") or self._detect_career_system(career_url)
                        enabled_text = field(row, "enabled", default="true").casefold()
                        enabled = enabled_text not in {"0", "false", "no", "off", "disabled"}
                        key = (name.casefold(), career_url.casefold())
                        existing = existing_by_key.get(key)
                        saved_id = self.db.save_company_watchlist_entry(
                            entry_id=int(existing["id"]) if existing else None,
                            company_name=name,
                            homepage_url=field(row, "homepage_url", "company_url", "website", "start_url"),
                            career_url=career_url,
                            career_system=system,
                            priority=priority,
                            locations=field(row, "locations", "location"),
                            notes=field(row, "notes"),
                            enabled=enabled,
                        )
                        existing_by_key[key] = {"id": saved_id}
                        if existing:
                            updated += 1
                        else:
                            imported += 1
                    except Exception as exc:
                        errors.append(f"Line {line_no}: {exc}")
        except Exception as exc:
            messagebox.showerror("Company watchlist import", str(exc), parent=parent)
            return
        if callable(reload_callback):
            reload_callback()
        message = f"Imported: {imported}\nUpdated: {updated}\nSkipped: {skipped}"
        if errors:
            message += f"\nErrors: {len(errors)}\n\n" + "\n".join(errors[:8])
        messagebox.showinfo("Company watchlist import", message, parent=parent)

    def _apply_company_watch_screening_result(
        self,
        result: dict,
        model_config: AiModelConfig,
        auto_reject_red: bool,
    ) -> tuple[int, int]:
        """Apply one automatic Company Watch screening batch on the GUI thread."""
        items = result.get("items", []) if isinstance(result, dict) else []
        if not isinstance(items, list):
            items = []
        raw_response = str(result.get("raw_response") or "") if isinstance(result, dict) else ""

        # Preserve the structured rejection fields so automatic rejection can use
        # the same configured reasons as the ordinary interactive workflow.
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("iid") or "").strip()
            if not iid:
                continue
            score = self._coerce_ai_screening_score(item.get("score"))
            decision = self._screening_decision_from_score(score) or str(item.get("decision") or "").strip().lower()
            self.temp_ai_screening_scores_by_iid[iid] = score if score is not None else 0
            self.temp_ai_screening_details_by_iid[iid] = {
                "score": score,
                "decision": decision,
                "reason": str(item.get("reason") or item.get("exclusion_reason") or "").strip(),
                "reject_reason": str(item.get("reject_reason") or item.get("exclusion_reason") or "").strip(),
                "rejection_tags": [str(tag).strip() for tag in item.get("rejection_tags", []) if str(tag).strip()] if isinstance(item.get("rejection_tags"), list) else [],
                "confidence": str(item.get("confidence") or "").strip(),
                "hard_exclusion": bool(item.get("hard_exclusion")),
                "interesting_signals": [str(signal).strip() for signal in item.get("interesting_signals", []) if str(signal).strip()] if isinstance(item.get("interesting_signals"), list) else [],
            }

        marked = self._apply_ai_screening_items(
            items,
            apply_summaries=True,
            chat_window={"model": model_config, "raw_response": raw_response},
        )
        rejected = 0
        if auto_reject_red:
            reasons_by_iid: dict[str, str] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("iid") or "").strip()
                score = self._coerce_ai_screening_score(item.get("score"))
                decision = self._screening_decision_from_score(score) or str(item.get("decision") or "").strip().lower()
                if not iid or decision != "unlikely" or self._get_display_job(iid) is None:
                    continue
                suggested = self._suggested_reject_reasons_for_iids([iid])
                reasons_by_iid[iid] = suggested[0] if suggested else "wrong domain"
            if reasons_by_iid:
                self._reject_iids_with_reasons(reasons_by_iid)
                rejected = len(reasons_by_iid)
                self.refresh_jobs_preserving_detail_tabs()
        else:
            self.refresh_jobs_preserving_detail_tabs()
        return marked, rejected

    def open_company_watch_window(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Company Watch")
        dialog.geometry("820x570")
        dialog.configure(bg=DARK_THEME["bg"])
        dialog.transient(self)

        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        entries = self.db.list_company_watchlist()
        priorities = sorted({int(item.get("priority") or 3) for item in entries})
        priority_frame = ttk.LabelFrame(outer, text="Priorities", padding=8)
        priority_frame.pack(fill=tk.X)
        all_var = tk.BooleanVar(value=True)
        priority_vars = {priority: tk.BooleanVar(value=True) for priority in priorities}

        def toggle_all() -> None:
            value = all_var.get()
            for var in priority_vars.values():
                var.set(value)

        ttk.Checkbutton(
            priority_frame,
            text="Check all/none",
            variable=all_var,
            command=toggle_all,
        ).pack(side=tk.LEFT, padx=(0, 12))
        for priority, var in priority_vars.items():
            ttk.Checkbutton(priority_frame, text=f"Priority {priority}", variable=var).pack(side=tk.LEFT, padx=(0, 10))

        country_frame = ttk.Frame(outer)
        country_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(country_frame, text="Country filter:").pack(side=tk.LEFT)
        country_var = tk.StringVar(value=self.db.get_setting("company_watch.country_filter", "Germany"))
        country_entry = ttk.Entry(country_frame, textvariable=country_var, width=24)
        country_entry.pack(side=tk.LEFT, padx=(6, 8))
        ttk.Label(
            country_frame,
            text="German or English names and common country codes are accepted. Empty = all countries.",
        ).pack(side=tk.LEFT)

        automation_frame = ttk.LabelFrame(outer, text="Automatic processing", padding=8)
        automation_frame.pack(fill=tk.X, pady=(8, 0))
        auto_screen_var = tk.BooleanVar(value=self.db.get_setting("company_watch.auto_screen", "1") != "0")
        auto_reject_var = tk.BooleanVar(value=self.db.get_setting("company_watch.auto_reject_red", "1") != "0")
        orange_style = ttk.Style(dialog)
        orange_style.configure(
            "CompanyWatchOrange.TCheckbutton",
            background=DARK_THEME["bg"],
            foreground="#ff9f43",
            font=("Segoe UI", 9, "bold"),
        )
        orange_style.map(
            "CompanyWatchOrange.TCheckbutton",
            background=[("active", DARK_THEME["bg"]), ("selected", DARK_THEME["bg"])],
            foreground=[("disabled", DARK_THEME["muted_fg"]), ("active", "#ffb76b")],
        )
        auto_screen_check = ttk.Checkbutton(
            automation_frame,
            text="Automatically shallow-screen new jobs",
            variable=auto_screen_var,
            style="CompanyWatchOrange.TCheckbutton",
        )
        auto_screen_check.pack(side=tk.LEFT, padx=(0, 18))
        auto_reject_check = ttk.Checkbutton(
            automation_frame,
            text="Automatically reject red screening results",
            variable=auto_reject_var,
            style="CompanyWatchOrange.TCheckbutton",
        )
        auto_reject_check.pack(side=tk.LEFT)
        ttk.Label(
            automation_frame,
            text=f"Batch size: {int(self.user_config.get('ai_screening_batch_size', 10) or 10)} (Settings → AI)",
            foreground=DARK_THEME["muted_fg"],
        ).pack(side=tk.RIGHT)

        def sync_auto_reject_state(*_args) -> None:
            auto_reject_check.configure(state=tk.NORMAL if auto_screen_var.get() else tk.DISABLED)

        auto_screen_var.trace_add("write", sync_auto_reject_state)
        sync_auto_reject_state()

        status_var = tk.StringVar(
            value=f"{len(entries)} companies configured. Supported adapters: Greenhouse, Personio, SmartRecruiters, SuccessFactors, Workday, Recruitee and JOIN."
        )
        ttk.Label(outer, textvariable=status_var, wraplength=760).pack(fill=tk.X, pady=(8, 6))

        # Reserve the bottom action row before packing the expanding log. This
        # keeps Start/Close visible even at the default window height.
        buttons = ttk.Frame(outer)
        buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

        log = tk.Text(
            outer,
            height=22,
            wrap=tk.WORD,
            bg=DARK_THEME["entry_bg"],
            fg=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["entry_fg"],
        )
        log.pack(fill=tk.BOTH, expand=True)
        log.configure(state=tk.DISABLED)

        def dialog_alive() -> bool:
            try:
                return bool(dialog.winfo_exists())
            except tk.TclError:
                return False

        def append(text: str) -> None:
            if not dialog_alive():
                return
            try:
                log.configure(state=tk.NORMAL)
                log.insert(tk.END, text + "\n")
                log.see(tk.END)
                log.configure(state=tk.DISABLED)
            except tk.TclError:
                # The search may deliberately continue after this window was closed.
                return

        def set_watch_status(text: str) -> None:
            if not dialog_alive():
                return
            try:
                status_var.set(text)
            except tk.TclError:
                return

        def start() -> None:
            chosen = {priority for priority, var in priority_vars.items() if var.get()}
            country_filter = country_var.get().strip()
            auto_screen = bool(auto_screen_var.get())
            auto_reject_red = bool(auto_reject_var.get()) and auto_screen
            self.db.set_setting("company_watch.country_filter", country_filter)
            self.db.set_setting("company_watch.auto_screen", "1" if auto_screen else "0")
            self.db.set_setting("company_watch.auto_reject_red", "1" if auto_reject_red else "0")
            screening_model = self._default_ai_model_config("screening") if auto_screen else None
            if auto_screen and screening_model is None:
                messagebox.showwarning(
                    "Company Watch",
                    "Automatic shallow screening is enabled, but no default screening model is configured in Settings → AI.",
                    parent=dialog,
                )
                return
            try:
                screening_batch_size = max(1, min(50, int(self.user_config.get("ai_screening_batch_size", 10) or 10)))
            except (TypeError, ValueError):
                screening_batch_size = 10
            selected = [
                item
                for item in self.db.list_company_watchlist()
                if bool(item.get("enabled")) and int(item.get("priority") or 3) in chosen
            ]
            selected.sort(key=lambda item: (int(item.get("priority") or 3), str(item.get("company_name") or "").casefold()))
            if not selected:
                messagebox.showinfo("Company Watch", "No enabled companies match the selected priorities.", parent=dialog)
                return
            start_button.configure(state=tk.DISABLED)
            append(f"Starting company-page search for {len(selected)} companies...")
            set_watch_status(f"Checking 0 / {len(selected)} companies...")

            def process_company(item: dict[str, object]):
                configured = str(item.get("career_system") or "unknown").casefold()
                system = configured
                detection_error = ""
                if system not in self.company_watch_adapters.supported_systems:
                    system, detection_error = self._inspect_career_system(str(item.get("career_url") or ""))
                if system not in self.company_watch_adapters.supported_systems:
                    status = (
                        "check failed" if detection_error
                        else ("detected - adapter not implemented" if system != "unknown" else "unknown platform - skipped")
                    )
                    return item, system, status, detection_error, []
                try:
                    fetch_entry = dict(item)
                    fetch_entry["_country_filter"] = country_filter
                    result = self.company_watch_adapters.fetch(fetch_entry, system)
                    total_count = len(result.candidates)
                    candidates = [
                        candidate for candidate in result.candidates
                        if candidate_matches_country(candidate, country_filter)
                    ]
                    if country_filter:
                        status = f"ok - {len(candidates)} matching / {total_count} open job(s)"
                    else:
                        status = f"ok - {total_count} open job(s)"
                    return item, system, status, "", candidates
                except Exception as exc:
                    return item, system, "adapter failed", str(exc), []

            def worker() -> None:
                counts: dict[str, int] = {}
                retrieved_total = 0
                completed = 0
                screening_queue: Queue[list[dict] | None] = Queue()
                screening_buffer: list[dict] = []
                screening_totals = {"screened": 0, "rejected": 0, "failed_batches": 0}

                def publish_company_result(candidates: list[JobCandidate], line: str, done: int, remaining: str) -> None:
                    # Publish each completed company's jobs immediately. Only rows
                    # actually accepted as new by the central import pipeline are
                    # queued for automatic screening.
                    added_iids = self._show_search_results(candidates, append_pending=True) if candidates else []
                    if auto_screen:
                        for iid in added_iids:
                            job = self._get_display_job(iid)
                            if job is not None:
                                screening_buffer.append(self._ai_screening_row(job))
                        while len(screening_buffer) >= screening_batch_size:
                            screening_queue.put(screening_buffer[:screening_batch_size])
                            del screening_buffer[:screening_batch_size]
                    append(line)
                    suffix = f" Still running: {remaining}" if remaining else ""
                    set_watch_status(f"Checking {done} / {len(selected)} companies...{suffix}")

                def screening_worker() -> None:
                    if not auto_screen or screening_model is None:
                        return
                    while True:
                        batch = screening_queue.get()
                        if batch is None:
                            break
                        iids = [str(row.get("iid") or "") for row in batch if str(row.get("iid") or "")]
                        task_id = self._agent_call_on_ui_thread(
                            lambda iids=list(iids): self._start_background_ai_task(
                                "screening", iids, f"Company Watch screening {len(iids)} job(s)"
                            )
                        )
                        try:
                            result = self.ai_service.screen_jobs_from_table(
                                batch, screening_model, self._ai_user_config_with_industries()
                            )
                            marked, rejected = self._agent_call_on_ui_thread(
                                lambda result=result: self._apply_company_watch_screening_result(
                                    result, screening_model, auto_reject_red
                                )
                            )
                            screening_totals["screened"] += int(marked or 0)
                            screening_totals["rejected"] += int(rejected or 0)
                            self._agent_call_on_ui_thread(lambda task_id=task_id: self._finish_background_ai_task(task_id, True))
                            self.after(0, lambda m=marked, r=rejected: append(
                                f"AI screening batch complete: {m} marked, {r} red job(s) automatically rejected."
                            ))
                        except Exception as exc:
                            screening_totals["failed_batches"] += 1
                            self._agent_call_on_ui_thread(lambda task_id=task_id: self._finish_background_ai_task(task_id, False))
                            self.after(0, lambda err=str(exc): append(f"AI screening batch failed: {err}"))

                screening_thread = threading.Thread(target=screening_worker, daemon=True)
                screening_thread.start()

                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {executor.submit(process_company, item): item for item in selected}
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            item, system, status, error, candidates = future.result()
                        except Exception as exc:
                            system, status, error, candidates = "unknown", "check failed", str(exc), []
                        self.db.update_company_watchlist_detection(int(item["id"]), system, status, error)
                        retrieved_total += len(candidates)
                        counts[system] = counts.get(system, 0) + 1
                        completed += 1
                        suffix = f" — {error}" if error else ""
                        line = f"[{completed}/{len(selected)}] {item.get('company_name')}: {system} — {status}{suffix}"
                        remaining_names = [
                            str(pending_item.get("company_name") or "")
                            for pending_future, pending_item in futures.items()
                            if not pending_future.done()
                        ]
                        remaining_text = ", ".join(remaining_names[:4])
                        if len(remaining_names) > 4:
                            remaining_text += f" (+{len(remaining_names) - 4} more)"
                        self.after(
                            0,
                            lambda batch=list(candidates), text=line, done=completed, remaining=remaining_text: publish_company_result(batch, text, done, remaining),
                        )

                def finish_publishing_and_signal() -> None:
                    if auto_screen and screening_buffer:
                        screening_queue.put(list(screening_buffer))
                        screening_buffer.clear()
                    screening_queue.put(None)

                self._agent_call_on_ui_thread(finish_publishing_and_signal)
                screening_thread.join()

                def finish() -> None:
                    summary = ", ".join(f"{name}: {count}" for name, count in sorted(counts.items()))
                    ai_suffix = ""
                    if auto_screen:
                        ai_suffix = (
                            f" AI screened: {screening_totals['screened']}; "
                            f"red auto-rejected: {screening_totals['rejected']}; "
                            f"failed AI batches: {screening_totals['failed_batches']}."
                        )
                    set_watch_status(
                        f"Company Watch complete. Retrieved {retrieved_total} open job(s). " + summary + ai_suffix
                    )
                    append(
                        f"Complete: {retrieved_total} open jobs retrieved. Known jobs were skipped in the main list."
                        + ai_suffix
                    )
                    if dialog_alive():
                        try:
                            start_button.configure(state=tk.NORMAL)
                        except tk.TclError:
                            pass

                self.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)
        start_button = ttk.Button(buttons, text="Start", command=start)
        start_button.pack(side=tk.RIGHT, padx=(0, 8))

    def settings_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Settings")
        dialog.geometry("1080x720")
        dialog.configure(bg=DARK_THEME["bg"])
        dialog.transient(self)
        dialog.grab_set()

        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        geo_tab = ttk.Frame(notebook, padding=10)
        status_tab = ttk.Frame(notebook, padding=10)
        industry_tab = ttk.Frame(notebook, padding=10)
        companies_tab = ttk.Frame(notebook, padding=10)
        blacklist_tab = ttk.Frame(notebook, padding=10)
        rejection_tab = ttk.Frame(notebook, padding=10)
        watchlist_tab = ttk.Frame(notebook, padding=10)
        source_tab = ttk.Frame(notebook, padding=10)
        ai_tab = ttk.Frame(notebook, padding=10)
        notebook.add(geo_tab, text="Geo / Routing")
        notebook.add(status_tab, text="States")
        notebook.add(industry_tab, text="Known industries")
        notebook.add(companies_tab, text="Known companies")
        notebook.add(blacklist_tab, text="Blacklist")
        notebook.add(rejection_tab, text="Rejection reasons")
        notebook.add(watchlist_tab, text="Company watchlist")
        notebook.add(source_tab, text="Manual sources")
        notebook.add(ai_tab, text="AI")

        # Company watchlist editor
        ttk.Label(
            watchlist_tab,
            text="Companies whose own career pages should be checked directly. Search support is added per career-system adapter.",
        ).pack(anchor=tk.W, pady=(0, 6))
        watch_frame = ttk.Frame(watchlist_tab)
        watch_frame.pack(fill=tk.BOTH, expand=True)
        # Keep the vertical scrollbar inside the visible client area even when
        # the Treeview columns request more width than the Settings window.
        watch_frame.columnconfigure(0, weight=1)
        watch_frame.rowconfigure(0, weight=1)
        watch_columns = ("priority", "enabled", "company", "locations", "system", "status", "career_url")
        watch_tree = ttk.Treeview(
            watch_frame,
            columns=watch_columns,
            show="headings",
            selectmode="extended",
        )
        watch_specs = (
            ("priority", "Priority", 65),
            ("enabled", "Enabled", 65),
            ("company", "Company", 210),
            ("locations", "Locations", 190),
            ("system", "Career system", 110),
            ("status", "Status", 190),
            ("career_url", "Career URL", 310),
        )
        watch_titles = {column: title for column, title, _width in watch_specs}
        watch_sort_column = "priority"
        watch_sort_reverse = False

        def watch_sort_key(iid: str, column: str):
            item = watch_rows.get(iid, {})
            if column == "priority":
                try:
                    return int(item.get("priority", 0))
                except (TypeError, ValueError):
                    return 0
            if column == "enabled":
                return int(bool(item.get("enabled")))
            key_map = {
                "company": "company_name",
                "locations": "locations",
                "system": "career_system",
                "status": "last_status",
                "career_url": "career_url",
            }
            return str(item.get(key_map.get(column, column), "") or "").casefold()

        def update_watch_headings() -> None:
            for column, title in watch_titles.items():
                marker = " ▼" if watch_sort_reverse else " ▲"
                text = title + marker if column == watch_sort_column else title
                watch_tree.heading(column, text=text, command=lambda c=column: sort_watchlist(c))

        def sort_watchlist(column: str) -> None:
            nonlocal watch_sort_column, watch_sort_reverse
            if watch_sort_column == column:
                watch_sort_reverse = not watch_sort_reverse
            else:
                watch_sort_column = column
                watch_sort_reverse = False
            ordered = sorted(
                watch_tree.get_children(""),
                key=lambda iid: watch_sort_key(iid, column),
                reverse=watch_sort_reverse,
            )
            for index, iid in enumerate(ordered):
                watch_tree.move(iid, "", index)
            update_watch_headings()

        for column, title, width in watch_specs:
            watch_tree.column(
                column,
                width=width,
                stretch=(column in {"company", "locations", "status", "career_url"}),
            )
        watch_y = ttk.Scrollbar(watch_frame, orient=tk.VERTICAL, command=watch_tree.yview)
        watch_x = ttk.Scrollbar(watch_frame, orient=tk.HORIZONTAL, command=watch_tree.xview)
        watch_tree.configure(yscrollcommand=watch_y.set, xscrollcommand=watch_x.set)
        watch_tree.grid(row=0, column=0, sticky="nsew")
        watch_y.grid(row=0, column=1, sticky="ns")
        watch_x.grid(row=1, column=0, sticky="ew")
        watch_rows: dict[str, dict[str, object]] = {}

        def on_watch_mousewheel(event) -> str:
            if getattr(event, "delta", 0):
                direction = -1 if event.delta > 0 else 1
            else:
                direction = -1 if getattr(event, "num", 0) == 4 else 1
            watch_tree.yview_scroll(direction * 3, "units")
            return "break"

        watch_tree.bind("<MouseWheel>", on_watch_mousewheel)
        watch_tree.bind("<Button-4>", on_watch_mousewheel)
        watch_tree.bind("<Button-5>", on_watch_mousewheel)

        def reload_watchlist() -> None:
            try:
                if not dialog.winfo_exists() or not watch_tree.winfo_exists():
                    return
                selected = set(watch_tree.selection())
                watch_tree.delete(*watch_tree.get_children())
            except tk.TclError:
                # Detection/import workers may finish after Settings was closed.
                return
            watch_rows.clear()
            # Adapter support can change after an application update. Refresh stale
            # local status text without forcing another HTTP detection request.
            current_entries = self.db.list_company_watchlist()
            for stale in current_entries:
                system = str(stale.get("career_system") or "unknown").casefold()
                status = str(stale.get("last_status") or "")
                if system in self.company_watch_adapters.supported_systems and status == "detected - adapter not implemented":
                    self.db.update_company_watchlist_detection(int(stale["id"]), system, "supported", "")
            for item in self.db.list_company_watchlist():
                iid = str(item["id"])
                watch_rows[iid] = item
                watch_tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        item.get("priority", 3),
                        "Yes" if item.get("enabled") else "No",
                        item.get("company_name", ""),
                        item.get("locations", ""),
                        item.get("career_system", "unknown"),
                        item.get("last_status", ""),
                        item.get("career_url", ""),
                    ),
                )
            ordered = sorted(
                watch_tree.get_children(""),
                key=lambda iid: watch_sort_key(iid, watch_sort_column),
                reverse=watch_sort_reverse,
            )
            for index, iid in enumerate(ordered):
                watch_tree.move(iid, "", index)
            still_present = [iid for iid in selected if watch_tree.exists(iid)]
            if still_present:
                watch_tree.selection_set(still_present)
            update_watch_headings()

        def add_watchlist() -> None:
            values = self._company_watchlist_entry_dialog(dialog)
            if values:
                self.db.save_company_watchlist_entry(**values)
                reload_watchlist()

        def edit_watchlist() -> None:
            selected = watch_tree.selection()
            if len(selected) != 1:
                messagebox.showinfo(
                    "Company watchlist",
                    "Select exactly one company to edit.",
                    parent=dialog,
                )
                return
            item = self.db.get_company_watchlist_entry(int(selected[0]))
            values = self._company_watchlist_entry_dialog(dialog, item)
            if values:
                self.db.save_company_watchlist_entry(entry_id=int(selected[0]), **values)
                reload_watchlist()

        def open_watchlist_url() -> None:
            selected = watch_tree.selection()
            if len(selected) != 1:
                messagebox.showinfo(
                    "Company watchlist",
                    "Select exactly one company whose URL should be opened.",
                    parent=dialog,
                )
                return
            item = watch_rows.get(selected[0]) or self.db.get_company_watchlist_entry(int(selected[0]))
            url = str((item or {}).get("career_url") or (item or {}).get("homepage_url") or "").strip()
            if not url:
                messagebox.showinfo("Company watchlist", "No URL is stored for this entry.", parent=dialog)
                return
            webbrowser.open(url)

        def copy_watchlist_url() -> None:
            selected = watch_tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("Company watchlist", "Select exactly one company whose URL should be copied.", parent=dialog)
                return
            item = watch_rows.get(selected[0]) or self.db.get_company_watchlist_entry(int(selected[0]))
            url = str((item or {}).get("career_url") or (item or {}).get("homepage_url") or "").strip()
            if not url:
                messagebox.showinfo("Company watchlist", "No URL is stored for this entry.", parent=dialog)
                return
            self.clipboard_clear()
            self.clipboard_append(url)

        def diagnose_watchlist_source() -> None:
            selected = list(watch_tree.selection())
            if len(selected) != 1:
                messagebox.showinfo(
                    "Company watchlist",
                    "Select exactly one company to diagnose.",
                    parent=dialog,
                )
                return
            item = watch_rows.get(selected[0]) or self.db.get_company_watchlist_entry(int(selected[0]))
            if not item:
                return
            safe_company = re.sub(r"[^A-Za-z0-9._-]+", "_", str(item.get("company_name") or "company")).strip("_") or "company"
            output_path = filedialog.asksaveasfilename(
                parent=dialog,
                title="Save career source diagnostic",
                defaultextension=".json",
                initialfile=f"career_source_diagnostic_{safe_company}.json",
                filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
            )
            if not output_path:
                return

            self.status_label_var.set(f"Diagnosing career source: {item.get('company_name', '')}...")

            def worker() -> None:
                try:
                    report = build_career_source_diagnostic(
                        item,
                        str(item.get("career_system") or "unknown"),
                        self.company_watch_adapters,
                    )
                    Path(output_path).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    def show_error() -> None:
                        self.status_label_var.set("Career source diagnostic failed.")
                        messagebox.showerror("Career source diagnostic", str(exc), parent=self)
                    self.after(0, show_error)
                    return

                def show_success() -> None:
                    self.status_label_var.set("Career source diagnostic saved.")
                    messagebox.showinfo(
                        "Career source diagnostic",
                        f"Diagnostic saved to:\n{output_path}",
                        parent=self,
                    )
                self.after(0, show_success)

            threading.Thread(target=worker, daemon=True).start()

        def toggle_watchlist_enabled() -> None:
            selected = list(watch_tree.selection())
            if not selected:
                return
            # Mixed selections are enabled; otherwise all selected entries are toggled off.
            target_enabled = not all(bool(watch_rows.get(iid, {}).get("enabled")) for iid in selected)
            self.db.set_company_watchlist_enabled([int(iid) for iid in selected], target_enabled)
            reload_watchlist()

        def delete_watchlist() -> None:
            selected = [int(value) for value in watch_tree.selection()]
            if not selected:
                return
            noun = "entry" if len(selected) == 1 else "entries"
            if messagebox.askyesno(
                "Delete companies",
                f"Really delete {len(selected)} selected watchlist {noun}?",
                parent=dialog,
            ):
                self.db.delete_company_watchlist_entries(selected)
                reload_watchlist()

        detection_status_var = tk.StringVar(value="Detection: idle")
        detection_running = False

        def detect_watchlist() -> None:
            nonlocal detection_running
            if detection_running:
                return
            selected = list(watch_tree.selection() or watch_tree.get_children())
            if not selected:
                return

            detection_running = True
            total = len(selected)
            detection_status_var.set(f"Detection: 0/{total}")

            def set_detection_progress(done: int, company_name: str = "") -> None:
                if not dialog.winfo_exists():
                    return
                suffix = f" — {company_name}" if company_name else ""
                detection_status_var.set(f"Detection: {done}/{total}{suffix}")

            def finish_detection() -> None:
                nonlocal detection_running
                detection_running = False
                if not dialog.winfo_exists():
                    return
                detection_status_var.set(f"Detection complete: {total}/{total}")
                reload_watchlist()

            def worker() -> None:
                for index, iid in enumerate(selected, start=1):
                    item = self.db.get_company_watchlist_entry(int(iid))
                    company_name = str((item or {}).get("company_name") or "")
                    self.after(0, lambda i=index - 1, name=company_name: set_detection_progress(i, name))
                    if item:
                        system, error = self._inspect_career_system(str(item.get("career_url") or ""))
                        status = (
                            "supported"
                            if system in self.company_watch_adapters.supported_systems
                            else ("detected - adapter not implemented" if system != "unknown" else ("check failed" if error else "unknown platform - skipped"))
                        )
                        self.db.update_company_watchlist_detection(int(iid), system, status, error)
                    self.after(0, lambda i=index, name=company_name: set_detection_progress(i, name))
                self.after(0, finish_detection)

            threading.Thread(target=worker, daemon=True).start()

        watch_menu = tk.Menu(watch_tree, tearoff=False)

        def show_watch_context_menu(event) -> None:
            iid = watch_tree.identify_row(event.y)
            if not iid:
                return
            if iid not in watch_tree.selection():
                watch_tree.selection_set(iid)
            watch_tree.focus(iid)
            selected = list(watch_tree.selection())
            watch_menu.delete(0, tk.END)
            watch_menu.add_command(label="Edit", command=edit_watchlist, state=(tk.NORMAL if len(selected) == 1 else tk.DISABLED))
            watch_menu.add_command(label="Open URL", command=open_watchlist_url, state=(tk.NORMAL if len(selected) == 1 else tk.DISABLED))
            watch_menu.add_command(label="Copy URL", command=copy_watchlist_url, state=(tk.NORMAL if len(selected) == 1 else tk.DISABLED))
            watch_menu.add_command(label="Detect system", command=detect_watchlist)
            watch_menu.add_command(label="Diagnose career source", command=diagnose_watchlist_source, state=(tk.NORMAL if len(selected) == 1 else tk.DISABLED))
            enabled_label = "Disable" if selected and all(bool(watch_rows.get(value, {}).get("enabled")) for value in selected) else "Enable"
            watch_menu.add_command(label=enabled_label, command=toggle_watchlist_enabled)
            watch_menu.add_separator()
            watch_menu.add_command(label="Delete", command=delete_watchlist)
            watch_menu.tk_popup(event.x_root, event.y_root)

        watch_tree.bind("<Button-3>", show_watch_context_menu)

        watch_buttons = ttk.Frame(watchlist_tab)
        watch_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            watch_buttons,
            text="Import CSV",
            command=lambda: self._import_company_watchlist_csv(dialog, reload_watchlist),
        ).pack(side=tk.LEFT)
        ttk.Button(watch_buttons, text="Add", command=add_watchlist).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(watch_buttons, text="Edit", command=edit_watchlist).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(watch_buttons, text="Delete selected", command=delete_watchlist).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(watch_buttons, text="Detect system", command=detect_watchlist).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(watch_buttons, text="Diagnose selected", command=diagnose_watchlist_source).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(watch_buttons, textvariable=detection_status_var, anchor=tk.W).pack(side=tk.LEFT, padx=(10, 0))
        watch_tree.bind("<Double-1>", lambda _event: edit_watchlist())
        reload_watchlist()

        settings = self.db.get_geo_settings()
        home_var = tk.StringVar(value=str(settings.get("home_address", "")))
        geocoding_provider_var = tk.StringVar(value=str(settings.get("geocoding_provider", "nominatim")))
        routing_provider_var = tk.StringVar(value=str(settings.get("routing_provider", settings.get("provider", "direct"))))
        osrm_delay_var = tk.StringVar(value=str(settings.get("osrm_delay", "1.1")))
        ors_delay_var = tk.StringVar(value=str(settings.get("ors_delay", "1.6")))
        ors_env_var = tk.StringVar(value=str(settings.get("ors_env_var", "ORS_API_KEY")))

        ttk.Label(geo_tab, text="Home address:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(geo_tab, textvariable=home_var, width=72).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(geo_tab, text="Geocoding provider:").grid(row=1, column=0, sticky="w", pady=(12, 4))
        ttk.Combobox(
            geo_tab,
            textvariable=geocoding_provider_var,
            values=["nominatim", "ors"],
            state="readonly",
            width=22,
        ).grid(row=1, column=1, sticky="w", pady=(12, 4))
        ttk.Label(geo_tab, text="Converts job and home addresses into map coordinates.", foreground=DARK_THEME["muted_fg"]).grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(12, 4))

        ttk.Label(geo_tab, text="Routing provider:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(
            geo_tab,
            textvariable=routing_provider_var,
            values=["direct", "osrm_demo", "ors"],
            state="readonly",
            width=22,
        ).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(geo_tab, text="Calculates route distance and travel time from those coordinates.", foreground=DARK_THEME["muted_fg"]).grid(row=2, column=2, sticky="w", padx=(12, 0), pady=4)

        ttk.Label(geo_tab, text="OSRM delay [s]:").grid(row=3, column=0, sticky="w", pady=(12, 4))
        ttk.Entry(geo_tab, textvariable=osrm_delay_var, width=10).grid(row=3, column=1, sticky="w", pady=(12, 4))

        ttk.Label(geo_tab, text="ORS delay [s]:").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(geo_tab, textvariable=ors_delay_var, width=10).grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(geo_tab, text="ORS API env var:").grid(row=5, column=0, sticky="w", pady=(12, 4))
        ttk.Entry(geo_tab, textvariable=ors_env_var, width=24).grid(row=5, column=1, sticky="w", pady=(12, 4))
        ttk.Label(
            geo_tab,
            text="Default: ORS_API_KEY. Used for ORS routing and ORS geocoding.",
            foreground=DARK_THEME["muted_fg"],
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        geo_tab.columnconfigure(1, weight=1)

        def build_list_editor(parent: ttk.Frame, title: str, initial_values: list[str], on_values_changed):
            ttk.Label(parent, text=title).pack(anchor=tk.W)
            listbox = tk.Listbox(
                parent,
                selectmode=tk.BROWSE,
                background=DARK_THEME["entry_bg"],
                foreground=DARK_THEME["entry_fg"],
                selectbackground=DARK_THEME["select_bg"],
                selectforeground=DARK_THEME["select_fg"],
                highlightthickness=0,
                relief=tk.FLAT,
            )
            scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(4, 0))
            scrollbar.pack(side=tk.LEFT, fill=tk.Y, pady=(4, 0))
            button_frame = ttk.Frame(parent)
            button_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))

            values = [str(v).strip() for v in initial_values if str(v).strip()]

            def reload_list() -> None:
                listbox.delete(0, tk.END)
                for value in values:
                    listbox.insert(tk.END, value)

            def selected_index() -> int | None:
                selection = listbox.curselection()
                return int(selection[0]) if selection else None

            def add() -> None:
                value = simpledialog.askstring("Add", "Name:", parent=dialog)
                if value and value.strip() and value.strip() not in values:
                    values.append(value.strip())
                    values.sort(key=str.casefold)
                    reload_list()
                    on_values_changed(list(values))

            def edit() -> None:
                index = selected_index()
                if index is None:
                    return
                old = values[index]
                new = simpledialog.askstring("Edit", "Name:", initialvalue=old, parent=dialog)
                if not new or not new.strip() or new.strip() == old:
                    return
                values[index] = new.strip()
                values.sort(key=str.casefold)
                reload_list()
                on_values_changed(list(values), old, new.strip())

            def remove() -> None:
                index = selected_index()
                if index is None:
                    return
                old = values.pop(index)
                reload_list()
                on_values_changed(list(values), old, "")

            ttk.Button(button_frame, text="Add", command=add).pack(fill=tk.X, pady=(0, 6))
            ttk.Button(button_frame, text="Edit", command=edit).pack(fill=tk.X, pady=(0, 6))
            ttk.Button(button_frame, text="Remove", command=remove).pack(fill=tk.X)
            reload_list()
            return values, reload_list

        def build_status_category_editor(parent: ttk.Frame) -> None:
            parent.columnconfigure(0, weight=1)
            parent.columnconfigure(1, weight=1)
            parent.columnconfigure(2, weight=1)
            parent.columnconfigure(3, weight=1)
            listboxes: dict[str, tk.Listbox] = {}

            def save_status_categories() -> None:
                self.status_values = self._flatten_status_categories()
                self.user_config["status_categories"] = self.status_categories
                self.user_config["status_values"] = self.status_values
                self._save_user_config()
                self._refresh_filter_status_values()
                self._configure_tree_tags()
                self.refresh_jobs()

            def reload_one(key: str) -> None:
                listbox = listboxes[key]
                listbox.delete(0, tk.END)
                colors = self._status_color_map()
                for status in self.status_categories.get(key, []):
                    display = f"{status} *" if self._is_system_status(status) else status
                    listbox.insert(tk.END, display)
                    bg = colors.get(status, DARK_THEME["panel"])
                    listbox.itemconfig(tk.END, background=bg, foreground=DARK_THEME["fg"])

            def reload_all() -> None:
                for key in STATUS_CATEGORY_ORDER:
                    reload_one(key)

            def selected_index(key: str) -> int | None:
                selection = listboxes[key].curselection()
                return int(selection[0]) if selection else None

            def status_exists(value: str, except_value: str = "") -> bool:
                wanted = self._normalized_status_name(value)
                ignored = self._normalized_status_name(except_value)
                return any(
                    self._normalized_status_name(existing) == wanted
                    and self._normalized_status_name(existing) != ignored
                    for values in self.status_categories.values()
                    for existing in values
                )

            def add_status(key: str) -> None:
                value = simpledialog.askstring("Add state", "State label:", parent=dialog)
                if not value or not value.strip():
                    return
                value = value.strip()
                if status_exists(value):
                    messagebox.showwarning("Duplicate state", f"The state '{value}' already exists.", parent=dialog)
                    return
                self.status_categories.setdefault(key, []).append(value)
                reload_all()
                save_status_categories()

            def edit_status(key: str) -> None:
                index = selected_index(key)
                if index is None:
                    return
                old = self.status_categories[key][index]
                if self._is_system_status(old):
                    messagebox.showinfo("System state", "System states cannot be renamed.", parent=dialog)
                    return
                new = simpledialog.askstring("Edit state", "State label:", initialvalue=old, parent=dialog)
                if not new or not new.strip() or new.strip() == old:
                    return
                new = new.strip()
                if status_exists(new, old):
                    messagebox.showwarning("Duplicate state", f"The state '{new}' already exists.", parent=dialog)
                    return
                self.status_categories[key][index] = new
                self.db.migrate_status_aliases({old: new})
                reload_all()
                save_status_categories()

            def remove_status(key: str) -> None:
                index = selected_index(key)
                if index is None:
                    return
                value = self.status_categories[key][index]
                if self._is_system_status(value):
                    messagebox.showinfo("System state", "System states cannot be removed.", parent=dialog)
                    return
                self.status_categories[key].pop(index)
                reload_all()
                save_status_categories()

            def move_status(key: str, direction: int) -> None:
                index = selected_index(key)
                if index is None:
                    return
                new_index = index + direction
                values = self.status_categories[key]
                if new_index < 0 or new_index >= len(values):
                    return
                values[index], values[new_index] = values[new_index], values[index]
                reload_all()
                listboxes[key].selection_set(new_index)
                save_status_categories()

            for col, key in enumerate(STATUS_CATEGORY_ORDER):
                frame = ttk.LabelFrame(parent, text=STATUS_CATEGORY_LABELS[key], padding=6)
                frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0), pady=4)
                frame.rowconfigure(0, weight=1)
                frame.columnconfigure(0, weight=1)
                listbox = tk.Listbox(
                    frame,
                    height=12,
                    background=DARK_THEME["entry_bg"],
                    foreground=DARK_THEME["entry_fg"],
                    selectbackground=DARK_THEME["select_bg"],
                    selectforeground=DARK_THEME["select_fg"],
                    highlightthickness=0,
                    relief=tk.FLAT,
                )
                listbox.grid(row=0, column=0, columnspan=2, sticky="nsew")
                listboxes[key] = listbox
                ttk.Button(frame, text="Add", command=lambda k=key: add_status(k)).grid(row=1, column=0, sticky="ew", pady=(6, 0))
                ttk.Button(frame, text="Edit", command=lambda k=key: edit_status(k)).grid(row=1, column=1, sticky="ew", pady=(6, 0), padx=(4, 0))
                ttk.Button(frame, text="Remove", command=lambda k=key: remove_status(k)).grid(row=2, column=0, sticky="ew", pady=(4, 0))
                ttk.Button(frame, text="Up", command=lambda k=key: move_status(k, -1)).grid(row=2, column=1, sticky="ew", pady=(4, 0), padx=(4, 0))
                ttk.Button(frame, text="Down", command=lambda k=key: move_status(k, 1)).grid(row=3, column=1, sticky="ew", pady=(4, 0), padx=(4, 0))
            reload_all()

        build_status_category_editor(status_tab)

        def update_industry_values(values: list[str], old: str = "", new: str = "") -> None:
            if old and new:
                self.db.rename_company_rating(old, new)
            elif old and not new:
                self.db.delete_company_rating(old)
            else:
                for value in values:
                    self.db.add_company_rating(value)
            self.company_rating_combo.configure(values=self.db.list_company_ratings())
            self.refresh_jobs()

        build_list_editor(industry_tab, "Known industries used for company/job classification", self.db.list_company_ratings(), update_industry_values)

        # Management pages that previously lived as separate main-window dialogs.
        ttk.Label(companies_tab, text="Known companies and their assigned industry / blacklist state").pack(anchor=tk.W)
        companies_tree_frame = ttk.Frame(companies_tab)
        companies_tree_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 6))
        companies_tree = ttk.Treeview(companies_tree_frame, columns=("company", "industry", "blacklisted"), show="headings", selectmode="browse")
        for column, title, width in (("company", "Company", 480), ("industry", "Industry", 220), ("blacklisted", "Blacklisted", 100)):
            companies_tree.heading(column, text=title)
            companies_tree.column(column, width=width, stretch=(column == "company"))
        companies_scroll = ttk.Scrollbar(companies_tree_frame, orient=tk.VERTICAL, command=companies_tree.yview)
        companies_tree.configure(yscrollcommand=companies_scroll.set)
        companies_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        companies_scroll.pack(side=tk.LEFT, fill=tk.Y)

        def on_companies_mousewheel(event) -> str:
            delta = getattr(event, "delta", 0)
            if delta:
                companies_tree.yview_scroll(-3 if delta > 0 else 3, "units")
            return "break"

        companies_tree.bind("<MouseWheel>", on_companies_mousewheel, add="+")
        companies_tree.bind("<Button-4>", lambda _event: (companies_tree.yview_scroll(-3, "units"), "break")[1], add="+")
        companies_tree.bind("<Button-5>", lambda _event: (companies_tree.yview_scroll(3, "units"), "break")[1], add="+")
        company_controls = ttk.Frame(companies_tab)
        company_controls.pack(fill=tk.X)
        company_industry_var = tk.StringVar()
        company_blacklisted_var = tk.BooleanVar()
        ttk.Label(company_controls, text="Industry:").pack(side=tk.LEFT)
        company_industry_combo = ttk.Combobox(company_controls, textvariable=company_industry_var, values=self.db.list_company_ratings(), width=26)
        company_industry_combo.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Checkbutton(company_controls, text="Blacklisted", variable=company_blacklisted_var).pack(side=tk.LEFT, padx=(0, 10))

        def reload_companies_tab() -> None:
            companies_tree.delete(*companies_tree.get_children())
            for index, profile in enumerate(self.db.list_company_profiles()):
                companies_tree.insert("", tk.END, iid=str(index), values=(profile.get("company", ""), profile.get("branch", ""), "Yes" if profile.get("blacklisted") else "No"))

        def load_company_selection(_event=None) -> None:
            selection = companies_tree.selection()
            if not selection:
                return
            values = companies_tree.item(selection[0], "values")
            company_industry_var.set(str(values[1] or ""))
            company_blacklisted_var.set(str(values[2]).lower() == "yes")

        def save_company_profile() -> None:
            selection = companies_tree.selection()
            if not selection:
                return
            values = companies_tree.item(selection[0], "values")
            company = str(values[0] or "").strip()
            if company:
                self.db.update_company_profile(company, company_industry_var.get().strip(), company_blacklisted_var.get())
                reload_companies_tab(); self.refresh_jobs()

        companies_tree.bind("<<TreeviewSelect>>", load_company_selection)
        ttk.Button(company_controls, text="Save company", command=save_company_profile).pack(side=tk.LEFT)
        reload_companies_tab()

        ttk.Label(blacklist_tab, text="Companies that should be automatically rejected").pack(anchor=tk.W)
        blacklist_list = tk.Listbox(blacklist_tab, selectmode=tk.BROWSE)
        blacklist_list.pack(fill=tk.BOTH, expand=True, pady=(6, 6))
        blacklist_buttons = ttk.Frame(blacklist_tab)
        blacklist_buttons.pack(fill=tk.X)
        def reload_blacklist_tab() -> None:
            blacklist_list.delete(0, tk.END)
            for company in self.db.list_blacklisted_companies():
                blacklist_list.insert(tk.END, company)
        def add_blacklist_tab() -> None:
            name = simpledialog.askstring("Add company", "Company name:", parent=dialog)
            if name:
                self.db.add_company_to_blacklist(name); reload_blacklist_tab(); reload_companies_tab(); self.refresh_jobs()
        def edit_blacklist_tab() -> None:
            sel = blacklist_list.curselection()
            if not sel: return
            old = str(blacklist_list.get(sel[0]))
            new = simpledialog.askstring("Edit company", "Company name:", initialvalue=old, parent=dialog)
            if new and new.strip() != old:
                self.db.rename_blacklisted_company(old, new.strip()); reload_blacklist_tab(); reload_companies_tab(); self.refresh_jobs()
        def remove_blacklist_tab() -> None:
            sel = blacklist_list.curselection()
            if not sel: return
            name = str(blacklist_list.get(sel[0]))
            self.db.remove_company_from_blacklist(name); reload_blacklist_tab(); reload_companies_tab(); self.refresh_jobs()
        ttk.Button(blacklist_buttons, text="Add", command=add_blacklist_tab).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(blacklist_buttons, text="Edit", command=edit_blacklist_tab).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(blacklist_buttons, text="Remove", command=remove_blacklist_tab).pack(side=tk.LEFT)
        reload_blacklist_tab()

        rejection_buttons = ttk.Frame(rejection_tab)
        rejection_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(
            rejection_buttons,
            text="Open rejection-reason editor",
            command=lambda: self.ask_reject_reason(selection_required=False),
        ).pack(side=tk.LEFT)

        ttk.Label(source_tab, text="Manual source abbreviations (shown with * in manually added jobs)").pack(anchor=tk.W)
        source_tree_frame = ttk.Frame(source_tab)
        source_tree_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        source_tree = ttk.Treeview(source_tree_frame, columns=("code", "description"), show="headings", selectmode="browse")
        source_tree.heading("code", text="Abbreviation")
        source_tree.heading("description", text="Description")
        source_tree.column("code", width=120, stretch=False, anchor=tk.W)
        source_tree.column("description", width=650, stretch=True, anchor=tk.W)
        source_scroll = ttk.Scrollbar(source_tree_frame, orient=tk.VERTICAL, command=source_tree.yview)
        source_tree.configure(yscrollcommand=source_scroll.set)
        source_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        source_scroll.pack(side=tk.LEFT, fill=tk.Y)
        source_buttons = ttk.Frame(source_tree_frame)
        source_buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))

        def reload_source_tree() -> None:
            source_tree.delete(*source_tree.get_children())
            for index, code in enumerate(self.manual_sources):
                source_tree.insert("", tk.END, iid=str(index), values=(code, self.manual_source_descriptions.get(code, "")))

        def source_editor(title: str, initial_code: str = "", initial_description: str = "") -> tuple[str, str] | None:
            sub = tk.Toplevel(dialog)
            sub.title(title)
            sub.transient(dialog)
            sub.grab_set()
            result = {"value": None}
            body = ttk.Frame(sub, padding=12)
            body.pack(fill=tk.BOTH, expand=True)
            code_var = tk.StringVar(value=initial_code)
            desc_var = tk.StringVar(value=initial_description)
            ttk.Label(body, text="Abbreviation (max. 5 characters):").grid(row=0, column=0, sticky="w")
            code_entry = ttk.Entry(body, textvariable=code_var, width=12)
            code_entry.grid(row=1, column=0, sticky="ew", pady=(4, 10))
            ttk.Label(body, text="Description:").grid(row=2, column=0, sticky="w")
            desc_entry = ttk.Entry(body, textvariable=desc_var, width=60)
            desc_entry.grid(row=3, column=0, sticky="ew", pady=(4, 10))
            buttons = ttk.Frame(body)
            buttons.grid(row=4, column=0, sticky="e")
            def save_source() -> None:
                code = code_var.get().strip().rstrip("*")
                if not code:
                    messagebox.showwarning("Manual source", "Please enter an abbreviation.", parent=sub)
                    return
                if len(code) > 5:
                    messagebox.showwarning("Manual source", "The abbreviation may contain at most 5 characters.", parent=sub)
                    return
                result["value"] = (code, desc_var.get().strip())
                sub.destroy()
            ttk.Button(buttons, text="Cancel", command=sub.destroy).pack(side=tk.RIGHT, padx=(6, 0))
            ttk.Button(buttons, text="Save", command=save_source).pack(side=tk.RIGHT)
            code_entry.focus_set() if not initial_code else desc_entry.focus_set()
            sub.wait_window()
            return result["value"]

        def selected_source_index() -> int | None:
            selection = source_tree.selection()
            return int(selection[0]) if selection else None

        def add_source() -> None:
            value = source_editor("Add manual source")
            if value is None:
                return
            code, description = value
            if any(code.casefold() == existing.casefold() for existing in self.manual_sources):
                messagebox.showwarning("Manual source", "That abbreviation already exists.", parent=dialog)
                return
            self.manual_sources.append(code)
            self.manual_sources.sort(key=str.casefold)
            self.manual_source_descriptions[code] = description
            reload_source_tree()

        def edit_source() -> None:
            index = selected_source_index()
            if index is None:
                return
            old = self.manual_sources[index]
            value = source_editor("Edit manual source", old, self.manual_source_descriptions.get(old, ""))
            if value is None:
                return
            code, description = value
            if code.casefold() != old.casefold() and any(code.casefold() == existing.casefold() for existing in self.manual_sources):
                messagebox.showwarning("Manual source", "That abbreviation already exists.", parent=dialog)
                return
            self.manual_sources[index] = code
            self.manual_source_descriptions.pop(old, None)
            self.manual_source_descriptions[code] = description
            self.manual_sources.sort(key=str.casefold)
            reload_source_tree()

        def remove_source() -> None:
            index = selected_source_index()
            if index is None:
                return
            code = self.manual_sources.pop(index)
            self.manual_source_descriptions.pop(code, None)
            if not self.manual_sources:
                self.manual_sources = ["Manual"]
            reload_source_tree()

        ttk.Button(source_buttons, text="Add", command=add_source).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(source_buttons, text="Edit", command=edit_source).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(source_buttons, text="Remove", command=remove_source).pack(fill=tk.X)
        reload_source_tree()

        # AI model configuration.
        ai_tab.columnconfigure(0, weight=1)

        ai_general_box = ttk.LabelFrame(ai_tab, text="AI profile / batch settings", padding=8)
        ai_general_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ai_general_box.columnconfigure(1, weight=1)

        profile_path_var = tk.StringVar(value=str(self.user_config.get("ai_profile_path") or "templates/profile_template.md"))
        detail_batch_size_var = tk.StringVar(value=str(self.user_config.get("ai_detail_batch_size", 4)))
        screening_batch_size_var = tk.StringVar(value=str(self.user_config.get("ai_screening_batch_size", 10)))
        ai_output_language_var = tk.StringVar(value=str(self.user_config.get("ai_output_language") or "German"))

        def browse_profile_path() -> None:
            filename = filedialog.askopenfilename(
                parent=dialog,
                title="Select AI profile",
                filetypes=[("Markdown files", "*.md"), ("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not filename:
                return
            try:
                profile_path_var.set(str(Path(filename).resolve().relative_to(Path.cwd().resolve())))
            except Exception:
                profile_path_var.set(filename)

        ttk.Label(ai_general_box, text="Profile path:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(ai_general_box, textvariable=profile_path_var, width=46).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(ai_general_box, text="Browse...", command=browse_profile_path).grid(row=0, column=2, sticky="w", padx=(8, 0), pady=3)
        ttk.Button(ai_general_box, text="Edit profile...", command=lambda: self.open_profile_editor(profile_path_var, parent=dialog)).grid(row=0, column=3, sticky="w", padx=(8, 0), pady=3)
        ttk.Label(ai_general_box, text="default: templates/profile_template.md", foreground=DARK_THEME["muted_fg"]).grid(row=1, column=1, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Label(ai_general_box, text="Deep-screening batch size:").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Spinbox(ai_general_box, textvariable=detail_batch_size_var, from_=1, to=20, width=7).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(
            ai_general_box,
            text="Jobs per API request; failed batches larger than 2 are retried as pairs.",
            foreground=DARK_THEME["muted_fg"],
        ).grid(row=2, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=3)

        ttk.Label(ai_general_box, text="Screening batch size:").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Spinbox(ai_general_box, textvariable=screening_batch_size_var, from_=1, to=50, width=7).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(
            ai_general_box,
            text="Jobs per API request for automatic Company Watch screening (default 10).",
            foreground=DARK_THEME["muted_fg"],
        ).grid(row=3, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=3)

        ttk.Label(ai_general_box, text="AI output language:").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Combobox(
            ai_general_box,
            textvariable=ai_output_language_var,
            values=("German", "English"),
            state="readonly",
            width=12,
        ).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(
            ai_general_box,
            text="Controls AI-generated summaries, ratings, screening reasons and AI chat. The UI remains English.",
            foreground=DARK_THEME["muted_fg"],
        ).grid(row=4, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=3)

        ai_tree = ttk.Treeview(
            ai_tab,
            columns=("default", "name", "provider", "model", "key", "base_url", "web", "enabled"),
            show="headings",
            height=9,
            selectmode="browse",
        )
        for col, label, width in [
            ("default", "Defaults", 165),
            ("name", "Name", 170),
            ("provider", "Provider", 85),
            ("model", "Model", 170),
            ("key", "API key env", 120),
            ("base_url", "Endpoint URL", 250),
            ("web", "Web", 55),
            ("enabled", "Enabled", 65),
        ]:
            ai_tree.heading(col, text=label)
            ai_tree.column(col, width=width, anchor=tk.W, stretch=(col == "base_url"))
        ai_tree.grid(row=1, column=0, sticky="nsew")
        ai_tab.rowconfigure(1, weight=1)

        ai_buttons = ttk.Frame(ai_tab)
        ai_buttons.grid(row=1, column=1, sticky="ns", padx=(8, 0))

        def reload_ai_tree() -> None:
            ai_tree.delete(*ai_tree.get_children())
            for index, config in enumerate(self.ai_models):
                ai_tree.insert(
                    "",
                    tk.END,
                    iid=str(index),
                    values=(
                        self._ai_default_labels_for_model(config.name),
                        config.name,
                        config.provider,
                        config.model,
                        config.api_key_env,
                        config.base_url,
                        "yes" if config.supports_web_search else "no",
                        "yes" if config.enabled else "no",
                    ),
                )

        def selected_ai_index() -> int | None:
            selection = ai_tree.selection()
            return int(selection[0]) if selection else None

        def edit_ai_dialog(existing: AiModelConfig | None = None) -> AiModelConfig | None:
            sub = tk.Toplevel(dialog)
            sub.title("AI model")
            sub.configure(bg=DARK_THEME["bg"])
            sub.transient(dialog)
            sub.grab_set()
            result: dict[str, AiModelConfig | None] = {"value": None}

            name_var = tk.StringVar(value=existing.name if existing else "")
            provider_var = tk.StringVar(value=existing.provider if existing else "openai")
            model_var = tk.StringVar(value=existing.model if existing else "")
            key_var = tk.StringVar(value=existing.api_key_env if existing else "OPENAI_API_KEY")
            base_url_var = tk.StringVar(value=existing.base_url if existing else "")
            enabled_var = tk.BooleanVar(value=existing.enabled if existing else True)
            web_search_var = tk.BooleanVar(value=existing.supports_web_search if existing else provider_var.get().strip().lower() == "openai")

            provider_combo = ttk.Combobox(sub, textvariable=provider_var, values=["openai", "mistral", "ollama"], state="readonly", width=20)
            rows = [
                ("Name:", ttk.Entry(sub, textvariable=name_var, width=44)),
                ("Provider:", provider_combo),
                ("Model:", ttk.Entry(sub, textvariable=model_var, width=44)),
                ("API key env:", ttk.Entry(sub, textvariable=key_var, width=44)),
                ("Base URL:", ttk.Entry(sub, textvariable=base_url_var, width=44)),
            ]
            for row_index, (label, widget) in enumerate(rows):
                ttk.Label(sub, text=label).grid(row=row_index, column=0, sticky="w", padx=10, pady=5)
                widget.grid(row=row_index, column=1, sticky="ew", padx=10, pady=5)
            def apply_provider_defaults(_event=None) -> None:
                # A provider change means the old endpoint belongs to a different
                # backend. Always replace provider-specific connection defaults.
                defaults = self._provider_defaults(provider_var.get())
                key_var.set(defaults.get("api_key_env", ""))
                base_url_var.set(defaults.get("base_url", ""))
                web_search_var.set(provider_var.get().strip().lower() == "openai")

            provider_combo.bind("<<ComboboxSelected>>", apply_provider_defaults)
            if existing is None:
                apply_provider_defaults()

            ttk.Checkbutton(sub, text="Enabled", variable=enabled_var).grid(row=len(rows), column=1, sticky="w", padx=10, pady=5)
            ttk.Checkbutton(sub, text="Supports OpenAI web_search tool", variable=web_search_var).grid(row=len(rows) + 1, column=1, sticky="w", padx=10, pady=5)
            sub.columnconfigure(1, weight=1)

            def ok() -> None:
                name = name_var.get().strip()
                provider = provider_var.get().strip().lower()
                model = model_var.get().strip()
                if not name or not provider or not model:
                    messagebox.showwarning("AI model", "Name, provider and model are required.", parent=sub)
                    return
                result["value"] = AiModelConfig(
                    name=name,
                    provider=provider,
                    model=model,
                    api_key_env=key_var.get().strip(),
                    base_url=base_url_var.get().strip(),
                    enabled=enabled_var.get(),
                    supports_web_search=web_search_var.get(),
                )
                sub.destroy()

            button_row = ttk.Frame(sub)
            button_row.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e", padx=10, pady=10)
            ttk.Button(button_row, text="Cancel", command=sub.destroy).pack(side=tk.RIGHT, padx=(6, 0))
            ttk.Button(button_row, text="OK", command=ok).pack(side=tk.RIGHT)
            sub.wait_window()
            return result["value"]

        def add_ai() -> None:
            config = edit_ai_dialog()
            if config is not None:
                self.ai_models.append(config)
                reload_ai_tree()

        def edit_ai() -> None:
            index = selected_ai_index()
            if index is None:
                return
            config = edit_ai_dialog(self.ai_models[index])
            if config is not None:
                self.ai_models[index] = config
                reload_ai_tree()

        def remove_ai() -> None:
            index = selected_ai_index()
            if index is None:
                return
            removed = self.ai_models.pop(index)
            defaults = self._ai_task_defaults()
            for key in list(defaults.keys()):
                if defaults.get(key) == removed.name:
                    defaults.pop(key, None)
            if self.user_config.get("default_ai_model") == removed.name:
                self.user_config["default_ai_model"] = ""
            reload_ai_tree()

        def duplicate_ai() -> None:
            index = selected_ai_index()
            if index is None:
                return
            src = self.ai_models[index]
            copy = AiModelConfig(
                name=self._unique_ai_model_copy_name(src.name),
                provider=src.provider,
                model=src.model,
                api_key_env=src.api_key_env,
                base_url=src.base_url,
                enabled=src.enabled,
                supports_web_search=src.supports_web_search,
            )
            edited = edit_ai_dialog(copy)
            if edited is not None:
                self.ai_models.append(edited)
                reload_ai_tree()

        def set_task_default(task_key: str) -> None:
            index = selected_ai_index()
            if index is None:
                return
            self._ai_task_defaults()[task_key] = self.ai_models[index].name
            reload_ai_tree()

        def clear_selected_defaults() -> None:
            index = selected_ai_index()
            if index is None:
                return
            name = self.ai_models[index].name
            defaults = self._ai_task_defaults()
            for key in list(defaults.keys()):
                if defaults.get(key) == name:
                    defaults.pop(key, None)
            reload_ai_tree()

        ai_model_menu = tk.Menu(dialog, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        def show_ai_model_menu(event) -> None:
            row_id = ai_tree.identify_row(event.y)
            if row_id:
                ai_tree.selection_set(row_id)
            ai_model_menu.delete(0, tk.END)
            selected_index = selected_ai_index()
            if selected_index is None:
                return
            selected_name = self.ai_models[selected_index].name
            defaults = self._ai_task_defaults()
            added = 0
            for task_key, task_label in self.AI_TASKS:
                assigned = str(defaults.get(task_key, "") or "").strip()
                if assigned == selected_name:
                    continue
                suffix = " (unassigned!)" if not assigned else ""
                ai_model_menu.add_command(label=f"Set default for {task_label}{suffix}", command=lambda k=task_key: set_task_default(k))
                added += 1
            selected_has_defaults = any(value == selected_name for value in defaults.values())
            if selected_has_defaults:
                if added:
                    ai_model_menu.add_separator()
                ai_model_menu.add_command(label="Clear defaults for selected", command=clear_selected_defaults)
            elif not added:
                ai_model_menu.add_command(label="No available default assignments", state=tk.DISABLED)
            ai_model_menu.tk_popup(event.x_root, event.y_root)
        ai_tree.bind("<Button-3>", show_ai_model_menu)

        ttk.Button(ai_buttons, text="Add", command=add_ai).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(ai_buttons, text="Duplicate", command=duplicate_ai).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(ai_buttons, text="Edit", command=edit_ai).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(ai_buttons, text="Remove", command=remove_ai).pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            ai_tab,
            text="Right-click an AI model to set task-specific defaults. Session choices are remembered until exit.",
            foreground=DARK_THEME["muted_fg"],
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        reload_ai_tree()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill=tk.X, padx=10, pady=(0, 10))

        def save_and_close() -> None:
            try:
                float(osrm_delay_var.get().strip() or "1.1")
                float(ors_delay_var.get().strip() or "1.6")
            except ValueError:
                messagebox.showwarning("Invalid delay", "Delays must be numbers.", parent=dialog)
                return
            old_home = str(settings.get("home_address", "")).strip()
            new_home = home_var.get().strip()
            self.db.save_geo_settings(
                new_home,
                geocoding_provider_var.get(),
                routing_provider_var.get(),
                osrm_delay_var.get(),
                ors_delay_var.get(),
                ors_env_var.get(),
            )
            if new_home and (new_home != old_home or self.db.get_cached_address_by_text(new_home) is None):
                try:
                    delay = self._delay_for_provider("ors" if geocoding_provider_var.get() == "ors" else "osrm_demo", {
                        "osrm_delay": osrm_delay_var.get(),
                        "ors_delay": ors_delay_var.get(),
                    })
                    router = create_router(
                        "direct",
                        request_delay_seconds=delay,
                        ors_env_var=ors_env_var.get(),
                        geocoding_provider=geocoding_provider_var.get(),
                    )
                    result = router.geocode(new_home)
                    self.db.save_cached_address_by_text(new_home, result.lat, result.lon, result.display_name, geocoding_provider_var.get())
                except Exception as exc:
                    messagebox.showwarning("Home address", f"Settings were saved, but the home address could not be geocoded yet:\n\n{exc}", parent=dialog)
            self.user_config["status_categories"] = self.status_categories
            self.user_config["status_values"] = self.status_values
            self.user_config["manual_sources"] = self.manual_sources
            self.user_config["manual_source_descriptions"] = self.manual_source_descriptions
            try:
                detail_batch_size = int(detail_batch_size_var.get().strip() or "4")
                screening_batch_size = int(screening_batch_size_var.get().strip() or "10")
                if not 1 <= detail_batch_size <= 20 or not 1 <= screening_batch_size <= 50:
                    raise ValueError("batch size out of range")
            except ValueError:
                messagebox.showwarning(
                    "Invalid AI settings",
                    "AI batch sizes must be integers. Deep-screening batches must be between 1 and 20 "
                    "and screening batches between 1 and 50.",
                    parent=dialog,
                )
                return
            self.user_config["ai_models"] = [config.to_dict() for config in self.ai_models]
            valid_ai_names = {config.name for config in self.ai_models}
            for key in list(self._ai_task_defaults().keys()):
                if self._ai_task_defaults().get(key) not in valid_ai_names:
                    self._ai_task_defaults().pop(key, None)
            if self.user_config.get("default_ai_model") not in valid_ai_names:
                self.user_config["default_ai_model"] = ""
            profile_path_value = profile_path_var.get().strip() or "templates/profile_template.md"
            profile_path = Path(profile_path_value)
            if not profile_path.is_absolute():
                repository_root = Path(__file__).resolve().parent.parent
                candidates = [Path.cwd() / profile_path, repository_root / profile_path, Path(__file__).resolve().parent / profile_path]
                # Keep the configured path even when the template/profile does not
                # exist yet. The profile editor will simply open it as an empty file.
            elif not profile_path.exists():
                pass
            self.user_config["ai_profile_path"] = profile_path_value
            self.user_config["ai_detail_batch_size"] = detail_batch_size
            self.user_config["ai_screening_batch_size"] = screening_batch_size
            self.user_config["ai_output_language"] = ai_output_language_var.get().strip() or "German"
            self._save_user_config()
            self._refresh_filter_status_values()
            self.status_label_var.set("Settings saved.")
            dialog.destroy()

        ttk.Button(buttons, text="Save and Close", command=save_and_close).pack(side=tk.RIGHT)

    def _set_window_icon(self) -> None:
        """Apply the bundled JobRadar icon to the root window and future dialogs."""
        assets_dir = Path(__file__).resolve().parent.parent / "assets"
        png_path = assets_dir / "jobradar_icon.png"
        ico_path = assets_dir / "jobradar_icon.ico"

        # iconphoto works cross-platform and, with default=True, also becomes
        # the default icon for subsequently created Toplevel windows.
        try:
            if png_path.exists():
                self._jobradar_icon_image = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, self._jobradar_icon_image)
        except tk.TclError:
            self._jobradar_icon_image = None

        # Windows title bars/taskbar generally prefer a multi-resolution .ico.
        if os.name == "nt":
            try:
                if ico_path.exists():
                    self.iconbitmap(default=str(ico_path))
            except tk.TclError:
                pass

    def _refresh_filter_status_values(self) -> None:
        # Rebuild the status filter combobox values. The widget is the only readonly combobox in the filterbar,
        # so keep a direct reference when available.
        try:
            self.status_filter_combo.configure(values=["all"] + self.status_values)
        except AttributeError:
            pass
        self.status_combo.configure(values=self.status_values)

    def copy_job_description(self) -> None:
        job = self._get_display_job(self.selected_iid)
        if job is None:
            return
        details = [
            f"Job title: {self._display_title(job)}",
            f"Company: {job.company}",
            f"Location: {self._display_location(job)}",
            f"Distance/time: {self._distance_display(job.route_distance_km, job.route_quality)} ({self._duration_display(job.route_duration_min, job.route_quality)})",
            f"Status: {job.status}",
            f"Industry: {job.branch or '-'}",
            f"Term: {job.fixed_term or '-'}",
            f"Salary min/max: {self._salary_display(job.min_salary_k)} / {self._salary_display(job.max_salary_k)} k€",
        ]
        features = self._job_feature_labels(job)
        if features:
            details.append(f"Special features: {', '.join(features)}")
        text = "\n".join(details) + "\n\n" + (job.description or "")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_label_var.set("Job description copied to clipboard.")

    def save_notes_for_selection(self) -> None:
        selection = list(self.tree.selection())
        if len(selection) != 1:
            messagebox.showinfo("Save notes", "Please select exactly one saved job.", parent=self)
            return
        job = self._get_display_job(selection[0])
        if job is None:
            return
        notes = self.notes_text.get("1.0", tk.END).strip()
        if job.db_id is None:
            candidate = self.pending_by_iid.get(job.iid)
            if candidate is not None:
                # Notes for unsaved jobs are only kept in the detail UI for now.
                self.status_label_var.set("Notes for unsaved jobs are not stored until the job is saved.")
            return
        self.db.save_user_fields(job_id=job.db_id, status=job.status, score=job.manual_score, company_rating=job.branch, notes=notes)
        job.notes = notes
        self.notes_dirty_iids.discard(job.iid)
        self._update_dirty_indicators_for_iid(job.iid)
        self.refresh_jobs_preserving_detail_tabs()
        self.notes_dirty_iids.discard(str(job.db_id))
        self._update_detail_tab_labels()
        self._update_attention_buttons()
        self.status_label_var.set("Notes saved.")

    def _raw_entry_text(self, job: DisplayJob) -> str:
        data = self._load_raw_entry_for_job(job)
        if data:
            try:
                return json.dumps(data, indent=4, ensure_ascii=False)
            except Exception:
                return str(data)
        if job.db_id is not None:
            return self.db.get_latest_raw_json(job.db_id) or ""
        if job.candidate is not None:
            return job.candidate.raw_json or ""
        return ""

    def _exact_location_text(self, job: DisplayJob) -> str:
        loc = self._selected_ba_location(job)
        lat = loc.get("breite") if loc else None
        lon = loc.get("laenge") if loc else None
        if lat is None or lon is None:
            return ""
        return f"{lat}, {lon}"

    def geo_settings_dialog(self) -> None:
        # Backward-compatible entry point for older buttons/shortcuts.
        self.settings_dialog()

    @staticmethod
    def _delay_for_provider(provider: str, settings: dict[str, object]) -> float:
        key = (provider or "direct").strip().lower()
        if key in {"direct", "direct_estimate", "estimate"}:
            return 0.0
        if key in {"ors", "openrouteservice", "open_route_service"}:
            raw = settings.get("ors_delay", "1.6")
            fallback = 1.6
        else:
            raw = settings.get("osrm_delay", settings.get("request_delay", "1.1"))
            fallback = 1.1
        try:
            return max(0.0, float(str(raw or fallback)))
        except ValueError:
            return fallback

    def _geocoding_delay(self, settings: dict[str, object]) -> float:
        provider = str(settings.get("geocoding_provider", "nominatim")).strip().lower()
        if provider in {"ors", "openrouteservice", "open_route_service"}:
            return self._delay_for_provider("ors", settings)
        # Nominatim's public service asks clients to be conservative. Reuse the OSRM delay field.
        return self._delay_for_provider("osrm_demo", settings)

    def _create_router_from_settings(self, provider: str, settings: dict[str, object]):
        route_delay = self._delay_for_provider(provider, settings)
        if (provider or "direct").strip().lower() in {"direct", "direct_estimate", "estimate"}:
            route_delay = self._geocoding_delay(settings)
        return create_router(
            provider,
            request_delay_seconds=route_delay,
            ors_env_var=str(settings.get("ors_env_var", "ORS_API_KEY")),
            geocoding_provider=str(settings.get("geocoding_provider", "nominatim")),
        )

    def _geocode_with_cache(self, address_text: str, router, provider_label: str) -> GeocodeResult:
        address_text = address_text.strip()
        if not address_text:
            raise ValueError("Empty address")
        if not address_text.lower().startswith("coords:"):
            cached = self.db.get_cached_address_by_text(address_text)
            if cached is not None:
                return GeocodeResult(
                    address_text=address_text,
                    lat=float(cached["lat"]),
                    lon=float(cached["lon"]),
                    display_name=str(cached.get("display_name") or ""),
                )
        result = router.geocode(address_text)
        self.db.save_cached_address_by_text(address_text, result.lat, result.lon, result.display_name, provider_label)
        return result

    def calculate_routes_for_selection_or_visible(self) -> None:
        iids = [job.iid for job in self.current_jobs]
        if not iids:
            self.status_label_var.set("No visible jobs to calculate routes for.")
            return
        destinations = set()
        for iid in iids:
            job = self._get_display_job(iid)
            if job is None:
                continue
            destination = self._route_destination_for_job(job, use_exact=False, show_errors=False)
            if destination:
                destinations.add(destination)
        if len(destinations) > 10:
            if not messagebox.askyesno("Calculate routes", f"This will calculate routes for {len(destinations)} different destinations. Continue?"):
                return
        self._calculate_routes_for_iids(iids, use_exact=False)

    def calculate_routes_for_selection(self, use_exact: bool = False) -> None:
        selection = list(self.tree.selection())
        if not selection:
            messagebox.showinfo("No selection", "Please select at least one job.")
            return
        self._calculate_routes_for_iids(selection, use_exact=use_exact)

    def _calculate_routes_for_iids(self, iids: list[str], use_exact: bool) -> None:
        settings = self.db.get_geo_settings()
        home_address = str(settings.get("home_address", "")).strip()
        if not home_address:
            messagebox.showinfo("Missing home address", "Please enter your home address in Geo settings first.")
            self.settings_dialog()
            return
        provider = str(settings.get("routing_provider", settings.get("provider", "direct")) or "direct")
        delay = self._delay_for_provider(provider, settings)
        if provider.strip().lower() in {"direct", "direct_estimate", "estimate"}:
            delay = self._geocoding_delay(settings)

        direct_router = None
        direct_start = None
        if not use_exact:
            try:
                direct_router = self._create_router_from_settings("direct", settings)
                direct_start = self._geocode_with_cache(home_address, direct_router, str(settings.get("geocoding_provider", "nominatim")))
            except Exception:
                direct_router = None
                direct_start = None

        tasks: list[dict[str, object]] = []
        cached_count = 0
        for iid in iids:
            job = self._get_display_job(iid)
            if job is None:
                continue
            if not use_exact and direct_router is not None and direct_start is not None:
                self._auto_select_nearest_location(job, direct_router, direct_start)
            destination = self._route_destination_for_job(job, use_exact=use_exact)
            if not destination:
                continue
            cached = self.db.get_cached_route_by_text(home_address, destination, provider)
            if cached is not None:
                self._apply_route_result_to_job(
                    iid=iid,
                    destination_text=destination,
                    distance_km=float(cached["distance_km"]),
                    duration_min=float(cached["duration_min"]),
                    quality=str(cached.get("quality") or ""),
                    locked=use_exact,
                    persist_job=job.db_id is not None,
                )
                cached_count += 1
            else:
                tasks.append({"iid": iid, "destination": destination, "persist_job": job.db_id is not None, "locked": use_exact})

        if cached_count:
            self.refresh_jobs()
        if not tasks:
            self.status_label_var.set(f"Routes loaded from cache: {cached_count}.")
            return

        self.status_label_var.set(f"Calculating {len(tasks)} route(s)... cached: {cached_count}")
        task_id = self._start_background_ai_task("routing", [], f"Route calculation ({len(tasks)} jobs)")
        threading.Thread(
            target=self._route_worker,
            args=(home_address, provider, delay, str(settings.get("geocoding_provider", "nominatim")), str(settings.get("ors_env_var", "ORS_API_KEY")), tasks, task_id),
            daemon=True,
        ).start()

    def _route_worker(self, home_address: str, provider: str, delay: float, geocoding_provider: str, ors_env_var: str, tasks: list[dict[str, object]], task_id: int) -> None:
        try:
            router = create_router(
                provider,
                request_delay_seconds=delay,
                ors_env_var=ors_env_var,
                geocoding_provider=geocoding_provider,
            )
            start = self._geocode_with_cache(home_address, router, geocoding_provider)
        except Exception as exc:
            error_message = str(exc)
            self.after(0, lambda msg=error_message, tid=task_id: (self.status_label_var.set(f"Routing setup failed: {msg}"), self._finish_background_ai_task(tid, False)))
            return
        results: list[tuple[dict[str, object], RouteResult | Exception]] = []
        local_results: dict[str, RouteResult] = {}
        local_geocodes: dict[str, GeocodeResult] = {}
        for task in tasks:
            destination = str(task["destination"])
            try:
                if destination in local_results:
                    route = local_results[destination]
                else:
                    destination_geo = local_geocodes.get(destination)
                    if destination_geo is None:
                        destination_geo = self._geocode_with_cache(destination, router, geocoding_provider)
                        local_geocodes[destination] = destination_geo
                    route = router.route(start, destination_geo)
                    local_results[destination] = route
                results.append((task, route))
            except Exception as exc:
                results.append((task, exc))
        self.after(0, lambda: self._finish_route_worker(home_address, provider, results, task_id))

    def _finish_route_worker(self, home_address: str, provider: str, results: list[tuple[dict[str, object], RouteResult | Exception]], task_id: int) -> None:
        ok = 0
        failed = 0
        first_error = ""
        for task, result in results:
            if isinstance(result, Exception):
                failed += 1
                if not first_error:
                    first_error = str(result)
                continue
            destination_text = str(task["destination"])
            locked = bool(task["locked"])
            self.db.save_cached_route_by_text(
                from_address_text=home_address,
                from_lat=result.start.lat,
                from_lon=result.start.lon,
                from_display_name=result.start.display_name,
                to_address_text=result.destination.address_text,
                to_lat=result.destination.lat,
                to_lon=result.destination.lon,
                to_display_name=result.destination.display_name,
                provider=result.provider,
                quality=result.quality,
                distance_km=result.distance_km,
                duration_min=result.duration_min,
            )
            self._apply_route_result_to_matching_visible_jobs(
                destination_text=destination_text,
                distance_km=result.distance_km,
                duration_min=result.duration_min,
                quality=result.quality,
                locked=locked,
            )
            ok += 1
        self.refresh_jobs()
        message = f"Routes calculated: {ok}"
        if failed:
            message += f", failed: {failed}"
            if first_error:
                message += f" ({first_error})"
        self.status_label_var.set(message)
        self._finish_background_ai_task(task_id, failed == 0)

    def _populate_known_routes_from_cache(self) -> None:
        settings = self.db.get_geo_settings()
        home_address = str(settings.get("home_address", "")).strip()
        if not home_address:
            return
        provider = str(settings.get("routing_provider", settings.get("provider", "direct")) or "direct")

        for job in list(self.current_jobs):
            if job.route_distance_km is not None and job.route_duration_min is not None:
                continue

            use_exact = bool(job.record.route_locked) if job.record is not None else bool(job.candidate.route_locked) if job.candidate is not None else False
            destination = job.route_address_text or self._route_destination_for_job(job, use_exact=use_exact)
            if not destination:
                continue

            cached = self.db.get_cached_route_by_text(home_address, destination, provider)
            if cached is None:
                continue

            self._apply_route_result_to_job(
                iid=job.iid,
                destination_text=destination,
                distance_km=float(cached["distance_km"]),
                duration_min=float(cached["duration_min"]),
                quality=str(cached.get("quality") or ""),
                locked=use_exact,
                persist_job=job.db_id is not None,
            )

    def _apply_route_result_to_matching_visible_jobs(self, destination_text: str, distance_km: float, duration_min: float, quality: str, locked: bool) -> None:
        for job in list(self.current_jobs):
            compare_destination = job.route_address_text or self._route_destination_for_job(job, use_exact=locked)
            if compare_destination != destination_text:
                continue
            self._apply_route_result_to_job(
                iid=job.iid,
                destination_text=destination_text,
                distance_km=distance_km,
                duration_min=duration_min,
                quality=quality,
                locked=locked,
                persist_job=job.db_id is not None,
            )

    def _apply_route_result_to_job(self, iid: str, destination_text: str, distance_km: float, duration_min: float, quality: str, locked: bool, persist_job: bool) -> None:
        job = self._get_display_job(iid)
        if job is None:
            return
        if persist_job and job.db_id is not None:
            self.db.save_job_route(job.db_id, destination_text, distance_km, duration_min, locked, quality)
            if job.record is not None:
                job.record.route_distance_km = distance_km
                job.record.route_duration_min = duration_min
                job.record.route_address_text = destination_text
                job.record.route_locked = locked
                job.record.route_quality = quality
        else:
            self.temp_routes_by_iid[iid] = (distance_km, duration_min, destination_text, quality)
            candidate = self.pending_by_iid.get(iid)
            if candidate is not None:
                candidate.route_distance_km = distance_km
                candidate.route_duration_min = duration_min
                candidate.route_address_text = destination_text
                candidate.route_locked = locked
                candidate.route_quality = quality

    def _location_alias_for_job(self, job: DisplayJob) -> str:
        display = self._display_location(job).lstrip("* ").strip()
        try:
            return self.db.get_location_alias(display)
        except Exception:
            return ""

    def delete_route_cache_for_selection(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if not jobs:
            self.status_label_var.set("Select at least one job first.")
            return
        settings = self.db.get_geo_settings()
        home_address = str(settings.get("home_address", "")).strip()
        provider = str(settings.get("routing_provider", settings.get("provider", "direct")) or "direct")
        if not home_address:
            self.status_label_var.set("No home address is configured.")
            return
        destinations: set[str] = set()
        for job in jobs:
            locked = bool(job.record.route_locked) if job.record is not None else bool(job.candidate.route_locked) if job.candidate is not None else False
            destination = job.route_address_text or self._route_destination_for_job(job, use_exact=locked, show_errors=False)
            if destination:
                destinations.add(destination)
        if not destinations:
            self.status_label_var.set("No route destination found for the selection.")
            return
        if not messagebox.askyesno(
            "Delete route cache",
            f"Delete cached route and destination geocoding for {len(destinations)} location(s)?\n\n"
            "The route can then be calculated again from scratch.",
            parent=self,
        ):
            return
        for destination in destinations:
            self.db.delete_cached_route_by_text(home_address, destination, provider="", clear_destination_geocode=True)
        cleared = 0
        for visible in list(self.current_jobs):
            locked = bool(visible.record.route_locked) if visible.record is not None else bool(visible.candidate.route_locked) if visible.candidate is not None else False
            destination = visible.route_address_text or self._route_destination_for_job(visible, use_exact=locked, show_errors=False)
            if destination not in destinations:
                continue
            if visible.db_id is not None:
                self.db.clear_job_route(visible.db_id)
            self.temp_routes_by_iid.pop(visible.iid, None)
            if visible.record is not None:
                visible.record.route_distance_km = None
                visible.record.route_duration_min = None
                visible.record.route_address_text = ""
                visible.record.route_locked = False
                visible.record.route_quality = ""
            if visible.candidate is not None:
                visible.candidate.route_distance_km = None
                visible.candidate.route_duration_min = None
                visible.candidate.route_address_text = ""
                visible.candidate.route_locked = False
                visible.candidate.route_quality = ""
            cleared += 1
        self.refresh_jobs()
        self.status_label_var.set(f"Route cache deleted for {len(destinations)} location(s); cleared {cleared} visible job(s).")

    def set_or_edit_location_alias_for_selected_job(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if len(jobs) != 1:
            self.status_label_var.set("Set/Edit alias requires exactly one selected job.")
            return
        job = jobs[0]
        display = self._display_location(job).lstrip("* ").strip()
        if not display:
            return
        current = self._location_alias_for_job(job)
        value = simpledialog.askstring(
            "Location alias",
            f"Display location:\n{display}\n\nRouting location alias (empty = delete alias):",
            initialvalue=current,
            parent=self,
        )
        if value is None:
            return
        self.db.set_location_alias(display, value.strip())
        # Clear cached route shown on all matching visible rows. The route cache stays intact,
        # but future lookups use the alias destination.
        for visible in list(self.current_jobs):
            if self._display_location(visible).lstrip("* ").strip() == display:
                if visible.db_id is not None:
                    try:
                        self.db.clear_job_route(visible.db_id)
                    except Exception:
                        # Fallback: at least clear the in-memory representation.
                        pass
                if visible.record is not None:
                    visible.record.route_distance_km = None
                    visible.record.route_duration_min = None
                    visible.record.route_address_text = ""
                    visible.record.route_locked = False
                    visible.record.route_quality = ""
                if visible.candidate is not None:
                    visible.candidate.route_distance_km = None
                    visible.candidate.route_duration_min = None
                    visible.candidate.route_address_text = ""
                    visible.candidate.route_locked = False
                    visible.candidate.route_quality = ""
                self.temp_routes_by_iid.pop(visible.iid, None)
        self.refresh_jobs()
        self.status_label_var.set(f"Location alias updated for {display}.")

    def _has_exact_location_for_job(self, job: DisplayJob) -> bool:
        return bool(self._extract_exact_route_destination(job))

    def open_selected_job_url(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if len(jobs) != 1:
            self.status_label_var.set("Open URL requires exactly one selected job.")
            return
        url = str(jobs[0].url or "").strip()
        if not url:
            self.status_label_var.set("Selected job has no URL.")
            return
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        webbrowser.open(url)

    def _route_destination_for_job(self, job: DisplayJob, use_exact: bool, show_errors: bool = True) -> str:
        if use_exact:
            exact = self._extract_exact_route_destination(job)
            if exact:
                return exact
            if show_errors:
                messagebox.showinfo("No exact address", f"No exact address or coordinates found for:\n\n{job.title}\n{job.company}")
            return ""

        # A user-defined alias overrides the visible location for routing, e.g.
        # "München (Vor Ort)" -> "München".
        alias = self._location_alias_for_job(job)
        if alias:
            country_text = self._country_display(job.country).strip()
            return f"{alias}, {country_text}" if country_text and country_text.casefold() not in alias.casefold() else alias

        # For normal location routing, prefer a clean BA location from raw JSON:
        # PLZ + city only. Regional suffixes like "Mittelfranken, BAYERN" often
        # make Nominatim fail. Fall back to the visible location string.
        return self._extract_clean_location_destination(job) or job.route_address_text or job.location

    def _prefetch_raw_entries(self, jobs: list[DisplayJob]) -> None:
        missing_ids = [job.db_id for job in jobs if job.db_id is not None and job.db_id not in self.raw_entry_cache]
        if not missing_ids:
            return
        try:
            raw_by_id = self.db.get_latest_raw_json_many(missing_ids)
        except Exception:
            return
        for job_id, raw_json in raw_by_id.items():
            try:
                value = json.loads(raw_json) if raw_json else {}
            except Exception:
                value = {}
            self.raw_entry_cache[int(job_id)] = value if isinstance(value, dict) else {}

    def _load_raw_entry_for_job(self, job: DisplayJob) -> dict:
        raw_json = ""
        if job.db_id is not None:
            cached = self.raw_entry_cache.get(job.db_id)
            if cached is not None:
                return cached
            raw_json = self.db.get_latest_raw_json(job.db_id)
        elif job.candidate is not None:
            raw_json = job.candidate.raw_json
        if not raw_json:
            return {}
        try:
            data = json.loads(raw_json)
        except Exception:
            return {}
        data = data if isinstance(data, dict) else {}
        if job.db_id is not None:
            self.raw_entry_cache[job.db_id] = data
        return data

    def _job_title_prefixes(self, job: DisplayJob) -> str:
        data = self._load_raw_entry_for_job(job)
        if not data:
            return ""

        prefixes: list[str] = []
        if data.get("istArbeitnehmerUeberlassung") is True:
            prefixes.append("ANÜ")
        if data.get("istPrivateArbeitsvermittlung") is True:
            prefixes.append("PAV")
        if data.get("istGeringfuegigeBeschaeftigung") is True:
            prefixes.append("MINI")
        if data.get("istBehinderungGefordert") is True:
            prefixes.append("DIS")
        if job.fixed_term and job.fixed_term != "-":
            prefixes.append("FIXED-TERM")

        return "".join(f"[{prefix}] " for prefix in prefixes)

    def _duplicate_score_for_job(self, job: DisplayJob) -> float:
        if job.candidate is not None:
            return float(getattr(job.candidate, "duplicate_score", 0.0) or 0.0)
        if job.record is not None:
            return float(getattr(job.record, "duplicate_score", 0.0) or 0.0)
        return 0.0

    def _display_title(self, job: DisplayJob) -> str:
        duplicate_score = self._duplicate_score_for_job(job)
        if 99.5 <= duplicate_score < 100.0:
            dup_prefix = f"[DUP {duplicate_score:.1f}%] "
        elif 80.0 <= duplicate_score < 99.5:
            dup_prefix = f"[DUP {duplicate_score:.0f}%] "
        else:
            dup_prefix = ""
        return f"{dup_prefix}{self._job_title_prefixes(job)}{job.title}"

    def _job_feature_labels(self, job: DisplayJob) -> list[str]:
        prefix_text = self._job_title_prefixes(job)
        labels: list[str] = []
        for part in prefix_text.split("]"):
            clean = part.replace("[", "").strip()
            if clean:
                labels.append(clean)
        return labels

    def _ba_locations(self, job: DisplayJob) -> list[dict]:
        data = self._load_raw_entry_for_job(job)
        locations = data.get("stellenlokationen") if isinstance(data, dict) else None
        if not isinstance(locations, list):
            return []
        return [item for item in locations if isinstance(item, dict)]

    def _selected_ba_location(self, job: DisplayJob) -> dict:
        locations = self._ba_locations(job)
        if not locations:
            return {}
        index = max(0, min(job.selected_location_index, len(locations) - 1))
        return locations[index]

    def _first_ba_location(self, job: DisplayJob) -> dict:
        # Backward-compatible alias: use the currently selected BA location.
        return self._selected_ba_location(job)

    def _has_multiple_ba_locations(self, job: DisplayJob) -> bool:
        return len(self._ba_locations(job)) > 1

    def _auto_select_nearest_location(self, job: DisplayJob, direct_router, direct_start) -> None:
        if job.selected_location_manual:
            return
        locations = self._ba_locations(job)
        if len(locations) <= 1:
            return

        best_index = job.selected_location_index
        best_distance = None
        for index, location in enumerate(locations):
            try:
                lat = float(location.get("breite"))
                lon = float(location.get("laenge"))
                destination_query = f"coords:{lat},{lon}"
            except (TypeError, ValueError):
                address = location.get("adresse") or {}
                if not isinstance(address, dict):
                    continue
                postal = str(address.get("plz") or address.get("postleitzahl") or "").strip()
                city = str(address.get("ort") or address.get("city") or "").strip()
                region = str(address.get("region") or "").strip()
                country = str(address.get("land") or job.country or "").strip()
                destination_query = ", ".join(part for part in (postal, city, region, country) if part)
                if not destination_query:
                    continue
            try:
                destination = direct_router.geocode(destination_query)
                route = direct_router.route(direct_start, destination)
            except Exception:
                continue
            if best_distance is None or route.distance_km < best_distance:
                best_distance = route.distance_km
                best_index = index

        if best_distance is None or best_index == job.selected_location_index:
            return

        location_text = self._format_ba_location_label(locations[best_index])
        location_country = self._location_country(locations[best_index])
        if job.db_id is not None:
            self.db.save_job_selected_location(job.db_id, best_index, location_text, manual=False, country=location_country)
            if job.record is not None:
                job.record.selected_location_index = best_index
                job.record.selected_location_manual = False
                job.record.location = location_text
                if location_country:
                    job.record.country = location_country
                job.record.route_distance_km = None
                job.record.route_duration_min = None
                job.record.route_address_text = ""
                job.record.route_locked = False
                job.record.route_quality = ""
        else:
            candidate = self.pending_by_iid.get(job.iid)
            if candidate is not None:
                candidate.selected_location_index = best_index
                candidate.selected_location_manual = False
                candidate.location = location_text
                if location_country:
                    candidate.country = location_country
                candidate.route_distance_km = None
                candidate.route_duration_min = None
                candidate.route_address_text = ""
                candidate.route_locked = False
                candidate.route_quality = ""
                self.temp_routes_by_iid.pop(job.iid, None)

    def _format_ba_location_label(self, location: dict) -> str:
        address = location.get("adresse") or {}
        if not isinstance(address, dict):
            return "Unknown location"
        plz = str(address.get("plz") or address.get("postleitzahl") or "").strip()
        city = str(address.get("ort") or address.get("city") or "").strip()
        region = str(address.get("region") or "").strip()
        parts = [part for part in [plz, city, region] if part]
        return ", ".join(parts) if parts else "Unknown location"

    @staticmethod
    def _location_country(location: dict) -> str:
        address = location.get("adresse") or {}
        if not isinstance(address, dict):
            return ""
        return str(address.get("land") or address.get("country") or "").strip()

    @staticmethod
    def _country_display(country: str) -> str:
        value = str(country or "").strip()
        aliases = {
            "DEUTSCHLAND": "Germany",
            "ESPANA": "Spain",
            "ESPAÑA": "Spain",
            "SPANIEN": "Spain",
        }
        return aliases.get(value.upper(), value)

    def _display_location(self, job: DisplayJob) -> str:
        location = job.location or ""
        if self._has_multiple_ba_locations(job):
            return f"* {location}"
        return location

    def _extract_clean_location_destination(self, job: DisplayJob) -> str:
        first = self._first_ba_location(job)
        address = first.get("adresse") or {}
        if not isinstance(address, dict):
            return self._clean_location_text(job.location, job.country)

        plz = str(address.get("plz") or address.get("postleitzahl") or "").strip()
        city = str(address.get("ort") or address.get("city") or "").strip()
        # BA sometimes puts region into ort: "Weßling, Oberbayern".
        city = city.split(",", 1)[0].strip()
        country = str(address.get("land") or job.country or "Germany").strip()
        if country.upper() == "DEUTSCHLAND":
            country = "Germany"

        if city and plz:
            return f"{plz} {city}, {country}"
        if city:
            return f"{city}, {country}"
        return self._clean_location_text(job.location, job.country)

    @classmethod
    def _clean_location_text(cls, location: str, country: str = "") -> str:
        text = str(location or "").strip()
        if not text:
            return ""
        country_text = cls._country_display(country).strip()
        parts = [part.strip() for part in text.split(",") if part.strip()]
        # Remove work-mode decorations that confuse geocoders.
        city_part = re.sub(r"\s*\((?:hybrid|vor ort|remote|on[- ]?site)\)\s*$", "", parts[0], flags=re.IGNORECASE).strip() if parts else text
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9 -]+", parts[0]) and any(ch.isdigit() for ch in parts[0]):
            city = re.sub(r"\s*\([^)]*\)\s*$", "", parts[1]).strip()
            base = f"{parts[0]} {city}"
        else:
            base = city_part
        return f"{base}, {country_text}" if country_text else base


    def _parse_exact_location_input(self, text: str):
        value = str(text or "").strip()
        if not value:
            return None
        # GeoJSON coordinate order: "lon, lat"
        match = re.match(r"^\s*([+-]?\d+(?:[\.,]\d+)?)\s*,\s*([+-]?\d+(?:[\.,]\d+)?)\s*$", value)
        if match:
            try:
                lon = float(match.group(1).replace(",", "."))
                lat = float(match.group(2).replace(",", "."))
            except ValueError:
                return ("address", value)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return ("coords", lat, lon)
        return ("address", value)

    def _extract_exact_route_destination(self, job: DisplayJob) -> str:
        raw_data = self._load_raw_entry_for_job(job)
        if isinstance(raw_data, dict):
            parsed = self._parse_exact_location_input(str(raw_data.get("exact_location") or raw_data.get("exact_address") or "").strip())
            if parsed:
                if parsed[0] == "coords":
                    return f"coords:{float(parsed[1]):.8f},{float(parsed[2]):.8f}"
                if parsed[0] == "address":
                    address_text = str(parsed[1]).strip()
                    country_text = self._country_display(job.country).strip()
                    if country_text and country_text.casefold() not in address_text.casefold():
                        address_text = f"{address_text}, {country_text}"
                    return address_text

        first = self._first_ba_location(job)
        if first:
            lat = first.get("breite")
            lon = first.get("laenge")
            if lat is not None and lon is not None:
                try:
                    return f"coords:{float(lat):.8f},{float(lon):.8f}"
                except (TypeError, ValueError):
                    pass

        # Fallback for non-BA sources or entries without coordinates.
        return self._extract_exact_address_text(job)

    def _extract_exact_address_text(self, job: DisplayJob) -> str:
        first = self._first_ba_location(job)
        if not first:
            raw_data = self._load_raw_entry_for_job(job)
            if isinstance(raw_data, dict):
                return str(raw_data.get("exact_address") or raw_data.get("exact_location") or "").strip()
            return ""
        address = first.get("adresse") or {}
        if not isinstance(address, dict):
            return ""
        street = address.get("strasse") or address.get("straße") or address.get("street") or ""
        house = address.get("hausnummer") or address.get("houseNumber") or ""
        plz = address.get("plz") or address.get("postleitzahl") or ""
        city = address.get("ort") or address.get("city") or ""
        city = str(city).split(",", 1)[0].strip()
        country = address.get("land") or job.country or "Germany"
        if str(country).upper() == "DEUTSCHLAND":
            country = "Germany"
        street_part = " ".join(part for part in [str(street).strip(), str(house).strip()] if part)
        city_part = " ".join(part for part in [str(plz).strip(), str(city).strip()] if part)
        parts = [part for part in [street_part, city_part, str(country).strip()] if part]
        return ", ".join(parts)


    # ------------------------------------------------------------------
    # AI tool host interface
    # ------------------------------------------------------------------

    def _ai_job_snapshot(self, job: DisplayJob) -> dict:
        return {
            "iid": job.iid,
            "job_id": job.db_id,
            "saved": job.db_id is not None,
            "source": job.source,
            "source_job_id": job.source_job_id,
            "status": job.status,
            "score": job.manual_score,
            "industry": job.branch,
            "title": job.title,
            "display_title": self._display_title(job),
            "company": job.company,
            "location": self._display_location(job),
            "country": self._country_display(job.country),
            "published": job.published_date,
            "term": job.fixed_term,
            "min_salary_k": job.min_salary_k,
            "max_salary_k": job.max_salary_k,
            "distance": self._distance_display(job.route_distance_km, job.route_quality),
            "duration": self._duration_display(job.route_duration_min, job.route_quality),
            "route_quality": job.route_quality,
            "features": self._job_feature_labels(job),
        }

    def _find_display_job_for_ai(self, job_id: int | None = None, iid: str | None = None) -> DisplayJob | None:
        if iid:
            job = self._get_display_job(str(iid))
            if job is not None:
                return job
        if job_id is not None:
            for job in self.current_jobs:
                if job.db_id == int(job_id):
                    return job
            record = self.db.get_job(int(job_id))
            if record is not None:
                return DisplayJob(
                    iid=str(record.id),
                    db_id=record.id,
                    candidate=None,
                    record=record,
                    status=record.status,
                    manual_score=record.manual_score,
                    branch=record.company_rating or "",
                    notes=record.notes or "",
                )
        return None

    def ai_get_visible_jobs(self) -> list[dict]:
        """Return the visible table after all filters as JSON-serializable rows."""
        return [self._ai_job_snapshot(job) for job in self.current_jobs]

    def ai_get_job(self, job_id: int | None = None, iid: str | None = None) -> dict:
        job = self._find_display_job_for_ai(job_id=job_id, iid=iid)
        if job is None:
            raise ValueError("Job not found")
        return self._ai_job_snapshot(job)

    def ai_get_job_details(self, job_id: int | None = None, iid: str | None = None) -> dict:
        job = self._find_display_job_for_ai(job_id=job_id, iid=iid)
        if job is None:
            raise ValueError("Job not found")
        data = self._ai_job_snapshot(job)
        data.update(
            {
                "description": job.formatted_description or job.description,
                "original_description": job.description,
                "formatted_description": job.formatted_description,
                "notes": job.notes,
                "raw_entry": self._raw_entry_text(job),
                "exact_location": self._exact_location_text(job),
                "latest_ai_evaluation": self.db.get_latest_ai_evaluation(job.db_id) if job.db_id is not None else None,
            }
        )
        return data

    def ai_get_lists(self) -> dict:
        reasons = []
        for reason in self.reject_reasons:
            reasons.append(
                {
                    "name": reason,
                    "description": str(self.reject_reason_descriptions.get(reason, "") or ""),
                    "category": str(self.reject_reason_category_by_name.get(reason, "Uncategorized") or "Uncategorized"),
                }
            )
        return {
            "statuses": list(self.status_values),
            "status_categories": self.status_categories,
            "industries": self.db.list_company_ratings(),
            "reject_reasons": reasons,
            "reject_reason_categories": list(self.reject_reason_categories),
            "blacklist": self.db.list_blacklisted_companies(),
            "ai_tool_specs": self.ai_service.available_tool_specs(),
        }

    def ai_get_saved_searches(self) -> dict:
        return dict(self.saved_searches)

    def ai_search_database(self, query: str, limit: int = 20) -> list[dict]:
        query = str(query or "").strip().lower()
        if not query:
            return []
        limit = max(1, min(int(limit or 20), 100))
        # Use the DB broad text search first, then add latest AI evaluation.
        records = self.db.list_jobs(status_filter="all", text_filter=query, hide_rejected=False, show_only_new=False)
        result = []
        for record in records[:limit]:
            job = DisplayJob(
                iid=str(record.id),
                db_id=record.id,
                candidate=None,
                record=record,
                status=record.status,
                manual_score=record.manual_score,
                branch=record.company_rating or "",
                notes=record.notes or "",
            )
            item = self._ai_job_snapshot(job)
            item["latest_ai_evaluation"] = self.db.get_latest_ai_evaluation(record.id)
            result.append(item)
        return result

    def ai_run_search(self, module: str, query: str, location: str, radius_km: int, max_results: int, page: int = 1) -> dict:
        module = (module or "BA").strip()
        if module != "BA":
            raise ValueError(f"Search module is not implemented for AI tools yet: {module}")
        candidates = self.ba_importer.search_page(
            query=str(query or ""),
            location=str(location or ""),
            radius_km=int(radius_km or 40),
            page=int(page or 1),
            size=int(max_results or 25),
        )
        before = set(self.pending_by_iid.keys())
        self._show_search_results(candidates)
        new_iids = [iid for iid in self.pending_by_iid if iid not in before]
        return {
            "module": module,
            "query": query,
            "location": location,
            "radius_km": radius_km,
            "page": page,
            "returned_candidates": len(candidates),
            "new_unsaved_iids": new_iids,
            "visible_jobs": self.ai_get_visible_jobs(),
        }

    def ai_remove_unsaved_results(self, iids: list[str]) -> dict:
        removed = []
        for iid in list(iids or []):
            iid = str(iid)
            if iid in self.pending_by_iid:
                self.pending_by_iid.pop(iid, None)
                self.temp_routes_by_iid.pop(iid, None)
                removed.append(iid)
        self.refresh_jobs()
        return {"removed_iids": removed}

    def ai_add_company_to_blacklist(self, company: str) -> dict:
        company = str(company or "").strip()
        if not company:
            raise ValueError("Company name is empty")
        self.db.add_company_to_blacklist(company)
        self.refresh_jobs()
        return {"company": company, "blacklisted": True}

    def ai_set_job_status(self, job_id: int, status: str, reason: str = "") -> dict:
        job_id = int(job_id)
        status = str(status or "").strip()
        if status not in self.status_values:
            raise ValueError(f"Unknown status: {status}")
        self.db.update_jobs_status([job_id], status, str(reason or ""))
        self.refresh_jobs()
        return {"job_id": job_id, "status": status}

    def ai_calculate_route(self, job_id: int | None = None, iid: str | None = None, exact: bool = False) -> dict:
        job = self._find_display_job_for_ai(job_id=job_id, iid=iid)
        if job is None:
            raise ValueError("Job not found")
        self._calculate_routes_for_iids([job.iid], use_exact=bool(exact))
        return {"job_id": job.db_id, "iid": job.iid, "started": True, "exact": bool(exact)}

    def ai_save_ai_texts(
        self,
        job_id: int,
        ai_name: str,
        provider: str,
        model: str,
        summary: str = "",
        rating: str = "",
        score: int | None = None,
        decision: str = "",
        raw_response: str = "",
        reject_reason: str = "",
        rejection_tags: list[str] | None = None,
    ) -> dict:
        evaluation_id = self.db.save_ai_evaluation(
            job_id=int(job_id),
            ai_name=str(ai_name or "AI"),
            provider=str(provider or ""),
            model=str(model or ""),
            summary=str(summary or ""),
            rating=str(rating or ""),
            score=score,
            decision=str(decision or ""),
            raw_response=str(raw_response or ""),
            reject_reason=str(reject_reason or ""),
            rejection_tags=[str(tag).strip() for tag in (rejection_tags or []) if str(tag).strip()],
        )
        self.refresh_jobs()
        return {"evaluation_id": evaluation_id, "job_id": int(job_id)}

    def ai_update_job(self, job_id: int) -> dict:
        job = self._find_display_job_for_ai(job_id=int(job_id))
        if job is None or job.db_id is None:
            raise ValueError("Saved job not found")
        if job.source.lower() != "ba":
            raise ValueError(f"Update is currently only implemented for BA jobs, not {job.source}")
        candidate = self.ba_importer.fetch_candidate_by_refnr(job.source_job_id)
        _updated_id, _is_new, changed = self.db.add_or_update_job(candidate)
        self.refresh_jobs()
        return {"job_id": int(job_id), "changed": bool(changed)}

    def reload_selected_job_description(self) -> None:
        selection = list(self.tree.selection())
        if len(selection) != 1:
            messagebox.showinfo("Reload description", "Select exactly one direct job.", parent=self)
            return
        job = self._get_display_job(selection[0])
        if job is None or job.source.casefold() != "direct" or not job.url:
            messagebox.showinfo("Reload description", "The selected job is not a direct company-page job with a detail URL.", parent=self)
            return

        candidate = job.candidate or JobCandidate(
            source=job.source,
            source_job_id=job.source_job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            country=job.country,
            url=job.url,
            description=job.description,
        )

        def worker() -> None:
            try:
                description = self.company_watch_adapters.fetch_description(candidate)
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Reload description", str(exc), parent=self))
                return

            def apply() -> None:
                if job.candidate is not None:
                    job.candidate.description = description
                    job.candidate.formatted_description = ""
                if job.record is not None:
                    job.record.description = description
                    job.record.formatted_description = ""
                if job.db_id is not None:
                    self.db.save_job_description(job.db_id, description)
                self.temp_formatted_descriptions_by_iid.pop(job.iid, None)
                if self.selected_iid == job.iid:
                    self._set_markdownish_content(self.description_text, description)
                messagebox.showinfo("Reload description", "The job description was reloaded from the detail page.", parent=self)

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def show_duplicate_candidates_for_selected_job(self) -> None:
        job = self._get_display_job(self.selected_iid)
        if job is None:
            return
        if job.db_id is not None:
            matches = self.db.list_duplicate_relations(job.db_id)
        else:
            matches = list(self.pending_duplicate_matches_by_iid.get(job.iid, []))
        if not matches:
            messagebox.showinfo("Duplicate candidates", "No duplicate candidates are stored for this job.")
            return

        dialog = tk.Toplevel(self)
        dialog.title("Duplicate candidates")
        dialog.geometry("1200x760")
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="POTENTIALLY NEW JOB", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        ttk.Label(frame, text=f"{self._display_id(job)} | {job.title} | {job.company} | {job.location} | {job.status}").pack(anchor=tk.W, pady=(2, 8))

        columns = ("score", "id", "title", "company", "location", "status", "source", "decision")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=8, selectmode="browse")
        widths = {"score":70,"id":70,"title":300,"company":180,"location":160,"status":130,"source":90,"decision":110}
        for col in columns:
            tree.heading(col, text=col.title())
            tree.column(col, width=widths[col], stretch=col in {"title","company","location"})
        for match in matches:
            tree.insert("", tk.END, iid=str(match.get("id")), values=(f"{float(match.get('score',0)):.0f}%", match.get("id"), match.get("title"), match.get("company"), match.get("location"), match.get("status"), match.get("source"), match.get("decision","candidate")))
        tree.pack(fill=tk.X, pady=(0, 8))

        panes = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        left = ttk.LabelFrame(panes, text="Potentially new job description", padding=6)
        right = ttk.LabelFrame(panes, text="Selected existing candidate description", padding=6)
        panes.add(left, weight=1); panes.add(right, weight=1)
        new_text = tk.Text(left, wrap=tk.WORD); new_text.pack(fill=tk.BOTH, expand=True)
        cand_text = tk.Text(right, wrap=tk.WORD); cand_text.pack(fill=tk.BOTH, expand=True)
        new_text.insert("1.0", job.description or "(no description)"); new_text.configure(state=tk.DISABLED)

        def selected_match():
            sel = tree.selection()
            if not sel: return None
            sid = str(sel[0])
            return next((m for m in matches if str(m.get("id")) == sid), None)
        def update_description(_event=None):
            match = selected_match()
            cand_text.configure(state=tk.NORMAL); cand_text.delete("1.0", tk.END)
            cand_text.insert("1.0", (match or {}).get("description") or "(no description)"); cand_text.configure(state=tk.DISABLED)
        tree.bind("<<TreeviewSelect>>", update_description)
        if tree.get_children(): tree.selection_set(tree.get_children()[0]); update_description()

        buttons = ttk.Frame(frame); buttons.pack(fill=tk.X, pady=(8,0))
        def mark_duplicate():
            match = selected_match()
            if not match: return
            if job.db_id is None:
                job_id = self._save_pending_job(job.iid, self._rejected_by_me_status(), branch=job.branch, notes=job.notes, reject_reason="duplicate")
            else:
                job_id = job.db_id
                self.db.update_jobs_status([job_id], self._rejected_by_me_status(), "duplicate")
            if job_id is not None:
                self.db.set_duplicate_info(job_id, 100.0, int(match["id"]))
                self.db.save_duplicate_relation(job_id, int(match["id"]), 100.0, "duplicate")
            dialog.destroy(); self.refresh_jobs()
        def not_duplicate():
            match = selected_match()
            if not match: return
            if job.db_id is None:
                job_id = self._save_pending_job(job.iid, job.status, branch=job.branch, notes=job.notes)
            else:
                job_id = job.db_id
            if job_id is not None:
                self.db.save_duplicate_relation(job_id, int(match["id"]), float(match.get("score",0)), "not_duplicate")
                remaining = [m for m in self.db.list_duplicate_relations(job_id) if m.get("decision") == "candidate"]
                best = max((float(m.get("score",0)) for m in remaining), default=0.0)
                self.db.set_duplicate_info(job_id, best, int(remaining[0]["id"]) if remaining else None)
            dialog.destroy(); self.refresh_jobs()
        ttk.Button(buttons, text="Mark selected as duplicate", command=mark_duplicate).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Not a duplicate", command=not_duplicate).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Context menu / blacklist / companies
    # ------------------------------------------------------------------

    def _rebuild_context_menu(self) -> None:
        self.context_menu.delete(0, tk.END)
        ai_menu = tk.Menu(self.context_menu, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        ai_menu.add_command(label="Shallow screen selected jobs", command=self.ai_screen_selected_jobs)
        ai_menu.add_command(label="Deep screen selected jobs", command=self.ai_analyze_selected_jobs_detail)
        selected_jobs = self._selected_jobs_for_ai()
        job = self._get_display_job(self.selected_iid)
        if len(selected_jobs) == 1:
            ai_menu.add_separator()
            ai_menu.add_command(label="Ask AI", command=self.ai_ask_about_selected_job)
        status_menu = tk.Menu(self.context_menu, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        for status in self.status_values:
            status_menu.add_command(label=status, command=lambda value=status: self.set_status_for_selected(value))

        self.context_menu.add_command(label="Reject", command=self.reject_selected_jobs)
        self.context_menu.add_separator()
        self.context_menu.add_cascade(label="Set status", menu=status_menu)
        self.context_menu.add_command(label="Update", command=self.update_selected_job)
        if job is not None and job.source.casefold() == "direct" and job.url:
            self.context_menu.add_command(label="Reload job description", command=self.reload_selected_job_description)
        self.context_menu.add_separator()
        if len(self.tree.selection()) == 1:
            self.context_menu.add_command(label="Edit entry", command=self.edit_selected_entry_dialog)
        self.context_menu.add_cascade(label="AI", menu=ai_menu)
        self.context_menu.add_separator()

        industry_menu = tk.Menu(self.context_menu, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        industry_menu.add_command(label="(add new)", command=self.add_new_industry_for_selected_companies)
        industry_menu.add_separator()
        for industry in self.db.list_company_ratings():
            industry_menu.add_command(label=industry, command=lambda value=industry: self.set_industry_for_selected_companies(value))
        self.context_menu.add_cascade(label="Set industry", menu=industry_menu)

        route_menu = tk.Menu(self.context_menu, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        route_menu.add_command(label="Calculate route (location)", command=lambda: self.calculate_routes_for_selection(use_exact=False))
        route_menu.add_command(label="Calculate route (exact address)", command=lambda: self.calculate_routes_for_selection(use_exact=True))
        if job is not None and self._has_multiple_ba_locations(job):
            route_menu.add_separator()
            location_menu = tk.Menu(route_menu, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
            selected_index = job.selected_location_index
            for index, location in enumerate(self._ba_locations(job)):
                label = self._format_ba_location_label(location)
                if index == selected_index:
                    label = f"✓ {label}"
                location_menu.add_command(label=label, command=lambda i=index: self.set_selected_location_for_selected_job(i))
            route_menu.add_cascade(label="Set location", menu=location_menu)
        route_menu.add_separator()
        alias_label = "Edit alias" if job is not None and self._location_alias_for_job(job) else "Set alias"
        route_menu.add_command(label=alias_label, command=self.set_or_edit_location_alias_for_selected_job)
        route_menu.add_separator()
        route_menu.add_command(label="Delete route cache entry", command=self.delete_route_cache_for_selection)
        self.context_menu.add_cascade(label="Route/Location", menu=route_menu)
        self.context_menu.add_separator()

        copy_menu = tk.Menu(self.context_menu, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        copy_menu.add_command(label="All", command=lambda: self.copy_selected_job_field("all"))
        copy_menu.add_command(label="Title", command=lambda: self.copy_selected_job_field("title"))
        copy_menu.add_command(label="Company", command=lambda: self.copy_selected_job_field("company"))
        copy_menu.add_command(label="Location", command=lambda: self.copy_selected_job_field("location"))
        if job is not None and self._has_exact_location_for_job(job):
            copy_menu.add_command(label="Exact location", command=lambda: self.copy_selected_job_field("exact_location"))
        copy_menu.add_command(label="Raw data", command=lambda: self.copy_selected_job_field("raw_data"))
        self.context_menu.add_cascade(label="Copy", menu=copy_menu)
        self.context_menu.add_separator()
        if job is not None and job.url:
            self.context_menu.add_command(label="Open URL", command=self.open_selected_job_url)
            self.context_menu.add_separator()
        self.context_menu.add_command(label="Show duplicate candidates", command=self.show_duplicate_candidates_for_selected_job)
        self.context_menu.add_command(label="Show raw entry", command=self.show_raw_json_for_selected_job)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Add company to blacklist", command=self.add_selected_company_to_blacklist)




    # ------------------------------------------------------------------
    # AI agent search and screening (MVP 0.6.4)
    # ------------------------------------------------------------------

    def _agent_call_on_ui_thread(self, callback):
        """Run a callback on Tk's main thread and return its result."""
        done = threading.Event()
        result: dict[str, object] = {}

        def run() -> None:
            try:
                result["value"] = callback()
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        self.after(0, run)
        if not done.wait(timeout=120):
            raise TimeoutError("Timed out while waiting for the JobRadar GUI thread.")
        error = result.get("error")
        if isinstance(error, Exception):
            raise error
        return result.get("value")

    def _agent_search_ba_page(
        self,
        query: str,
        location: str,
        radius_km: int,
        page: int,
        size: int,
    ) -> list[JobCandidate]:
        return self.ba_importer.search_page(
            query=query,
            location=location,
            radius_km=radius_km,
            page=page,
            size=max(1, min(50, size)),
        )

    def _agent_publish_candidates(self, candidates: list[JobCandidate], page: int, first_page: bool) -> list[dict]:
        before = set(self.pending_by_iid)
        self._show_search_results(
            candidates,
            append_pending=not first_page,
            page=page,
            normalized_params=self._normalize_search_session_params(self._current_search_params()),
        )
        if first_page:
            new_iids = list(self.pending_by_iid)
        else:
            new_iids = [iid for iid in self.pending_by_iid if iid not in before]
        rows = []
        for iid in new_iids:
            job = self._get_display_job(iid)
            if job is not None:
                rows.append(self._ai_screening_row(job))
        return rows

    def _record_agent_usage(self, usage: dict, operation: str) -> None:
        window = self.agent_window
        if not window or not usage:
            return
        totals = window.setdefault("token_totals", {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        for key in totals:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
        token_var = window.get("token_var")
        if token_var is not None:
            text = (f"Run tokens: in {totals['input_tokens']:,} | cached {totals['cached_tokens']:,} | "
                    f"out {totals['output_tokens']:,} | total {totals['total_tokens']:,} · "
                    f"last {operation}: {self._format_token_usage(usage).replace('Tokens: ', '')}")
            self.after(0, lambda t=text, v=token_var: v.set(t))

    def _load_agent_search_history(self, limit: int = 60) -> list[dict]:
        try:
            raw = self.db.get_setting("ai.agent_search_history", "[]")
            data = json.loads(raw)
            if not isinstance(data, list):
                return []
            clean = [item for item in data if isinstance(item, dict)]
            return clean[-max(1, int(limit)):]
        except Exception:
            return []

    def _record_agent_search_outcome(self, outcome: dict) -> None:
        if not isinstance(outcome, dict):
            return
        record = dict(outcome)
        record["recorded_at"] = datetime.now().isoformat(timespec="seconds")
        record["location"] = self.location_var.get().strip()
        try:
            record["radius_km"] = int(self.radius_var.get())
        except (TypeError, ValueError, tk.TclError):
            record["radius_km"] = None
        total = sum(int(record.get(key, 0) or 0) for key in ("green", "orange", "red"))
        record["evaluated_jobs"] = total
        if total:
            record["quality_score"] = round(
                (3 * int(record.get("green", 0) or 0) + int(record.get("orange", 0) or 0)) / (3 * total),
                3,
            )
        else:
            record["quality_score"] = 0.0
        history = self._load_agent_search_history(limit=199)
        history.append(record)
        self.db.set_setting("ai.agent_search_history", json.dumps(history[-200:], ensure_ascii=False))

    def _agent_plan_searches(
        self,
        model_config: AiModelConfig,
        feedback: list[dict] | None = None,
        additional_instructions: str = "",
    ) -> dict:
        result = self.ai_service.plan_agent_searches(
            model_config=model_config,
            user_config=self._ai_user_config_with_industries(),
            location=self.location_var.get().strip(),
            radius_km=int(self.radius_var.get()),
            saved_searches=dict(self.saved_searches),
            current_query=self.query_var.get().strip(),
            feedback=(self._load_agent_search_history() + list(feedback or []))[-80:],
            additional_instructions=additional_instructions,
        )
        self._record_agent_usage(self.ai_service.consume_last_usage(), "planning")
        return result

    def _agent_screen_rows(self, rows: list[dict], model_config: AiModelConfig) -> dict:
        iids = [str(row.get("iid") or "") for row in rows if isinstance(row, dict) and str(row.get("iid") or "")]
        task_id = self._agent_call_on_ui_thread(
            lambda: self._start_background_ai_task("screening", iids, f"Agent screening {len(iids)} job(s)")
        )
        try:
            result = self.ai_service.screen_jobs_from_table(rows, model_config, self._ai_user_config_with_industries())
            self._record_agent_usage(self.ai_service.consume_last_usage(), f"screening {len(rows)} job(s)")
            self._agent_call_on_ui_thread(lambda: self._finish_background_ai_task(task_id, True))
            return result
        except Exception:
            self._agent_call_on_ui_thread(lambda: self._finish_background_ai_task(task_id, False))
            raise

    def _agent_apply_screening(self, items: list[dict]) -> dict:
        counts = {"marked": 0, "potential": 0, "maybe": 0, "unlikely": 0}
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("iid") or "").strip()
            score_value = self._coerce_ai_screening_score(item.get("score"))
            decision = self._screening_decision_from_score(score_value) or str(item.get("decision") or "").strip().lower()
            if not iid or decision not in {"potential", "maybe", "unlikely"}:
                continue
            job = self._get_display_job(iid)
            if job is None:
                continue
            self.ai_screening_marks[iid] = decision
            if score_value is not None:
                self.temp_ai_screening_scores_by_iid[iid] = score_value
            self.temp_ai_screening_details_by_iid[iid] = {
                "score": score_value,
                "decision": decision,
                "reason": str(item.get("reason") or item.get("exclusion_reason") or "").strip(),
                "reject_reason": str(item.get("reject_reason") or item.get("exclusion_reason") or "").strip(),
                "rejection_tags": [str(tag).strip() for tag in item.get("rejection_tags", []) if str(tag).strip()] if isinstance(item.get("rejection_tags"), list) else [],
                "confidence": str(item.get("confidence") or "").strip(),
                "hard_exclusion": bool(item.get("hard_exclusion")),
                "interesting_signals": [str(signal).strip() for signal in item.get("interesting_signals", []) if str(signal).strip()] if isinstance(item.get("interesting_signals"), list) else [],
            }
            self._apply_ai_detected_flags(iid, item, job)
            if self.tree.exists(iid):
                self.tree.item(iid, tags=self._row_tags(job))
                self._update_dirty_indicators_for_iid(iid)
                self._refresh_ai_cell(iid)
            counts["marked"] += 1
            counts[decision] += 1
        self.status_label_var.set(
            f"AI agent marked {counts['marked']} jobs temporarily: "
            f"{counts['potential']} green, {counts['maybe']} orange, {counts['unlikely']} red."
        )
        return counts

    def _agent_build_detail_payloads(self, iids: list[str]) -> list[dict]:
        payloads: list[dict] = []
        for iid in iids:
            job = self._get_display_job(str(iid))
            if job is not None:
                payloads.append(self._ai_detail_payload(job))
        return payloads

    def _agent_analyze_details(self, payloads: list[dict], model_config: AiModelConfig) -> dict:
        iids = [str(payload.get("iid") or "") for payload in payloads if isinstance(payload, dict) and str(payload.get("iid") or "")]
        task_id = self._agent_call_on_ui_thread(
            lambda: self._start_background_ai_task("detail", iids, f"Agent detail analysis {len(iids)} job(s)")
        )
        try:
            result = self.ai_service.analyze_jobs_in_detail(
                payloads,
                model_config,
                self._ai_user_config_with_industries(),
            )
            self._record_agent_usage(self.ai_service.consume_last_usage(), f"detail {len(payloads)} job(s)")
            self._agent_call_on_ui_thread(lambda: self._finish_background_ai_task(task_id, True))
            return result
        except Exception:
            self._agent_call_on_ui_thread(lambda: self._finish_background_ai_task(task_id, False))
            raise

    def _agent_apply_details(self, items: list[dict], model_config: AiModelConfig, raw_response: str) -> dict:
        counts = {"updated": 0, "potential": 0, "maybe": 0, "unlikely": 0}
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("iid") or "").strip()
            if not iid:
                continue
            job = self._get_display_job(iid)
            if job is None:
                continue
            score_value = self._coerce_ai_score(item.get("score"))
            decision = str(item.get("decision") or "").strip().lower()
            if score_value is not None:
                if score_value >= 70:
                    decision = "potential"
                elif score_value >= 45:
                    decision = "maybe"
                else:
                    decision = "unlikely"
            if decision not in {"potential", "maybe", "unlikely"}:
                decision = "maybe"
            self.temp_ai_evaluations_by_iid[iid] = {
                "ai_name": model_config.name,
                "provider": model_config.provider,
                "model": model_config.model,
                "created_at": utc_now_iso(),
                "summary": str(item.get("summary") or ""),
                "rating": str(item.get("rating") or ""),
                "score": score_value,
                "decision": decision,
                "reject_reason": str(item.get("reject_reason") or "").strip(),
                "rejection_tags": [str(tag).strip() for tag in item.get("rejection_tags", []) if str(tag).strip()] if isinstance(item.get("rejection_tags"), list) else [],
                "raw_response": raw_response,
            }
            self.ai_screening_marks[iid] = decision
            self.temp_ai_screening_scores_by_iid.pop(iid, None)
            self.temp_ai_screening_details_by_iid.pop(iid, None)
            self._apply_ai_industry_suggestion(iid, item, job)
            self._update_dirty_indicators_for_iid(iid)
            self._refresh_ai_cell(iid)
            counts["updated"] += 1
            counts[decision] += 1
        if self.selected_iid:
            selected = self._get_display_job(self.selected_iid)
            if selected is not None:
                self._load_ai_details_for_job(selected)
        self.status_label_var.set(
            f"AI agent detail analysis updated {counts['updated']} jobs temporarily: "
            f"{counts['potential']} green, {counts['maybe']} orange, {counts['unlikely']} red."
        )
        return counts

    def open_ai_agent_window(self) -> None:
        existing = getattr(self, "agent_window", None)
        if isinstance(existing, dict):
            dialog = existing.get("dialog")
            try:
                if dialog is not None and bool(dialog.winfo_exists()):
                    dialog.deiconify()
                    dialog.lift()
                    dialog.focus_force()
                    return
            except Exception:
                pass

        models = self._enabled_ai_model_configs()
        if not models:
            messagebox.showwarning(
                "AI Agent",
                "No enabled AI model is configured in Settings → AI.",
                parent=self,
            )
            return

        default_model = self._default_ai_model_config("agent") or models[0]
        dialog = tk.Toplevel(self)
        dialog.title("AI Agent")
        dialog.geometry("940x820")
        dialog.transient(self)
        self._center_child_window(dialog, width=940, height=820)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        settings = ttk.LabelFrame(frame, text="Run settings", padding=10)
        settings.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(settings, text="Planning model:").grid(row=0, column=0, sticky=tk.W)
        model_var = tk.StringVar(value=default_model.name)
        model_combo = ttk.Combobox(
            settings,
            textvariable=model_var,
            values=[item.name for item in models],
            state="readonly",
            width=30,
        )
        model_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 16))
        agent_token_var = tk.StringVar(value="Run tokens: in 0 | cached 0 | out 0 | total 0")
        ttk.Label(settings, textvariable=agent_token_var).grid(row=2, column=0, columnspan=8, sticky=tk.W, pady=(8, 0))

        ttk.Label(settings, text="Target jobs:").grid(row=0, column=2, sticky=tk.W)
        target_var = tk.IntVar(value=5)
        ttk.Spinbox(settings, from_=1, to=100, textvariable=target_var, width=7).grid(row=0, column=3, sticky=tk.W, padx=(6, 16))

        ttk.Label(settings, text="Successful searches:").grid(row=0, column=4, sticky=tk.W)
        searches_var = tk.IntVar(value=20)
        ttk.Spinbox(settings, from_=1, to=200, textvariable=searches_var, width=7).grid(row=0, column=5, sticky=tk.W, padx=(6, 12))

        ttk.Label(settings, text="Maximum attempts:").grid(row=0, column=6, sticky=tk.W)
        attempts_var = tk.IntVar(value=40)
        ttk.Spinbox(settings, from_=1, to=500, textvariable=attempts_var, width=7).grid(row=0, column=7, sticky=tk.W, padx=(6, 0))

        detail_var = tk.BooleanVar(value=True)
        route_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="Perform detail analysis", variable=detail_var).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Checkbutton(settings, text="Calculate routes", variable=route_var).grid(row=1, column=2, columnspan=2, sticky=tk.W, pady=(8, 0))

        instruction_frame = ttk.LabelFrame(frame, text="Additional instructions for this run", padding=8)
        instruction_frame.pack(fill=tk.X, pady=(0, 8))
        instruction_history = tk.Text(
            instruction_frame,
            height=3,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 9),
        )
        instruction_history.pack(fill=tk.X, pady=(0, 5))
        instruction_input_frame = ttk.Frame(instruction_frame)
        instruction_input_frame.pack(fill=tk.X)
        instruction_input = tk.Text(
            instruction_input_frame, height=4, wrap=tk.WORD,
            background=DARK_THEME["entry_bg"], foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"], relief=tk.FLAT, font=("Segoe UI", 9),
        )
        instruction_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        add_instruction_button = ttk.Button(instruction_input_frame, text="Add")
        add_instruction_button.pack(side=tk.LEFT, padx=(6, 0))
        instructions: list[str] = []

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(0, 8))
        start_button = ttk.Button(button_row, text="Start")
        start_button.pack(side=tk.LEFT)
        stop_button = ttk.Button(button_row, text="Stop", state=tk.DISABLED)
        stop_button.pack(side=tk.LEFT, padx=(8, 0))
        reset_agent_button = ttk.Button(button_row, text="Reset")
        reset_agent_button.pack(side=tk.LEFT, padx=(8, 0))

        status_frame = ttk.LabelFrame(frame, text="Status", padding=8)
        status_frame.pack(fill=tk.X, pady=(0, 8))
        status_var = tk.StringVar(value="Idle")
        status_label = ttk.Label(status_frame, textvariable=status_var, font=("Segoe UI", 10, "bold"))
        status_label.pack(anchor=tk.W)

        log_frame = ttk.LabelFrame(frame, text="Log", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)
        log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_text.yview)
        log_text.configure(yscrollcommand=log_scroll.set)
        log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        window = {
            "dialog": dialog,
            "model_var": model_var,
            "model_combo": model_combo,
            "target_var": target_var,
            "searches_var": searches_var,
            "attempts_var": attempts_var,
            "detail_var": detail_var,
            "route_var": route_var,
            "start_button": start_button,
            "stop_button": stop_button,
            "reset_button": reset_agent_button,
            "status_var": status_var,
            "log_text": log_text,
            "token_var": agent_token_var,
            "token_totals": {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "instruction_frame": instruction_frame,
            "instruction_input_frame": instruction_input_frame,
            "instruction_input": instruction_input,
            "instruction_history": instruction_history,
            "instructions": instructions,
        }
        self.agent_window = window

        def append_log(message: str) -> None:
            text = str(message or "").strip()
            if not text:
                return
            try:
                log_text.configure(state=tk.NORMAL)
                timestamp = datetime.now().strftime("%H:%M:%S")
                log_text.tag_configure("current_search", font=("Segoe UI", 10, "bold"))
                log_text.insert(tk.END, f"[{timestamp}] ")
                if text.startswith("CURRENT SEARCH:"):
                    log_text.insert(tk.END, text + "\n", ("current_search",))
                else:
                    log_text.insert(tk.END, text + "\n")
                log_text.configure(state=tk.DISABLED)
                log_text.see(tk.END)
            except tk.TclError:
                pass

        def add_instruction(_event=None):
            text = instruction_input.get("1.0", tk.END).strip()
            if not text:
                return "break"
            instructions.append(text)
            instruction_history.configure(state=tk.NORMAL)
            instruction_history.insert(tk.END, f"• {text}\n")
            instruction_history.configure(state=tk.DISABLED)
            instruction_history.see(tk.END)
            instruction_input.delete("1.0", tk.END)
            return "break"

        def set_running(running: bool) -> None:
            start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
            stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)
            model_combo.configure(state=tk.DISABLED if running else "readonly")
            if running:
                instruction_input_frame.pack_forget()
            elif not instruction_input_frame.winfo_ismapped():
                instruction_input_frame.pack(fill=tk.X)

        def on_status(message: str) -> None:
            self.after(0, lambda value=str(message): status_var.set(value))

        def on_log(message: str) -> None:
            self.after(0, lambda value=str(message): append_log(value))

        def on_done(message: str) -> None:
            def finish() -> None:
                append_log(message)
                set_running(False)
                if "WARNING:" in str(message):
                    messagebox.showwarning("AI Agent completed with warnings", message, parent=dialog)
                else:
                    messagebox.showinfo("AI Agent", message, parent=dialog)
            self.after(0, finish)

        def on_error(exc: Exception) -> None:
            def fail() -> None:
                append_log(f"Error: {exc}")
                set_running(False)
                messagebox.showerror("AI Agent", str(exc), parent=dialog)
            self.after(0, fail)

        def start_agent() -> None:
            model = self._ai_model_config_by_name(model_var.get())
            if model is None:
                messagebox.showwarning("AI Agent", "Please select an enabled AI model.", parent=dialog)
                return
            try:
                target_jobs = int(target_var.get())
                maximum_searches = int(searches_var.get())
                maximum_search_attempts = int(attempts_var.get())
                if target_jobs < 1 or maximum_searches < 1 or maximum_search_attempts < 1:
                    raise ValueError
            except (TypeError, ValueError, tk.TclError):
                messagebox.showwarning("AI Agent", "Target jobs, successful searches and maximum attempts must be positive integers.", parent=dialog)
                return

            self._remember_ai_model_choice(model.name, "agent")
            screening_model = self._default_ai_model_config("screening")
            if screening_model is None:
                messagebox.showwarning(
                    "AI Agent",
                    "No enabled default model is configured for AI screening in Settings → AI.",
                    parent=dialog,
                )
                return
            detail_model = self._default_ai_model_config("detailed_analysis") if bool(detail_var.get()) else None
            if bool(detail_var.get()) and detail_model is None:
                messagebox.showwarning(
                    "AI Agent",
                    "Detail analysis is enabled, but no default model is configured for detailed analysis in Settings → AI.",
                    parent=dialog,
                )
                return
            query = self.query_var.get().strip()
            location = self.location_var.get().strip()
            try:
                radius_km = int(self.radius_var.get())
                results_per_search = max(1, min(50, int(self.max_results_var.get())))
            except (TypeError, ValueError, tk.TclError):
                messagebox.showwarning("AI Agent", "Radius and Max in the main search bar must be integers.", parent=dialog)
                return
            if not location or radius_km < 1:
                messagebox.showwarning(
                    "AI Agent",
                    "Enter a location and positive radius in the main search bar first. The agent plans its own search terms.",
                    parent=dialog,
                )
                return

            pending_instruction = instruction_input.get("1.0", tk.END).strip()
            if pending_instruction:
                add_instruction()
            additional_instructions = "\n".join(f"- {item}" for item in instructions)

            config = AgentRunConfig(
                planning_model=model,
                screening_model=screening_model,
                detail_model=detail_model,
                query=query,
                location=location,
                radius_km=radius_km,
                results_per_search=results_per_search,
                target_jobs=target_jobs,
                maximum_searches=maximum_searches,
                maximum_search_attempts=maximum_search_attempts,
                perform_detail_analysis=bool(detail_var.get()),
                calculate_routes=bool(route_var.get()),
                additional_instructions=additional_instructions,
            )
            window["token_totals"] = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            agent_token_var.set("Run tokens: in 0 | cached 0 | out 0 | total 0")
            append_log("Starting BA search and AI screening...")
            set_running(True)
            started = self.agent_controller.start(
                config,
                on_status=on_status,
                on_log=on_log,
                on_done=on_done,
                on_error=on_error,
            )
            if not started:
                set_running(False)
                messagebox.showinfo("AI Agent", "The agent is already running.", parent=dialog)

        def stop_agent() -> None:
            if self.agent_controller.is_running:
                append_log("Stop requested. The agent will stop after the current step.")
                status_var.set("Stopping...")
                self.agent_controller.request_stop()

        def reset_agent_window() -> None:
            if self.agent_controller.is_running:
                messagebox.showinfo("AI Agent", "Stop the running agent before resetting the window.", parent=dialog)
                return
            model_var.set(default_model.name)
            target_var.set(5); searches_var.set(20); attempts_var.set(40)
            detail_var.set(True); route_var.set(False)
            instructions.clear()
            instruction_input.delete("1.0", tk.END)
            instruction_history.configure(state=tk.NORMAL); instruction_history.delete("1.0", tk.END); instruction_history.configure(state=tk.DISABLED)
            log_text.configure(state=tk.NORMAL); log_text.delete("1.0", tk.END); log_text.configure(state=tk.DISABLED)
            status_var.set("Idle")
            window["token_totals"] = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            agent_token_var.set("Run tokens: in 0 | cached 0 | out 0 | total 0")
            set_running(False)
            append_log("Ready. Add optional instructions above, then start the agent. Search results remain temporary.")

        def close_agent_window() -> None:
            # Keep the controller and complete window state alive. The toolbar button
            # restores this exact window, analogous to hidden detail-analysis windows.
            dialog.withdraw()

        add_instruction_button.configure(command=add_instruction)
        instruction_input.bind("<Control-Return>", add_instruction)
        start_button.configure(command=start_agent)
        stop_button.configure(command=stop_agent)
        reset_agent_button.configure(command=reset_agent_window)
        dialog.protocol("WM_DELETE_WINDOW", close_agent_window)
        append_log("Ready. Add optional instructions above, then start the agent. Search results remain temporary.")

    def _center_child_window(self, dialog: tk.Toplevel, width: int | None = None, height: int | None = None) -> None:
        try:
            self.update_idletasks()
            dialog.update_idletasks()
            w = width or dialog.winfo_width() or 800
            h = height or dialog.winfo_height() or 600
            x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 2)
            dialog.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _bind_detail_text_context_menus(self) -> None:
        for widget, name in (
            (getattr(self, "description_text", None), "Job description"),
            (getattr(self, "ai_summary_text", None), "AI summary"),
            (getattr(self, "notes_text", None), "Notes"),
            (getattr(self, "ai_rating_text", None), "AI rating"),
        ):
            if widget is not None:
                widget.bind("<Button-3>", lambda event, field=name: self._show_text_field_context_menu(event, field))
                widget.bind("<Control-f>", lambda event, target=widget: self._search_in_text_widget(target))
                widget.bind("<Control-F>", lambda event, target=widget: self._search_in_text_widget(target))

    def _search_in_text_widget(self, widget: tk.Text) -> str:
        term = simpledialog.askstring("Find text", "Search for:", parent=self)
        if term is None:
            return "break"
        widget.tag_remove("text_search_match", "1.0", tk.END)
        term = term.strip()
        if not term:
            return "break"
        widget.tag_configure("text_search_match", background="#d6a700", foreground="#101010")
        start = "1.0"
        first_index = None
        while True:
            index = widget.search(term, start, stopindex=tk.END, nocase=True)
            if not index:
                break
            if first_index is None:
                first_index = index
            end = f"{index}+{len(term)}c"
            widget.tag_add("text_search_match", index, end)
            start = end
        if first_index is None:
            messagebox.showinfo("Find text", f'"{term}" was not found.', parent=self)
        else:
            widget.see(first_index)
            widget.mark_set(tk.INSERT, first_index)
        # simpledialog/messagebox may leave the toplevel focused rather than the Text.
        # Restore it explicitly so Ctrl+F can immediately be pressed again.
        try:
            widget.focus_force()
        except tk.TclError:
            pass
        return "break"

    def _highlight_active_include_terms(self) -> None:
        widgets = [
            getattr(self, "description_text", None),
            getattr(self, "ai_summary_text", None),
            getattr(self, "notes_text", None),
            getattr(self, "ai_rating_text", None),
        ]
        search_texts = bool(getattr(self, "filter_search_texts_var", None) and self.filter_search_texts_var.get())
        terms = self._split_filter_terms(self.include_filter_var.get()) if search_texts and hasattr(self, "include_filter_var") else []
        for widget in widgets:
            if widget is None:
                continue
            widget.tag_remove("include_filter_match", "1.0", tk.END)
            if not terms:
                continue
            widget.tag_configure("include_filter_match", background="#d6a700", foreground="#101010")
            for term in terms:
                start = "1.0"
                while True:
                    index = widget.search(term, start, stopindex=tk.END, nocase=True)
                    if not index:
                        break
                    end = f"{index}+{len(term)}c"
                    widget.tag_add("include_filter_match", index, end)
                    start = end

    def _show_text_field_context_menu(self, event: tk.Event, field_name: str) -> None:
        menu = tk.Menu(self, tearoff=False, bg=DARK_THEME["panel"], fg=DARK_THEME["fg"], activebackground=DARK_THEME["select_bg"], activeforeground=DARK_THEME["select_fg"])
        menu.add_command(label="Ask AI", command=lambda f=field_name: self.ai_ask_about_selected_job(field_name=f))
        if field_name == "Job description":
            menu.add_separator()
            menu.add_command(label="Re-format with AI", command=self.ai_reformat_selected_job_description)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _ai_symbol_tooltip_text(self, symbol: str) -> str:
        return {
            "🕑": "AI analysis is currently running",
            "⚠️": "No job description is available",
            "✅": "Detailed AI rating is available",
            "ℹ️": "Preliminary AI screening or summary is available",
            "-": "No AI analysis is available",
        }.get(str(symbol or ""), "")

    def _hide_ai_tooltip(self) -> None:
        window = getattr(self, "_ai_tooltip_window", None)
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
        self._ai_tooltip_window = None
        self._ai_tooltip_cell = None

    def _on_job_tree_motion(self, event: tk.Event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        column = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        # AI is the third displayed column (#3).
        if region != "cell" or column != "#3" or not row:
            self._hide_ai_tooltip()
            return
        cell = (row, column)
        if self._ai_tooltip_cell == cell and self._ai_tooltip_window is not None:
            return
        values = self.tree.item(row, "values")
        symbol = str(values[2]) if len(values) > 2 else ""
        text = self._ai_symbol_tooltip_text(symbol)
        self._hide_ai_tooltip()
        if not text:
            return
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        ttk.Label(tip, text=text, padding=(6, 3)).pack()
        tip.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")
        self._ai_tooltip_window = tip
        self._ai_tooltip_cell = cell

    def _ai_status_symbol(self, job: DisplayJob) -> str:
        if self.ai_running_by_iid.get(job.iid):
            return "🕑"
        if not str(job.formatted_description or job.description or "").strip():
            return "⚠️"
        evaluation = self.temp_ai_evaluations_by_iid.get(job.iid)
        if not evaluation and job.db_id is not None:
            evaluation = self.db.get_latest_ai_evaluation(job.db_id) or {}
        if isinstance(evaluation, dict) and str(evaluation.get("rating") or "").strip():
            return "✅"
        if job.iid in self.temp_ai_screening_details_by_iid:
            return "ℹ️"
        if isinstance(evaluation, dict) and str(evaluation.get("summary") or "").strip():
            return "ℹ️"
        return "-"

    def _refresh_ai_cell(self, iid: str) -> None:
        if not hasattr(self, "tree") or not self.tree.exists(iid):
            return
        job = self._get_display_job(iid)
        if job is None:
            return
        values = list(self.tree.item(iid, "values"))
        if len(values) > 2:
            values[2] = self._ai_status_symbol(job)
            self.tree.item(iid, values=values)

    def _start_background_ai_task(self, kind: str, iids: list[str], description: str) -> int:
        self._background_task_counter += 1
        task_id = self._background_task_counter
        if kind in {"screening", "detail"}:
            for iid in iids:
                self.ai_running_by_iid.setdefault(iid, set()).add(kind)
                self._refresh_ai_cell(iid)
        self.background_tasks.append({"id": task_id, "kind": kind, "iids": list(iids), "text": description, "active": True})
        self._refresh_background_task_list()
        return task_id

    def _finish_background_ai_task(self, task_id: int, success: bool = True) -> None:
        task = next((item for item in self.background_tasks if item.get("id") == task_id), None)
        if task is None:
            return
        task["active"] = False
        task["text"] = ("✓ " if success else "✗ ") + str(task.get("text") or "AI task")
        if str(task.get("kind") or "") in {"screening", "detail"}:
            for iid in task.get("iids", []):
                kinds = self.ai_running_by_iid.get(iid)
                if kinds:
                    kinds.discard(str(task.get("kind") or ""))
                    if not kinds:
                        self.ai_running_by_iid.pop(iid, None)
                self._refresh_ai_cell(iid)
        self._trim_background_tasks()
        self._refresh_background_task_list()

    @staticmethod
    def _destroy_background_task_window(task: dict | None) -> None:
        chat_window = task.get("chat_window") if isinstance(task, dict) else None
        dialog = chat_window.get("dialog") if isinstance(chat_window, dict) else None
        try:
            if dialog is not None and dialog.winfo_exists():
                dialog.destroy()
        except Exception:
            pass
        if isinstance(chat_window, dict):
            chat_window["dialog"] = None
            chat_window["hidden"] = False
        if isinstance(task, dict):
            task["chat_window"] = None

    def _trim_background_tasks(self) -> None:
        while len(self.background_tasks) > 5:
            index = next((i for i, item in enumerate(self.background_tasks) if not item.get("active")), None)
            if index is None:
                break
            removed = self.background_tasks.pop(index)
            self._destroy_background_task_window(removed)

    def _refresh_background_task_list(self) -> None:
        frame = getattr(self, "background_task_frame", None)
        if frame is None:
            return
        active = [item for item in self.background_tasks if item.get("active")]
        completed = [item for item in self.background_tasks if not item.get("active")]
        remaining_slots = max(0, 5 - len(active))
        visible = active + completed[-remaining_slots:] if remaining_slots else active
        labels = getattr(self, "background_task_labels", [])
        while len(labels) < len(visible):
            label = ttk.Label(frame, text="", width=42, anchor=tk.W)
            label.pack(fill=tk.X)
            labels.append(label)
        while len(labels) > max(5, len(visible)):
            labels.pop().destroy()
        self.background_task_labels = labels
        for index, label in enumerate(labels):
            text = ""
            if index < len(visible):
                item = visible[index]
                prefix = "🕑 " if item.get("active") else ""
                text = prefix + str(item.get("text") or "")
            label.configure(text=text)
            label.unbind("<Double-Button-1>")
            if index < len(visible):
                item = visible[index]
                if item.get("kind") == "detail" and item.get("chat_window") is not None:
                    label.bind(
                        "<Double-Button-1>",
                        lambda _event, task_id=int(item.get("id")): self._reopen_background_task_window(task_id),
                    )

    def _row_tags(self, job: DisplayJob, *, selected: bool | None = None) -> tuple[str, ...]:
        ai_mark = self.ai_screening_marks.get(job.iid)
        if ai_mark in {"potential", "maybe", "unlikely"}:
            tag = f"ai_screen_{ai_mark}"
        else:
            tag = self._status_tag(job.status)
        if selected is None:
            selected = job.iid in set(self.tree.selection()) if hasattr(self, "tree") else False
        return (f"selected_{tag}" if selected else tag,)

    def _refresh_tree_selection_tags(self) -> None:
        if not hasattr(self, "tree"):
            return
        current = set(self.tree.selection())
        changed = current.symmetric_difference(getattr(self, "_last_tree_selection", set()))
        for iid in changed:
            if not self.tree.exists(iid):
                continue
            job = self._get_display_job(iid)
            if job is not None:
                self.tree.item(iid, tags=self._row_tags(job, selected=iid in current))
        self._last_tree_selection = current

    def select_all_jobs(self, _event: tk.Event | None = None) -> str:
        self.tree.selection_set(self.tree.get_children(""))
        return "break"

    def clear_unsaved_results(self) -> None:
        count = len(self.pending_by_iid)
        if count <= 0:
            self.status_label_var.set("There are no unsaved new* entries to remove.")
            return
        if not messagebox.askyesno(
            "Clear unsaved entries",
            f"Remove all {count} unsaved new* entries?\n\nThis cannot be undone.",
            parent=self,
        ):
            return
        self.pending_by_iid.clear()
        self.pending_import_index_by_iid.clear()
        self.temp_routes_by_iid.clear()
        for iid in list(self.ai_screening_marks.keys()):
            if str(iid).startswith("tmp"):
                self.ai_screening_marks.pop(iid, None)
        for iid in list(self.temp_ai_screening_scores_by_iid.keys()):
            if str(iid).startswith("tmp"):
                self.temp_ai_screening_scores_by_iid.pop(iid, None)
        for iid in list(self.temp_ai_screening_details_by_iid.keys()):
            if str(iid).startswith("tmp"):
                self.temp_ai_screening_details_by_iid.pop(iid, None)
        for iid in list(self.temp_ai_evaluations_by_iid.keys()):
            if str(iid).startswith("tmp"):
                self.temp_ai_evaluations_by_iid.pop(iid, None)
        for iid in list(self.temp_industries_by_iid.keys()):
            if str(iid).startswith("tmp"):
                self.temp_industries_by_iid.pop(iid, None)
        self.refresh_jobs()
        self.status_label_var.set(f"Removed {count} unsaved new* entries.")

    def _selected_jobs_for_ai(self) -> list[DisplayJob]:
        jobs: list[DisplayJob] = []
        for iid in self.tree.selection():
            job = self._get_display_job(iid)
            if job is not None:
                jobs.append(job)
        return jobs

    def _has_ai_evaluation_for_job(self, job: DisplayJob) -> bool:
        if job.iid in self.temp_ai_evaluations_by_iid:
            return True
        return bool(job.db_id is not None and self.db.get_latest_ai_evaluation(job.db_id))

    def _get_ai_evaluation_for_job(self, job: DisplayJob) -> dict | None:
        if job.iid in self.temp_ai_evaluations_by_iid:
            return self.temp_ai_evaluations_by_iid[job.iid]
        if job.db_id is not None:
            return self.db.get_latest_ai_evaluation(job.db_id)
        return None

    def _ai_detail_payload(self, job: DisplayJob) -> dict:
        evaluation = self._get_ai_evaluation_for_job(job) or {}
        raw_json = ""
        if job.db_id is not None:
            raw_json = self.db.get_latest_raw_json(job.db_id) or ""
        elif job.candidate is not None:
            raw_json = job.candidate.raw_json or ""
        return {
            "iid": job.iid,
            "db_id": job.db_id,
            "table": self._ai_screening_row(job),
            "description": job.description,
            "notes": job.notes,
            "raw_json": raw_json,
            "ai_summary": evaluation.get("summary", ""),
            "ai_rating": evaluation.get("rating", ""),
        }

    def _ai_screening_row(self, job: DisplayJob) -> dict:
        return {
            "iid": job.iid,
            "status": job.status,
            "industry": job.branch,
            "min_k_eur": self._salary_display(job.min_salary_k),
            "max_k_eur": self._salary_display(job.max_salary_k),
            "term": job.fixed_term or "-",
            "published": job.published_date,
            "title": self._display_title(job),
            "company": job.company,
            "distance": self._distance_display(job.route_distance_km, job.route_quality),
            "travel_time": self._duration_display(job.route_duration_min, job.route_quality),
            "location": self._display_location(job),
        }


    def _ai_user_config_with_industries(self) -> dict:
        config = dict(self.user_config)
        config["ai_allowed_industries"] = self.db.list_company_ratings()
        return config


    def ai_screen_selected_jobs(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if not jobs:
            self.status_label_var.set("No jobs selected for AI screening.")
            return
        model_config = self._default_ai_model_config("screening")
        if model_config is None:
            messagebox.showwarning("AI screening", "No enabled AI model is configured in Settings → AI.")
            return
        rows = [self._ai_screening_row(job) for job in jobs]
        chat = self._open_ai_screening_window(rows, model_config, task="screening")
        self._append_ai_chat_text(chat, "System", "Ready. Choose an AI model and click **Start analysis** to screen the selected jobs using table data only.")
        start_button = chat.get("start_button")
        if start_button is not None:
            start_button.configure(command=lambda chat=chat: self._start_ai_screening_analysis(chat))
            self._blink_start_analysis_button(start_button)

    def _start_ai_screening_analysis(self, chat) -> None:
        if chat.get("busy"):
            return
        rows = chat.get("rows", [])
        model_config = chat.get("model")
        if model_config is None:
            self.status_label_var.set("No AI model selected.")
            return
        start_button = chat.get("start_button")
        if start_button is not None:
            try:
                start_button.configure(style="TButton")
            except Exception:
                pass
        self._set_ai_chat_busy(chat, True, f"AI is screening {len(rows)} selected jobs...")
        self.status_label_var.set(f"AI screening started for {len(rows)} selected jobs...")
        iids = [str(row.get("iid") or "") for row in rows if str(row.get("iid") or "")]
        chat["background_task_id"] = self._start_background_ai_task("screening", iids, f"Screening {len(iids)} job(s)")
        self._attach_background_task_window(chat["background_task_id"], chat)
        threading.Thread(target=self._ai_screening_worker, args=(rows, model_config, chat), daemon=True).start()

    def _ai_screening_worker(self, rows: list[dict], model_config: AiModelConfig, chat_window) -> None:
        try:
            result = self.ai_service.screen_jobs_from_table(rows, model_config, self._ai_user_config_with_industries())
            usage = self.ai_service.consume_last_usage()
            self.after(0, lambda result=result, usage=usage: self._finish_ai_screening(result, chat_window, usage))
        except Exception as exc:
            error_message = str(exc)
            self.after(0, lambda error_message=error_message: self._fail_ai_screening(error_message, chat_window))

    def _finish_ai_screening(self, result: dict, chat_window, usage: dict | None = None) -> None:
        if isinstance(chat_window, dict) and chat_window.get("cancelled"):
            return
        token_var = chat_window.get("token_var") if isinstance(chat_window, dict) else None
        if token_var is not None:
            token_var.set(self._format_token_usage(usage))
        items = result.get("items", []) if isinstance(result, dict) else []
        if isinstance(chat_window, dict):
            chat_window["latest_items"] = items if isinstance(items, list) else []
            chat_window["raw_response"] = str(result.get("raw_response") or "") if isinstance(result, dict) else ""
            button = chat_window.get("apply_marks_button")
            if button is not None:
                button.configure(state=tk.NORMAL if items else tk.DISABLED)
        chat_text = str(result.get("chat_text") or "").strip()
        if not chat_text:
            chat_text = self._format_ai_screening_result_text(items)
        self._append_ai_chat_text(chat_window, "AI", chat_text, model_name=self._chat_model_label(chat_window))
        self._show_ai_chat_input(chat_window)
        self._set_ai_chat_busy(chat_window, False, "AI screening finished. You can now ask follow-up questions or click 'Apply AI results'.")
        self.status_label_var.set("AI screening finished. Results are ready but not applied yet.")
        self._finish_background_ai_task(chat_window.get("background_task_id", -1), True)

    def _apply_ai_screening_marks_from_chat(self, chat_window) -> None:
        items = chat_window.get("latest_items", []) if isinstance(chat_window, dict) else []
        marked = self._apply_ai_screening_items(items, apply_summaries=True, chat_window=chat_window)
        if isinstance(chat_window, dict):
            chat_window["results_applied"] = True
        self._append_ai_chat_text(chat_window, "System", f"Applied temporary AI results to {marked} visible rows.")
        self.status_label_var.set(f"Applied temporary AI results to {marked} visible rows.")

    def _apply_ai_screening_items(self, items: list, apply_summaries: bool = False, chat_window=None) -> int:
        marked = 0
        model_config = chat_window.get("model") if isinstance(chat_window, dict) else None
        raw_response = ""
        if isinstance(chat_window, dict):
            raw_response = str(chat_window.get("raw_response") or "")
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("iid") or "").strip()
            score_value = self._coerce_ai_screening_score(item.get("score"))
            decision = self._screening_decision_from_score(score_value) or str(item.get("decision") or "").strip().lower()
            if iid and decision in {"potential", "maybe", "unlikely"}:
                self.ai_screening_marks[iid] = decision
                if self.tree.exists(iid):
                    job = self._get_display_job(iid)
                    if job is not None:
                        self._apply_ai_detected_flags(iid, item, job)
                        self.tree.item(iid, tags=self._row_tags(job))
                        self._update_dirty_indicators_for_iid(iid)
                        if apply_summaries:
                            summary = self._screening_item_to_summary(item, job)
                            existing = self._get_ai_evaluation_for_job(job) or {}
                            existing_text = str(existing.get("summary") or "").strip()
                            allow = True
                            if existing_text:
                                allow = messagebox.askyesno(
                                    "Overwrite AI summary?",
                                    f"{job.title}\n{job.company}\n\nExisting AI summary: {len(existing_text)} characters\nNew screening summary: {len(summary)} characters\n\nOverwrite existing AI summary?",
                                    parent=self,
                                )
                            if allow:
                                current = self.temp_ai_evaluations_by_iid.get(iid, {})
                                current.update({
                                    "ai_name": getattr(model_config, "name", "AI"),
                                    "provider": getattr(model_config, "provider", ""),
                                    "model": getattr(model_config, "model", ""),
                                    "created_at": utc_now_iso(),
                                    "summary": summary,
                                    "rating": str(current.get("rating") or ""),
                                    "score": score_value if score_value is not None else current.get("score"),
                                    "decision": decision,
                                    "raw_response": raw_response,
                                })
                                self.temp_ai_evaluations_by_iid[iid] = current
                                if isinstance(current.get("score"), int):
                                    job.manual_score = current.get("score")
                                self._apply_ai_industry_suggestion(iid, item, job)
                                self._update_dirty_indicators_for_iid(iid)
                marked += 1
        if apply_summaries and self.selected_iid:
            job = self._get_display_job(self.selected_iid)
            if job is not None:
                self._load_ai_details_for_job(job)
        return marked

    def _screening_item_to_summary(self, item: dict, job: DisplayJob) -> str:
        decision = str(item.get("decision") or "-")
        score = item.get("score", "-")
        reason = str(item.get("reason") or "-")
        flags = []
        if item.get("is_anue") is True or item.get("anue") is True or item.get("is_arbeitnehmerueberlassung") is True:
            flags.append("ANÜ")
        if item.get("is_fixed_term") is True or item.get("fixed_term") is True or item.get("befristet") is True:
            flags.append("Fixed-Term")
        flag_text = ", ".join(flags) if flags else "-"
        return (
            "## AI Screening\n"
            f"- **Job:** {self._display_title(job)}\n"
            f"- **Firma / Ort:** {job.company} — {self._display_location(job)}\n"
            f"- **Entfernung:** {self._distance_display(job.route_distance_km, job.route_quality)} ({self._duration_display(job.route_duration_min, job.route_quality)})\n"
            f"- **Entscheidung:** {decision}\n"
            f"- **Score:** {score}\n"
            f"- **Erkannte Flags:** {flag_text}\n"
            f"- **Kurzgrund:** {reason}\n\n"
            "Hinweis: Dies ist nur ein grobes Screening anhand der Tabellendaten, keine Detailanalyse der Stellenausschreibung."
        )

    def _fail_ai_screening(self, error_message: str, chat_window) -> None:
        if isinstance(chat_window, dict) and chat_window.get("cancelled"):
            return
        self._append_ai_chat_text(chat_window, "System", f"AI screening failed: {error_message}")
        self._show_ai_chat_input(chat_window)
        self._set_ai_chat_busy(chat_window, False, "AI screening failed.")
        self.status_label_var.set("AI screening failed.")
        self._finish_background_ai_task(chat_window.get("background_task_id", -1), False)

    def _format_ai_screening_result_text(self, items: list) -> str:
        lines = ["**AI screening result**", ""]
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = item.get("iid", "")
            decision = item.get("decision", "")
            score = item.get("score", "")
            reason = item.get("reason", "")
            lines.append(f"- **{iid}**: {decision} ({score}) — {reason}")
        return "\n".join(lines)

    @staticmethod
    def _format_token_usage(usage: dict | None) -> str:
        usage = usage or {}
        def fmt(value):
            return "N/A" if value is None else f"{int(value):,}"
        return (
            f"Tokens: in {fmt(usage.get('input_tokens'))} | "
            f"cached {fmt(usage.get('cached_tokens'))} | "
            f"out {fmt(usage.get('output_tokens'))} | "
            f"total {fmt(usage.get('total_tokens'))}"
        )

    @staticmethod
    def _estimate_tokens_from_text(text: str) -> int:
        # Provider-neutral estimate. Exact tokenization depends on the selected model.
        return max(1, int(round(len(str(text or '').encode('utf-8')) / 4.0)))

    def _chat_context_options(self, chat_window: dict) -> dict:
        vars_by_name = chat_window.get("context_vars") or {}
        return {name: bool(var.get()) for name, var in vars_by_name.items()}

    def _update_chat_context_estimate(self, chat_window: dict) -> None:
        label_var = chat_window.get("context_size_var")
        if label_var is None:
            return
        options = self._chat_context_options(chat_window)
        pieces = []
        context = chat_window.get("job_context") or {}
        if options.get("job_data"):
            pieces.append(json.dumps({k: v for k, v in context.items() if k not in {"ai_summary", "ai_rating", "notes"}}, ensure_ascii=False))
        if options.get("ai_analysis"):
            pieces.append(str(context.get("ai_summary") or "") + str(context.get("ai_rating") or ""))
        if options.get("notes"):
            pieces.append(str(context.get("notes") or ""))
        if options.get("user_profile"):
            try:
                from .ai.memory import load_profile_text
                pieces.append(load_profile_text(str(self.user_config.get("ai_profile_path") or "").strip() or None))
            except Exception:
                pass
        if options.get("chat_history"):
            pieces.append(json.dumps(chat_window.get("conversation", []), ensure_ascii=False))
        label_var.set(f"Selected context: ~{self._estimate_tokens_from_text(''.join(pieces)):,} tokens (estimate)")

    def _toggle_all_chat_context(self, chat_window: dict) -> None:
        master = chat_window.get("check_all_var")
        enabled = bool(master.get()) if master is not None else False
        for var in (chat_window.get("context_vars") or {}).values():
            var.set(enabled)
        self._update_chat_context_estimate(chat_window)

    def _open_ai_screening_window(self, rows: list[dict], model_config: AiModelConfig, task: str = "screening"):
        dialog = tk.Toplevel(self)
        dialog.title("AI screening")
        window_height = 720 if task == "detailed_analysis" else 850
        dialog.geometry(f"850x{window_height}")
        dialog.resizable(False, False)
        dialog.transient(self)
        self._center_child_window(dialog, width=850, height=window_height)
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        top_row = ttk.Frame(frame)
        top_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(top_row, text=f"{len(rows)} selected jobs").pack(side=tk.LEFT)
        ttk.Label(top_row, text="Model:").pack(side=tk.LEFT, padx=(16, 4))
        model_names = [config.name for config in self.ai_models if config.enabled]
        model_by_name = {config.name: config for config in self.ai_models if config.enabled}
        model_var = tk.StringVar(value=model_config.name)
        model_combo = ttk.Combobox(top_row, textvariable=model_var, values=model_names, state="readonly", width=28)
        model_combo.pack(side=tk.LEFT)
        start_button = ttk.Button(top_row, text="Start analysis", style="Attention.TButton")
        start_button.pack(side=tk.LEFT, padx=(8, 0))

        token_var = tk.StringVar(value=self._format_token_usage({}))
        ttk.Label(frame, textvariable=token_var).pack(fill=tk.X, pady=(0, 4))

        context_frame = ttk.LabelFrame(frame, text="Context sent with Ask AI", padding=6)
        context_frame.pack(fill=tk.X, pady=(0, 6))
        # Context defaults depend on the task. Quick screening already receives its
        # compact table payload separately, while detailed analysis should have the
        # complete available context selected by default.
        detail_defaults = task == "detailed_analysis"
        chat_defaults = task == "chat"
        screening_defaults = task == "screening"
        check_all_var = tk.BooleanVar(value=detail_defaults)
        context_vars = {
            "chat_history": tk.BooleanVar(value=detail_defaults or chat_defaults),
            "job_data": tk.BooleanVar(value=detail_defaults),
            # The initial quick screening always receives the user profile. Keep it
            # selected for follow-up questions as well so the UI reflects that context.
            "user_profile": tk.BooleanVar(value=detail_defaults or screening_defaults),
            "ai_analysis": tk.BooleanVar(value=detail_defaults),
            "notes": tk.BooleanVar(value=detail_defaults),
        }
        check_all = ttk.Checkbutton(context_frame, text="Check all/none", variable=check_all_var)
        check_all.pack(side=tk.LEFT, padx=(0, 12))
        style = ttk.Style(dialog)
        style.configure("Context.TCheckbutton", background=DARK_THEME["panel"], foreground=DARK_THEME["fg"])
        style.map(
            "Context.TCheckbutton",
            background=[("!selected", "#b85c00"), ("selected", DARK_THEME["panel"])],
            foreground=[("!selected", "#ffffff"), ("selected", DARK_THEME["fg"])],
        )
        labels = [("chat_history", "Chat history"), ("job_data", "Job data"), ("user_profile", "User profile"), ("ai_analysis", "AI analysis"), ("notes", "Notes")]
        for key, label in labels:
            ttk.Checkbutton(context_frame, text=label, variable=context_vars[key], style="Context.TCheckbutton").pack(side=tk.LEFT, padx=(0, 10))
        context_size_var = tk.StringVar(value="Selected context: ~0 tokens (estimate)")
        ttk.Label(frame, textvariable=context_size_var).pack(fill=tk.X, pady=(0, 4))

        status_var = tk.StringVar(value="Idle")
        status_label = ttk.Label(frame, textvariable=status_var)
        status_label.pack(fill=tk.X, pady=(0, 6))

        chat_frame = ttk.Frame(frame)
        chat_frame.pack(fill=tk.BOTH, expand=True)
        text = tk.Text(
            chat_frame,
            wrap=tk.WORD,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            selectbackground=DARK_THEME["select_bg"],
            selectforeground=DARK_THEME["select_fg"],
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        scroll = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.tag_configure("speaker", foreground="#8ab4f8", font=("Segoe UI", 10, "bold"))
        text.tag_configure("model_name", foreground="#9aa0a6", font=("Segoe UI", 10))
        text.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        text.tag_configure("bullet", lmargin1=18, lmargin2=32)

        input_frame = ttk.Frame(frame)
        input_frame.pack(fill=tk.X, pady=(8, 0))
        user_input = tk.Text(
            input_frame,
            height=10,
            wrap=tk.WORD,
            background=DARK_THEME["entry_bg"],
            foreground=DARK_THEME["entry_fg"],
            insertbackground=DARK_THEME["fg"],
            font=("Segoe UI", 10),
        )
        user_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        send_button = ttk.Button(input_frame, text="Send")
        send_button.pack(side=tk.RIGHT, padx=(8, 0))
        input_frame.pack_forget()

        action_row = ttk.Frame(frame)
        action_row.pack(fill=tk.X, pady=(6, 0))
        apply_marks_button = ttk.Button(action_row, text="Apply AI results", state=tk.DISABLED)
        apply_marks_button.pack(side=tk.LEFT)
        regenerate_button = ttk.Button(action_row, text="Re-generate AI summary/rating", state=tk.DISABLED)
        regenerate_button.pack(side=tk.LEFT, padx=(8, 0))

        chat_window = {
            "dialog": dialog,
            "text": text,
            "input": user_input,
            "input_frame": input_frame,
            "send_button": send_button,
            "start_button": start_button,
            "status_var": status_var,
            "token_var": token_var,
            "context_frame": context_frame,
            "check_all_var": check_all_var,
            "context_vars": context_vars,
            "context_size_var": context_size_var,
            "rows": rows,
            "model": model_config,
            "model_var": model_var,
            "model_by_name": model_by_name,
            "mode": "screening",
            "task": task,
            "conversation": [],
            "busy": False,
            "hidden": False,
            "cancelled": False,
            "latest_items": [],
            "apply_marks_button": apply_marks_button,
            "regenerate_button": regenerate_button,
            "action_row": action_row,
        }
        check_all.configure(command=lambda cw=chat_window: self._toggle_all_chat_context(cw))
        for var in context_vars.values():
            var.trace_add("write", lambda *_args, cw=chat_window: self._update_chat_context_estimate(cw))
        self._update_chat_context_estimate(chat_window)
        apply_marks_button.configure(command=lambda: self._apply_ai_screening_marks_from_chat(chat_window))
        regenerate_button.configure(command=lambda cw=chat_window: self._regenerate_ai_summary_rating_from_chat(cw))
        model_combo.bind("<<ComboboxSelected>>", lambda _event: self._set_ai_chat_model_from_dropdown(chat_window))
        send_button.configure(command=lambda: self._send_ai_chat_message(chat_window))
        user_input.bind("<Shift-Return>", lambda _event: None)
        user_input.bind("<Return>", lambda _event: (self._send_ai_chat_message(chat_window), "break")[-1])
        dialog.protocol("WM_DELETE_WINDOW", lambda cw=chat_window: self._close_ai_chat_window(cw))
        return chat_window

    def _close_ai_chat_window(self, chat_window) -> None:
        dialog = chat_window.get("dialog") if isinstance(chat_window, dict) else None
        if dialog is None:
            return

        # Detail analyses are independent background jobs. While a request is
        # running, closing only hides the window so it can be reopened from the
        # background-task list. After completion, closing destroys it normally.
        if chat_window.get("task") == "detailed_analysis":
            if chat_window.get("busy"):
                try:
                    dialog.withdraw()
                    chat_window["hidden"] = True
                except Exception:
                    pass
            else:
                task_id = chat_window.get("background_task_id")
                task = next((item for item in self.background_tasks if item.get("id") == task_id), None)
                if isinstance(task, dict):
                    task["chat_window"] = None
                try:
                    dialog.destroy()
                except Exception:
                    pass
                chat_window["dialog"] = None
                chat_window["hidden"] = False
                self._refresh_background_task_list()
            return

        if chat_window.get("busy"):
            close_anyway = messagebox.askyesno(
                "AI task still running",
                "This AI task is still running. Closing the window will cancel the task in JobRadar and discard its result.\n\nClose anyway?",
                parent=dialog,
            )
            if not close_anyway:
                return
            chat_window["cancelled"] = True
            self._finish_background_ai_task(chat_window.get("background_task_id", -1), False)

        if chat_window.get("mode") == "screening" and chat_window.get("latest_items") and not chat_window.get("results_applied"):
            answer = messagebox.askyesnocancel(
                "Unapplied AI results",
                "There are unapplied AI screening results.\n\nApply results before closing?",
                parent=dialog,
            )
            if answer is None:
                return
            if answer is True:
                self._apply_ai_screening_marks_from_chat(chat_window)
        try:
            dialog.destroy()
        except Exception:
            pass

    def _attach_background_task_window(self, task_id: int, chat_window: dict) -> None:
        task = next((item for item in self.background_tasks if item.get("id") == task_id), None)
        if task is not None:
            task["chat_window"] = chat_window
            self._refresh_background_task_list()

    def _reopen_background_task_window(self, task_id: int) -> None:
        task = next((item for item in self.background_tasks if item.get("id") == task_id), None)
        if task is None or task.get("kind") != "detail":
            return
        chat_window = task.get("chat_window")
        dialog = chat_window.get("dialog") if isinstance(chat_window, dict) else None
        if dialog is None:
            return
        try:
            if not dialog.winfo_exists():
                return
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()
            chat_window["hidden"] = False
        except Exception:
            pass

    def _show_ai_chat_input(self, chat_window) -> None:
        if not isinstance(chat_window, dict):
            return
        frame = chat_window.get("input_frame")
        if frame is None:
            return
        try:
            if not bool(frame.winfo_ismapped()):
                frame.pack(fill=tk.X, pady=(8, 0), before=chat_window.get("action_row"))
        except Exception:
            try:
                frame.pack(fill=tk.X, pady=(8, 0))
            except Exception:
                pass

    def _blink_start_analysis_button(self, button, remaining: int = 8) -> None:
        if button is None or remaining <= 0:
            try:
                if button is not None and str(button.cget("state")) != tk.DISABLED:
                    button.configure(style="Attention.TButton")
            except Exception:
                pass
            return
        try:
            if str(button.cget("state")) == tk.DISABLED:
                return
            current = str(button.cget("style") or "")
            button.configure(style=("AttentionFlash.TButton" if current == "Attention.TButton" else "Attention.TButton"))
            self.after(180, lambda b=button, r=remaining - 1: self._blink_start_analysis_button(b, r))
        except Exception:
            pass


    def _set_ai_chat_model_from_dropdown(self, chat_window) -> None:
        if not isinstance(chat_window, dict):
            return
        model_var = chat_window.get("model_var")
        model_by_name = chat_window.get("model_by_name") or {}
        if model_var is None:
            return
        selected = str(model_var.get() or "")
        if selected in model_by_name:
            chat_window["model"] = model_by_name[selected]
            self._remember_ai_model_choice(selected, chat_window.get("task"))
            self._set_ai_chat_busy(chat_window, bool(chat_window.get("busy")), f"Model changed to {selected}.")

    def _send_ai_chat_message(self, chat_window) -> None:
        input_widget = chat_window.get("input") if isinstance(chat_window, dict) else None
        if input_widget is None:
            return
        if chat_window.get("busy"):
            self.status_label_var.set("AI is still working. Please wait for the current answer.")
            return
        question = input_widget.get("1.0", tk.END).strip()
        if not question:
            return
        self._append_ai_chat_text(chat_window, "You", question)
        chat_window.setdefault("conversation", []).append({"role": "user", "content": question})
        self._set_ai_chat_busy(chat_window, True, "AI is writing an answer...")
        self.status_label_var.set("AI follow-up running...")
        context = chat_window.get("job_context", {}) if isinstance(chat_window, dict) else {}
        iids = []
        if isinstance(context, dict) and context.get("iid"):
            iids = [str(context.get("iid"))]
        elif isinstance(chat_window, dict):
            iids = [str(row.get("iid")) for row in chat_window.get("rows", []) if isinstance(row, dict) and row.get("iid")]
        chat_window["background_task_id"] = self._start_background_ai_task("chat", iids, f"AI chat ({len(iids) or 'general'})")
        self._attach_background_task_window(chat_window["background_task_id"], chat_window)
        threading.Thread(target=self._ai_chat_worker, args=(chat_window, question), daemon=True).start()

    def _ai_chat_worker(self, chat_window, question: str) -> None:
        try:
            if chat_window.get("mode") in {"job", "detail_multi"}:
                answer = self.ai_service.chat_about_job(
                    job_context=chat_window.get("job_context", {}),
                    conversation=chat_window.get("conversation", []),
                    question=question,
                    model_config=chat_window.get("model"),
                    user_config=self.user_config,
                    context_options=self._chat_context_options(chat_window),
                )
            else:
                answer = self.ai_service.chat_about_screening(
                    rows=chat_window.get("rows", []),
                    conversation=chat_window.get("conversation", []),
                    question=question,
                    model_config=chat_window.get("model"),
                    user_config=self.user_config,
                    context_options=self._chat_context_options(chat_window),
                )
            usage = self.ai_service.consume_last_usage()
            self.after(0, lambda answer=answer, usage=usage: self._finish_ai_chat_message(chat_window, answer, usage))
        except Exception as exc:
            error_message = str(exc)
            self.after(0, lambda error_message=error_message: self._fail_ai_chat_message(chat_window, error_message))

    def _finish_ai_chat_message(self, chat_window, answer: str, usage: dict | None = None) -> None:
        if isinstance(chat_window, dict) and chat_window.get("cancelled"):
            return
        token_var = chat_window.get("token_var") if isinstance(chat_window, dict) else None
        if token_var is not None:
            token_var.set(self._format_token_usage(usage))
        chat_window.setdefault("conversation", []).append({"role": "assistant", "content": answer})
        self._append_ai_chat_text(chat_window, "AI", answer, model_name=self._chat_model_label(chat_window))
        self._set_ai_chat_busy(chat_window, False, "AI follow-up finished.")
        self.status_label_var.set("AI follow-up finished.")
        self._finish_background_ai_task(chat_window.get("background_task_id", -1), True)

    def _fail_ai_chat_message(self, chat_window, error_message: str) -> None:
        if isinstance(chat_window, dict) and chat_window.get("cancelled"):
            return
        self._append_ai_chat_text(chat_window, "System", f"AI follow-up failed: {error_message}")
        self._set_ai_chat_busy(chat_window, False, "AI follow-up failed.")
        self.status_label_var.set("AI follow-up failed.")
        self._finish_background_ai_task(chat_window.get("background_task_id", -1), False)

    def _set_ai_chat_busy(self, chat_window, busy: bool, status_text: str = "") -> None:
        if not isinstance(chat_window, dict):
            return
        chat_window["busy"] = busy
        chat_window["busy_base_status"] = status_text or ("AI is working" if busy else "Idle")
        chat_window["busy_tick"] = 0
        status_var = chat_window.get("status_var")
        if status_var is not None:
            status_var.set(status_text or ("AI is working" if busy else "Idle"))
        send_button = chat_window.get("send_button")
        if send_button is not None:
            send_button.configure(state=(tk.DISABLED if busy else tk.NORMAL))
        start_button = chat_window.get("start_button")
        if start_button is not None:
            start_button.configure(state=(tk.DISABLED if busy else tk.NORMAL))
        if busy:
            self._animate_ai_busy_status(chat_window)

    def _animate_ai_busy_status(self, chat_window) -> None:
        if not isinstance(chat_window, dict) or not chat_window.get("busy"):
            return
        status_var = chat_window.get("status_var")
        if status_var is None:
            return
        base = str(chat_window.get("busy_base_status") or "AI is working").rstrip(".")
        tick = int(chat_window.get("busy_tick") or 0)
        dots = "." * (tick % 4)
        status_var.set(base + dots)
        chat_window["busy_tick"] = tick + 1
        self.after(350, lambda cw=chat_window: self._animate_ai_busy_status(cw))

    def _chat_model_label(self, chat_window) -> str:
        model_config = chat_window.get("model") if isinstance(chat_window, dict) else None
        return getattr(model_config, "name", "") or getattr(model_config, "model", "") or ""

    def _append_ai_chat_text(self, chat_window, speaker: str, message: str, model_name: str = "", persist: bool = True) -> None:
        text = chat_window.get("text") if isinstance(chat_window, dict) else None
        if text is None:
            return
        text.configure(state=tk.NORMAL)
        text.insert(tk.END, "\n")
        header_index = text.index(tk.END)
        text.insert(tk.END, speaker, ("speaker",))
        if speaker == "AI" and model_name:
            text.insert(tk.END, f" ({model_name})", ("model_name",))
        text.insert(tk.END, ":\n", ("speaker",))
        message_text = str(message or "").strip()
        self._insert_markdownish_text(text, message_text + "\n")
        if speaker == "AI":
            try:
                visible_lines = max(8, int(text.winfo_height() / max(1, int(font.Font(font=text.cget("font")).metrics("linespace")))))
            except Exception:
                visible_lines = 24
            response_lines = message_text.count("\n") + 3
            if response_lines >= visible_lines:
                text.see(header_index)
                text.yview(header_index)
            else:
                text.see(tk.END)
        else:
            text.see(tk.END)
        text.configure(state=tk.DISABLED)
        if persist and chat_window.get("persist_chat") and speaker in {"You", "AI"}:
            job_id = chat_window.get("job_db_id")
            if job_id is not None:
                role = "user" if speaker == "You" else "assistant"
                try:
                    self.db.save_ai_chat_message(int(job_id), role, str(message or ""), model_name if speaker == "AI" else "")
                except Exception:
                    pass
        if speaker == "You":
            input_widget = chat_window.get("input")
            if input_widget is not None:
                input_widget.delete("1.0", tk.END)

    def _insert_markdownish_text(self, text: tk.Text, message: str) -> None:
        # Lightweight readable rendering: headings, bullets and **bold**.
        for raw_line in str(message or "").splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith("### "):
                self._insert_markdown_inline(text, stripped[4:], ("h2",))
                text.insert(tk.END, "\n")
                continue
            if stripped.startswith("## "):
                self._insert_markdown_inline(text, stripped[3:], ("h1",))
                text.insert(tk.END, "\n")
                continue
            if stripped.startswith("# "):
                self._insert_markdown_inline(text, stripped[2:], ("h1",))
                text.insert(tk.END, "\n")
                continue
            tags = ("bullet",) if stripped.startswith(("- ", "* ", "• ")) else ()
            known_prefixes = {
                "rejection reason", "would use rejection tags", "score", "recommendation",
                "overall rating", "gesamturteil", "technische passung", "interesse / sinn",
                "energie-/gedächtnis-fit", "standort / vertrag", "risiken / no-gos",
                "empfehlung", "passende skills", "lücken / unklar", "warnsignale",
                "woran arbeitet die firma?", "woran arbeitet das team?",
                "was mache ich den ganzen tag?", "likely industry"
            }
            plain = stripped[2:].strip() if tags and stripped[:2] in {"- ", "* "} else stripped.lstrip("• ")
            prefix, sep, rest = plain.partition(":")
            if "**" not in line and sep and prefix.strip().casefold() in known_prefixes:
                bullet_prefix = line[:len(line) - len(line.lstrip())]
                if tags:
                    bullet_prefix += line.lstrip()[:2]
                text.insert(tk.END, bullet_prefix, tags)
                text.insert(tk.END, prefix.strip() + ":", tuple(tags) + ("bold",))
                text.insert(tk.END, " " + rest.lstrip(), tags)
            else:
                self._insert_markdown_inline(text, line, tags)
            text.insert(tk.END, "\n", tags)

    def _insert_markdown_inline(self, text: tk.Text, line: str, base_tags: tuple = ()) -> None:
        parts = str(line or "").split("**")
        for idx, part in enumerate(parts):
            if not part:
                continue
            tags = base_tags + (("bold",) if idx % 2 == 1 else ())
            self._insert_text_with_links(text, part, tags)

    def _insert_text_with_links(self, text: tk.Text, segment: str, tags: tuple = ()) -> None:
        """Insert text and turn Markdown links like [label](https://...) into clickable Text tags."""
        pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
        pos = 0
        for match in pattern.finditer(str(segment or "")):
            if match.start() > pos:
                text.insert(tk.END, segment[pos:match.start()], tags)
            label = match.group(1)
            url = match.group(2)
            tag = f"link_{id(text)}_{len(text.tag_names())}_{match.start()}"
            link_tags = tags + (tag,)
            text.insert(tk.END, label, link_tags)
            text.tag_configure(tag, foreground="#8ab4f8", underline=True)
            text.tag_bind(tag, "<Button-1>", lambda _event, u=url: webbrowser.open(u))
            text.tag_bind(tag, "<Enter>", lambda _event, u=url, w=text: self._on_text_link_enter(w, u))
            text.tag_bind(tag, "<Leave>", lambda _event, w=text: self._on_text_link_leave(w))
            pos = match.end()
        if pos < len(segment):
            text.insert(tk.END, segment[pos:], tags)

    def _on_text_link_enter(self, widget: tk.Text, url: str) -> None:
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        try:
            self.status_label_var.set(str(url))
        except Exception:
            pass

    def _on_text_link_leave(self, widget: tk.Text) -> None:
        try:
            widget.configure(cursor="xterm")
        except Exception:
            pass
        try:
            self.status_label_var.set("Ready")
        except Exception:
            pass


    def _regenerate_ai_summary_rating_from_chat(self, chat_window) -> None:
        if not isinstance(chat_window, dict) or chat_window.get("busy"):
            return
        if chat_window.get("mode") != "job":
            self.status_label_var.set("Regenerate summary/rating is available for single-job chats only.")
            return
        model_config = chat_window.get("model")
        if model_config is None:
            self.status_label_var.set("No AI model selected.")
            return
        job_context = chat_window.get("job_context", {})
        if not isinstance(job_context, dict):
            return
        iid = str(job_context.get("iid") or "").strip()
        if not iid:
            job = self._selected_jobs_for_ai()
            iid = job[0].iid if len(job) == 1 else ""
        if not iid:
            self.status_label_var.set("Cannot identify the job to update.")
            return
        self._set_ai_chat_busy(chat_window, True, "AI is regenerating summary/rating...")
        threading.Thread(target=self._ai_regenerate_summary_rating_worker, args=(chat_window, iid, model_config), daemon=True).start()

    def _ai_regenerate_summary_rating_worker(self, chat_window, iid: str, model_config: AiModelConfig) -> None:
        try:
            job_context = chat_window.get("job_context", {})
            payload = {
                "iid": iid,
                "table": job_context.get("table", {}) if isinstance(job_context, dict) else {},
                "description": job_context.get("description", "") if isinstance(job_context, dict) else "",
                "notes": job_context.get("notes", "") if isinstance(job_context, dict) else "",
                "ai_summary": job_context.get("ai_summary", "") if isinstance(job_context, dict) else "",
                "ai_rating": job_context.get("ai_rating", "") if isinstance(job_context, dict) else "",
                "chat_history": chat_window.get("conversation", []),
            }
            result = self.ai_service.analyze_jobs_in_detail([payload], model_config, self._ai_user_config_with_industries())
            usage = self.ai_service.consume_last_usage()
            self.after(0, lambda result=result, iid=iid, cw=chat_window, usage=usage: self._finish_ai_regenerate_summary_rating(cw, iid, result, usage))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda msg=msg, cw=chat_window: self._fail_ai_regenerate_summary_rating(cw, msg))

    def _finish_ai_regenerate_summary_rating(self, chat_window, iid: str, result: dict, usage: dict | None = None) -> None:
        token_var = chat_window.get("token_var") if isinstance(chat_window, dict) else None
        if token_var is not None:
            token_var.set(self._format_token_usage(usage))
        items = result.get("items", []) if isinstance(result, dict) else []
        item = items[0] if items and isinstance(items[0], dict) else {}
        if not item:
            self._append_ai_chat_text(chat_window, "System", "AI did not return a usable summary/rating update.")
            self._set_ai_chat_busy(chat_window, False, "Regeneration finished without usable update.")
            return
        model_config = chat_window.get("model") if isinstance(chat_window, dict) else None
        score_value = self._coerce_ai_score(item.get("score"))
        current = self.temp_ai_evaluations_by_iid.get(iid, {})
        current.update({
            "ai_name": getattr(model_config, "name", "AI"),
            "provider": getattr(model_config, "provider", ""),
            "model": getattr(model_config, "model", ""),
            "created_at": utc_now_iso(),
            "summary": str(item.get("summary") or current.get("summary") or ""),
            "rating": str(item.get("rating") or current.get("rating") or ""),
            "score": score_value if score_value is not None else current.get("score"),
            "decision": str(item.get("decision") or current.get("decision") or ""),
            "raw_response": str(result.get("raw_response") or current.get("raw_response") or ""),
        })
        self.temp_ai_evaluations_by_iid[iid] = current
        job = self._get_display_job(iid)
        if job is not None and isinstance(current.get("score"), int):
            job.manual_score = current.get("score")
        self._apply_ai_industry_suggestion(iid, item, job)
        self._update_dirty_indicators_for_iid(iid)
        if self.selected_iid == iid and job is not None:
            self._load_ai_details_for_job(job)
        self._append_ai_chat_text(chat_window, "System", "AI summary/rating regenerated. Use **Save selected in database** to persist it.")
        self._set_ai_chat_busy(chat_window, False, "Summary/rating regeneration finished.")
        self.status_label_var.set("AI summary/rating regenerated temporarily.")

    def _fail_ai_regenerate_summary_rating(self, chat_window, error_message: str) -> None:
        self._append_ai_chat_text(chat_window, "System", f"Regenerate failed: {error_message}")
        self._set_ai_chat_busy(chat_window, False, "Regenerate failed.")
        self.status_label_var.set("AI summary/rating regeneration failed.")

    def ai_reformat_selected_job_description(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if len(jobs) != 1:
            self.status_label_var.set("Re-format requires exactly one selected job.")
            return
        model_config = self._default_ai_model_config("reformat")
        if model_config is None:
            messagebox.showwarning("Re-format with AI", "No enabled AI model is configured in Settings → AI.", parent=self)
            return
        job = jobs[0]
        current_text = self.description_text.get("1.0", tk.END).strip() or job.description or ""
        if not current_text:
            self.status_label_var.set("No job description to re-format.")
            return
        iid = job.iid
        self.status_label_var.set("AI is re-formatting the job description...")
        task_id = self._start_background_ai_task("reformat", [iid], f"Re-format: {job.title[:32]}")
        threading.Thread(
            target=self._ai_reformat_description_worker,
            args=(iid, current_text, model_config, task_id),
            daemon=True,
        ).start()

    def _ai_reformat_description_worker(self, iid: str, description: str, model_config: AiModelConfig, task_id: int) -> None:
        try:
            formatted = self.ai_service.reformat_job_description(description, model_config)
            self.after(0, lambda formatted=formatted, iid=iid, task_id=task_id: self._finish_ai_reformat_description(iid, formatted, task_id))
        except Exception as exc:
            error_message = str(exc)
            self.after(0, lambda error_message=error_message, task_id=task_id: self._fail_ai_reformat_description(error_message, task_id))

    def _finish_ai_reformat_description(self, iid: str, formatted: str, task_id: int = -1) -> None:
        formatted = str(formatted or "").strip()
        if not formatted:
            self.status_label_var.set("AI returned an empty re-formatted description.")
            return
        self.temp_formatted_descriptions_by_iid[iid] = formatted
        job = self._get_display_job(iid)
        saved = False
        if job is not None:
            if job.db_id is not None:
                self.db.save_job_formatted_description(job.db_id, formatted)
                # Keep the in-memory row in sync with the database. Without
                # this, switching back to a previously reformatted job showed
                # the stale description until the complete table was reloaded.
                if job.record is not None:
                    job.record.formatted_description = formatted
                if job.candidate is not None:
                    job.candidate.formatted_description = formatted
                self.temp_formatted_descriptions_by_iid.pop(iid, None)
                saved = True
            elif job.candidate is not None:
                job.candidate.formatted_description = formatted
        if self.selected_iid == iid:
            self._set_markdownish_content(self.description_text, formatted)
            try:
                self.description_notebook.select(0)
            except Exception:
                pass
        self._update_dirty_indicators_for_iid(iid)
        self.status_label_var.set("Job description re-formatted and saved." if saved else "Job description re-formatted for unsaved job; it will be saved with the job.")
        self._finish_background_ai_task(task_id, True)

    def _fail_ai_reformat_description(self, error_message: str, task_id: int = -1) -> None:
        self.status_label_var.set(f"Re-format failed: {error_message}")
        self._finish_background_ai_task(task_id, False)

    def ai_analyze_selected_jobs_detail(self) -> None:
        jobs = self._selected_jobs_for_ai()
        if not jobs:
            self.status_label_var.set("No jobs selected for AI detail analysis.")
            return
        model_config = self._default_ai_model_config("detailed_analysis")
        if model_config is None:
            messagebox.showwarning("AI analysis", "No enabled AI model is configured in Settings → AI.")
            return
        payloads = [self._ai_detail_payload(job) for job in jobs]
        chat = self._open_ai_detail_window(payloads, model_config)
        start_button = chat.get("start_button")
        if start_button is not None:
            start_button.configure(command=lambda chat=chat: self._start_ai_detail_analysis(chat))
            self._blink_start_analysis_button(start_button)

    def _start_ai_detail_analysis(self, chat) -> None:
        if chat.get("busy"):
            return
        payloads = chat.get("payloads", [])
        model_config = chat.get("model")
        if model_config is None:
            self.status_label_var.set("No AI model selected.")
            return
        start_button = chat.get("start_button")
        if start_button is not None:
            try:
                start_button.configure(style="TButton")
            except Exception:
                pass
        self._set_ai_chat_busy(chat, True, f"AI is analyzing {len(payloads)} selected jobs in detail...")
        self.status_label_var.set(f"AI detail analysis started for {len(payloads)} jobs...")
        iids = [str(payload.get("iid") or "") for payload in payloads if str(payload.get("iid") or "")]
        chat["background_task_id"] = self._start_background_ai_task("detail", iids, f"Detail analysis {len(iids)} job(s)")
        self._attach_background_task_window(chat["background_task_id"], chat)
        threading.Thread(target=self._ai_detail_worker, args=(payloads, model_config, chat), daemon=True).start()

    def _ai_detail_worker(self, payloads: list[dict], model_config: AiModelConfig, chat_window) -> None:
        """Analyze jobs in bounded batches and publish each successful batch immediately.

        The configured batch size defaults to four. If a batch larger than two
        fails, that batch is retried once as consecutive two-job batches. If a
        two-job (or one-job) batch fails, the run stops immediately.
        """
        try:
            configured_size = int(self.user_config.get("ai_detail_batch_size", 4) or 4)
        except (TypeError, ValueError):
            configured_size = 4
        batch_size = max(1, min(20, configured_size))
        total = len(payloads)
        completed = 0
        usage_totals: dict[str, int] = {}

        def merge_usage(usage: dict | None) -> None:
            if not isinstance(usage, dict):
                return
            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    usage_totals[key] = usage_totals.get(key, 0) + int(value)

        def run_batch(batch: list[dict]) -> dict:
            return self.ai_service.analyze_jobs_in_detail(
                batch, model_config, self._ai_user_config_with_industries()
            )

        index = 0
        while index < total:
            original_batch = payloads[index:index + batch_size]
            try:
                result = run_batch(original_batch)
                merge_usage(self.ai_service.consume_last_usage())
                completed += len(original_batch)
                self.after(
                    0,
                    lambda result=result, batch=list(original_batch), done=completed, total=total:
                        self._apply_ai_detail_batch(result, chat_window, batch, done, total),
                )
                index += len(original_batch)
                continue
            except Exception as first_exc:
                # A failed large request is not retried at the same size. Retry
                # that exact group immediately as pairs, as requested.
                if len(original_batch) <= 2:
                    raise RuntimeError(
                        f"Batch with {len(original_batch)} job(s) failed: {first_exc}"
                    ) from first_exc
                self.after(
                    0,
                    lambda size=len(original_batch), err=str(first_exc):
                        self._report_ai_detail_batch_retry(chat_window, size, err),
                )

                for pair_start in range(0, len(original_batch), 2):
                    pair = original_batch[pair_start:pair_start + 2]
                    try:
                        result = run_batch(pair)
                        merge_usage(self.ai_service.consume_last_usage())
                    except Exception as pair_exc:
                        raise RuntimeError(
                            f"Retry batch with {len(pair)} job(s) failed; analysis aborted: {pair_exc}"
                        ) from pair_exc
                    completed += len(pair)
                    self.after(
                        0,
                        lambda result=result, batch=list(pair), done=completed, total=total:
                            self._apply_ai_detail_batch(result, chat_window, batch, done, total),
                    )
                index += len(original_batch)

        self.after(
            0,
            lambda done=completed, total=total, usage=dict(usage_totals):
                self._finish_ai_detail_batches(chat_window, done, total, usage),
        )

    def _report_ai_detail_batch_retry(self, chat_window, failed_size: int, error_message: str) -> None:
        message = (
            f"A detail-analysis batch of {failed_size} jobs failed. "
            f"Retrying that batch immediately in groups of 2.\n{error_message}"
        )
        self._append_ai_chat_text(chat_window, "System", message)
        self._set_ai_chat_busy(chat_window, True, "Large batch failed; retrying in groups of 2...")
        self.status_label_var.set("AI detail batch failed; retrying in groups of 2...")

    def _apply_ai_detail_batch(
        self,
        result: dict,
        chat_window,
        batch_payloads: list[dict],
        completed: int,
        total: int,
    ) -> None:
        self._finish_ai_detail_analysis(
            result,
            chat_window,
            usage=None,
            payloads_override=batch_payloads,
            finalize=False,
        )
        self._set_ai_chat_busy(
            chat_window, True, f"AI detail analysis: {completed} / {total} jobs completed..."
        )
        self.status_label_var.set(f"AI detail analysis: {completed} / {total} jobs completed...")

    def _finish_ai_detail_batches(
        self, chat_window, completed: int, total: int, usage: dict | None = None
    ) -> None:
        token_var = chat_window.get("token_var") if isinstance(chat_window, dict) else None
        if token_var is not None:
            token_var.set(self._format_token_usage(usage))
        self._show_ai_chat_input(chat_window)
        self._set_ai_chat_busy(
            chat_window, False, f"AI detail analysis finished. Updated {completed} / {total} jobs."
        )
        self.status_label_var.set(
            f"AI detail analysis finished. Updated {completed} / {total} jobs."
        )
        self._finish_background_ai_task(chat_window.get("background_task_id", -1), True)

    def _finish_ai_detail_analysis(
        self,
        result: dict,
        chat_window,
        usage: dict | None = None,
        *,
        payloads_override: list[dict] | None = None,
        finalize: bool = True,
    ) -> None:
        token_var = chat_window.get("token_var") if isinstance(chat_window, dict) else None
        if token_var is not None and usage is not None:
            token_var.set(self._format_token_usage(usage))
        items = result.get("items", []) if isinstance(result, dict) else []
        payloads = (
            payloads_override
            if payloads_override is not None
            else (chat_window.get("payloads", []) if isinstance(chat_window, dict) else [])
        )
        requested_iids = [str(payload.get("iid") or "").strip() for payload in payloads if isinstance(payload, dict)]
        requested_iids = [iid for iid in requested_iids if iid]
        requested_set = set(requested_iids)
        used_iids: set[str] = set()
        updated_iids: list[str] = []
        updated = 0
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            returned_iid = str(item.get("iid") or "").strip()
            iid = returned_iid if returned_iid in requested_set and returned_iid not in used_iids else ""
            # Models occasionally alter or normalize an opaque Treeview iid.
            # Fall back to the corresponding requested payload instead of
            # updating a different row or creating a detached temporary entry.
            if not iid and index < len(requested_iids):
                candidate_iid = requested_iids[index]
                if candidate_iid not in used_iids:
                    iid = candidate_iid
            if not iid:
                iid = next((candidate for candidate in requested_iids if candidate not in used_iids), "")
            if not iid:
                continue
            used_iids.add(iid)
            model_config = chat_window.get("model") if isinstance(chat_window, dict) else None
            score_value = self._coerce_ai_score(item.get("score"))
            previous_screening = self.temp_ai_screening_details_by_iid.get(iid, {})
            summary = str(item.get("summary") or "")
            reject_reason = str(
                item.get("reject_reason")
                or previous_screening.get("reject_reason")
                or previous_screening.get("reason")
                or ""
            ).strip()
            item_tags = item.get("rejection_tags") if isinstance(item.get("rejection_tags"), list) else []
            rejection_tags = item_tags or (
                previous_screening.get("rejection_tags")
                if isinstance(previous_screening.get("rejection_tags"), list)
                else []
            )
            if reject_reason and "rejection reason:" not in summary.casefold():
                appendix = ["", f"Rejection reason: {reject_reason}"]
                if rejection_tags:
                    appendix.append(f"Would use rejection tags: {', '.join(str(tag) for tag in rejection_tags)}")
                summary = summary.rstrip() + "\n" + "\n".join(appendix)
            self.temp_ai_evaluations_by_iid[iid] = {
                "ai_name": getattr(model_config, "name", "AI"),
                "provider": getattr(model_config, "provider", ""),
                "model": getattr(model_config, "model", ""),
                "created_at": utc_now_iso(),
                "summary": summary,
                "rating": str(item.get("rating") or ""),
                "score": score_value,
                "decision": str(item.get("decision") or ""),
                "reject_reason": reject_reason,
                "rejection_tags": [str(tag).strip() for tag in rejection_tags if str(tag).strip()],
                "raw_response": str(result.get("raw_response") or ""),
            }
            job = self._get_display_job(iid)
            if score_value is not None and job is not None:
                job.manual_score = score_value
            self.ai_screening_marks.pop(iid, None)
            self.temp_ai_screening_scores_by_iid.pop(iid, None)
            self.temp_ai_screening_details_by_iid.pop(iid, None)
            self._apply_ai_industry_suggestion(iid, item, job)
            self._update_dirty_indicators_for_iid(iid)
            updated_iids.append(iid)
            updated += 1

        # Never change the user's current table selection when a background AI job finishes.
        # Only refresh the detail tabs when the updated job is already selected.
        if self.selected_iid in updated_iids:
            selected_job = self._get_display_job(self.selected_iid)
            if selected_job is not None:
                self._load_ai_details_for_job(selected_job)
                self._update_detail_tab_labels()
        format_context = dict(chat_window) if isinstance(chat_window, dict) else {}
        format_context["payloads"] = payloads
        chat_text = self._format_ai_detail_chat_result(items, format_context, updated)
        self._append_ai_chat_text(chat_window, "AI", chat_text, model_name=self._chat_model_label(chat_window))
        if finalize:
            self._show_ai_chat_input(chat_window)
            self._set_ai_chat_busy(chat_window, False, f"AI detail analysis finished. Updated {updated} temporary evaluations.")
            self.status_label_var.set(f"AI detail analysis finished. Updated {updated} temporary evaluations.")
            self._finish_background_ai_task(chat_window.get("background_task_id", -1), True)

    def _format_ai_detail_chat_result(self, items: list, chat_window, updated: int) -> str:
        payloads = chat_window.get("payloads", []) if isinstance(chat_window, dict) else []
        by_iid = {str(payload.get("iid") or ""): payload for payload in payloads if isinstance(payload, dict)}
        lines = ["## Detailanalyse abgeschlossen", "", f"Temporäre AI Summary/Rating aktualisiert für **{updated}** Job(s).", "", "### Kurzfazit"]
        if not items:
            lines.append("- Keine auswertbaren Einträge in der AI-Antwort gefunden.")
            return "\n".join(lines)
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("iid") or "")
            payload = by_iid.get(iid, {})
            table = payload.get("table", {}) if isinstance(payload, dict) else {}
            title = str(table.get("title") or iid or "Job")
            company = str(table.get("company") or "")
            decision = str(item.get("decision") or "").strip()
            score = item.get("score", "")
            conclusion = self._extract_ai_conclusion(str(item.get("summary") or "") + "\n" + str(item.get("rating") or ""))
            head = f"**{title}**"
            if company:
                head += f" — {company}"
            suffix = ""
            if decision or score != "":
                suffix = f" ({decision}" + (f", {score}" if score != "" else "") + ")"
            lines.append(f"- {head}{suffix}: {conclusion}")
        lines.extend(["", "Use **Save selected in database** to persist these temporary AI texts."])
        return "\n".join(lines)

    @staticmethod
    def _extract_ai_conclusion(text: str) -> str:
        for raw_line in str(text or "").splitlines():
            stripped = raw_line.strip().lstrip("-•* ").strip()
            lower = stripped.lower()
            if any(key in lower for key in ["kurzfazit", "gesamturteil", "empfehlung", "fazit"]):
                if ":" in stripped:
                    stripped = stripped.split(":", 1)[1].strip()
                stripped = stripped.replace("**", "").strip()
                if stripped:
                    return stripped[:260]
        compact = " ".join(str(text or "").replace("**", "").split())
        return (compact[:260] + "…") if len(compact) > 260 else (compact or "Kein Kurzfazit geliefert.")

    def _fail_ai_detail_analysis(self, error_message: str, chat_window) -> None:
        self._append_ai_chat_text(chat_window, "System", f"AI detail analysis failed: {error_message}")
        self._show_ai_chat_input(chat_window)
        self._set_ai_chat_busy(chat_window, False, "AI detail analysis failed.")
        self.status_label_var.set("AI detail analysis failed.")
        self._finish_background_ai_task(chat_window.get("background_task_id", -1), False)

    def _open_ai_detail_window(self, payloads: list[dict], model_config: AiModelConfig):
        chat = self._open_ai_screening_window([], model_config, task="detailed_analysis")
        chat["mode"] = "job" if len(payloads) == 1 else "detail_multi"
        chat["job_context"] = {"jobs": payloads}
        chat["payloads"] = payloads
        chat["rows"] = [payload.get("table", {}) for payload in payloads]
        self._update_chat_context_estimate(chat)
        try:
            chat["dialog"].title("AI detail analysis")
        except Exception:
            pass
        self._append_ai_chat_text(chat, "System", f"Ready. Choose an AI model and click **Start analysis** to analyze {len(payloads)} selected jobs with full descriptions, notes and raw data. Results will be temporary until saved.")
        apply_button = chat.get("apply_marks_button")
        if apply_button is not None:
            apply_button.pack_forget()
        return chat

    def ai_ask_about_selected_job(self, field_name: str = "") -> None:
        jobs = self._selected_jobs_for_ai()
        if len(jobs) != 1:
            self.status_label_var.set("Ask AI requires exactly one selected job.")
            return
        job = jobs[0]
        model_config = self._default_ai_model_config("chat")
        if model_config is None:
            messagebox.showwarning("Ask AI", "No enabled AI model is configured in Settings → AI.")
            return
        chat = self._open_ai_job_chat_window(job, model_config, field_name=field_name)
        start_button = chat.get("start_button")
        if start_button is not None:
            start_button.pack_forget()
        apply_button = chat.get("apply_marks_button")
        if apply_button is not None:
            apply_button.pack_forget()
        regen_button = chat.get("regenerate_button")
        if regen_button is not None:
            regen_button.configure(state=tk.NORMAL)
        self._show_ai_chat_input(chat)
        self._append_ai_chat_text(chat, "System", "Ask questions about this job. Job description, AI summary, notes and AI rating are included as context.")

    def _open_ai_job_chat_window(self, job: DisplayJob, model_config: AiModelConfig, field_name: str = ""):
        chat = self._open_ai_screening_window([], model_config, task="chat")
        chat["mode"] = "job"
        context = self._ai_detail_payload(job)
        if field_name:
            context["focused_field"] = field_name
        chat["job_context"] = context
        chat["rows"] = [self._ai_screening_row(job)]
        context_vars = chat.get("context_vars") or {}
        if "chat_history" in context_vars:
            context_vars["chat_history"].set(True)
        if "job_data" in context_vars:
            context_vars["job_data"].set(True)
        has_analysis = bool(context.get("ai_summary") or context.get("ai_rating"))
        if "ai_analysis" in context_vars:
            context_vars["ai_analysis"].set(has_analysis or field_name in {"ai_summary", "ai_rating"})
        if "notes" in context_vars:
            context_vars["notes"].set(False)
        if "user_profile" in context_vars:
            context_vars["user_profile"].set(False)
        self._update_chat_context_estimate(chat)
        if job.db_id is not None:
            chat["persist_chat"] = True
            chat["job_db_id"] = job.db_id
            history = self.db.list_ai_chat_messages(job.db_id)
            if history:
                chat["conversation"] = []
                for msg in history:
                    role = str(msg.get("role") or "")
                    content = str(msg.get("content") or "")
                    model = str(msg.get("model") or "")
                    if role == "user":
                        chat["conversation"].append({"role": "user", "content": content})
                        self._append_ai_chat_text(chat, "You", content, persist=False)
                    elif role == "assistant":
                        chat["conversation"].append({"role": "assistant", "content": content})
                        self._append_ai_chat_text(chat, "AI", content, model_name=model, persist=False)
        try:
            title = f"Ask AI — {job.title}" + (f" — {field_name}" if field_name else "")
            chat["dialog"].title(title)
        except Exception:
            pass
        return chat

    def update_selected_job(self) -> None:
        selection = list(self.tree.selection())
        if not selection and self.selected_iid:
            selection = [self.selected_iid]
        if not selection:
            return

        jobs_to_update: list[dict] = []
        skipped: list[str] = []
        for iid in selection:
            job = self._get_display_job(iid)
            if job is None:
                continue
            label = f"{job.title} — {job.company}".strip(" —")
            if job.db_id is None:
                skipped.append(f"{label}: not saved")
                continue
            if job.source.lower() != "ba":
                skipped.append(f"{label}: source is {job.source}")
                continue
            if not job.source_job_id:
                skipped.append(f"{label}: missing BA reference")
                continue
            jobs_to_update.append({"job_id": job.db_id, "source_job_id": job.source_job_id, "label": label})

        if not jobs_to_update:
            messagebox.showinfo(
                "Update",
                "No saved BA jobs with a reference number selected.\n\n" + "\n".join(skipped[:10]),
            )
            return

        self.status_label_var.set(f"Updating {len(jobs_to_update)} job(s)...")
        threading.Thread(target=self._update_ba_jobs_worker, args=(jobs_to_update,), daemon=True).start()

    def _update_ba_jobs_worker(self, jobs_to_update: list[dict]) -> None:
        results: list[dict] = []
        for item in jobs_to_update:
            try:
                candidate = self.ba_importer.fetch_candidate_by_refnr(item["source_job_id"])
                results.append({**item, "status": "found", "candidate": candidate})
            except Exception as exc:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code in {404, 410}:
                    results.append({**item, "status": "expired"})
                else:
                    results.append({**item, "status": "error", "error": str(exc)})
        self.after(0, lambda results=results: self._finish_ba_jobs_update(results))

    def _finish_ba_jobs_update(self, results: list[dict]) -> None:
        changed: list[str] = []
        unchanged: list[str] = []
        expired: list[str] = []
        errors: list[str] = []

        for item in results:
            label = str(item.get("label") or item.get("job_id") or "")
            status = item.get("status")
            if status == "found":
                try:
                    _updated_job_id, _is_new, content_changed = self.db.add_or_update_job(item["candidate"])
                    if content_changed:
                        changed.append(label)
                    else:
                        unchanged.append(label)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            elif status == "expired":
                try:
                    self.db.update_jobs_status([int(item["job_id"])], self._expired_status(), "BA update did not find this job anymore")
                    expired.append(label)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            elif status == "error":
                errors.append(f"{label}: {item.get('error', '')}")

        self.refresh_jobs()
        total = len(results)
        if total == 1:
            if changed:
                messagebox.showinfo("Update", "Job data changed. A new version was stored.")
                self.status_label_var.set("Job updated.")
            elif expired:
                messagebox.showinfo("Update", "Job was not found anymore and was marked as expired.")
                self.status_label_var.set("Job marked expired.")
            elif errors:
                messagebox.showerror("Update failed", errors[0])
                self.status_label_var.set("Update failed")
            else:
                messagebox.showinfo("Update", "No changes detected.")
                self.status_label_var.set("Job unchanged.")
            return

        lines = [f"Updated {total} selected jobs."]
        if changed:
            lines.append("\nChanged:")
            lines.extend(f"- {name}" for name in changed[:20])
            if len(changed) > 20:
                lines.append(f"- ... and {len(changed) - 20} more")
        if expired:
            lines.append("\nExpired / not found:")
            lines.extend(f"- {name}" for name in expired[:20])
            if len(expired) > 20:
                lines.append(f"- ... and {len(expired) - 20} more")
        if errors:
            lines.append("\nErrors:")
            lines.extend(f"- {err}" for err in errors[:10])
            if len(errors) > 10:
                lines.append(f"- ... and {len(errors) - 10} more")
        if not changed and not expired and not errors:
            lines.append("\nNo changes detected.")
        messagebox.showinfo("Update", "\n".join(lines))
        self.status_label_var.set(f"Update finished: {len(changed)} changed, {len(expired)} expired, {len(errors)} errors.")

    # Backward-compatible wrappers kept for old callbacks/tests.
    def _update_ba_job_worker(self, job_id: int, source_job_id: str) -> None:
        self._update_ba_jobs_worker([{"job_id": job_id, "source_job_id": source_job_id, "label": str(job_id)}])

    def _finish_ba_job_update(self, job_id: int, candidate: JobCandidate) -> None:
        self._finish_ba_jobs_update([{"job_id": job_id, "label": str(job_id), "status": "found", "candidate": candidate}])

    def _mark_job_expired_after_update(self, job_id: int) -> None:
        self._finish_ba_jobs_update([{"job_id": job_id, "label": str(job_id), "status": "expired"}])

    def _show_update_error(self, error_message: str) -> None:
        messagebox.showerror("Update failed", error_message)
        self.status_label_var.set("Update failed")

    def set_selected_location_for_selected_job(self, index: int) -> None:
        job = self._get_display_job(self.selected_iid)
        if job is None:
            return
        locations = self._ba_locations(job)
        if index < 0 or index >= len(locations):
            return
        location_text = self._format_ba_location_label(locations[index])
        location_country = self._location_country(locations[index])
        if job.db_id is not None:
            self.db.save_job_selected_location(job.db_id, index, location_text, manual=True, country=location_country)
        else:
            candidate = self.pending_by_iid.get(job.iid)
            if candidate is not None:
                candidate.selected_location_index = index
                candidate.selected_location_manual = True
                candidate.location = location_text
                if location_country:
                    candidate.country = location_country
                candidate.route_distance_km = None
                candidate.route_duration_min = None
                candidate.route_address_text = ""
                candidate.route_locked = False
                candidate.route_quality = ""
                self.temp_routes_by_iid.pop(job.iid, None)
        self.refresh_jobs()

    def set_status_for_selected(self, status: str) -> None:
        selection = list(self.tree.selection())
        if not selection:
            return
        reason = ""
        application_details = None
        if self._is_rejected_by_me_status(status):
            chosen_reason = self.ask_reject_reason(initial_reasons=self._suggested_reject_reasons_for_iids(selection))
            if chosen_reason is None:
                return
            reason = chosen_reason
        if self._is_applied_status(status):
            transitioning = any(
                (job := self._get_display_job(iid)) is not None and not self._is_applied_status(job.status)
                for iid in selection
            )
            if transitioning:
                application_details = self._application_details_dialog(self)
                if application_details is None:
                    return
        for iid in selection:
            job = self._get_display_job(iid)
            if job is None:
                continue
            if job.db_id is None:
                job_id = self._save_pending_job(iid, status, branch=job.branch, notes=job.notes, reject_reason=reason)
                if job_id is not None:
                    self._save_application_details_for_job(job_id, application_details)
            else:
                self.db.update_jobs_status([job.db_id], status, reason)
                self._save_application_details_for_job(job.db_id, application_details)
        self.refresh_jobs()

    def add_new_industry_for_selected_companies(self) -> None:
        name = simpledialog.askstring("Add industry", "Industry:", parent=self)
        if not name or not name.strip():
            return
        self.set_industry_for_selected_companies(name.strip())

    def set_industry_for_selected_companies(self, industry: str) -> None:
        selection = list(self.tree.selection())
        if not selection:
            return
        for iid in selection:
            job = self._get_display_job(iid)
            if job is not None and job.company:
                self.db.set_company_branch(job.company, industry, commit=False)
        self.db.connection.commit()
        self.company_rating_combo.configure(values=self.db.list_company_ratings())
        self.refresh_jobs()

    def copy_selected_job_field(self, field: str) -> None:
        job = self._get_display_job(self.selected_iid)
        if job is None:
            return
        if field == "title":
            text = job.title
        elif field == "company":
            text = job.company
        elif field == "location":
            text = self._display_location(job)
        elif field == "exact_location":
            text = self._exact_location_text(job)
        elif field == "raw_data":
            text = self._raw_entry_text(job)
        else:
            values = self.tree.item(job.iid, "values")
            text = "; ".join(str(value) for value in values)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_label_var.set("Copied to clipboard.")

    def show_context_menu(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            if row_id not in self.tree.selection():
                self.tree.selection_set(row_id)
            self.selected_iid = row_id
            self._rebuild_context_menu()
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def add_selected_company_to_blacklist(self) -> None:
        job = self._get_display_job(self.selected_iid)
        if job is None:
            return

        company = job.company.strip()
        if not company:
            return

        answer = messagebox.askyesnocancel(
            "Blacklist company",
            f"Add this company to the blacklist and reject all exact company matches?\n\n{company}",
            parent=self,
        )
        if answer is not True:
            return

        reason = self.ask_reject_reason()
        if reason is None:
            return

        # Only add the company after the reject-reason dialog was confirmed.
        # Cancelling either dialog leaves both blacklist and jobs unchanged.
        self.db.add_company_to_blacklist(company)
        rejected_status = self._rejected_by_me_status()
        stored_matches = [
            record.id
            for record in self.db.list_jobs(
                status_filter="all",
                text_filter="",
                hide_rejected=False,
                show_only_new=False,
            )
            if record.company.strip() == company
        ]
        if stored_matches:
            self.db.update_jobs_status(stored_matches, rejected_status, reason)

        saved_pending = 0
        for iid, candidate in list(self.pending_by_iid.items()):
            if candidate.company.strip() != company:
                continue
            display = self._get_display_job(iid)
            branch = display.branch if display is not None else ""
            notes = display.notes if display is not None else ""
            if self._save_pending_job(
                iid,
                rejected_status,
                branch=branch,
                notes=notes,
                reject_reason=reason,
            ) is not None:
                saved_pending += 1

        self.refresh_jobs()
        self.status_label_var.set(
            f"Blacklisted {company}. Rejected {len(stored_matches)} stored and "
            f"{saved_pending} new* matching jobs."
        )

    def edit_blacklist_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Edit blacklist")
        dialog.geometry("600x450")
        dialog.transient(self)
        self._center_child_window(dialog, width=850, height=650)
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(frame, selectmode=tk.BROWSE)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        def reload_list() -> None:
            listbox.delete(0, tk.END)
            for company in self.db.list_blacklisted_companies():
                listbox.insert(tk.END, company)

        def selected_company() -> str | None:
            selection = listbox.curselection()
            return str(listbox.get(selection[0])) if selection else None

        def add() -> None:
            name = simpledialog.askstring("Add company", "Company name:", parent=dialog)
            if name:
                self.db.add_company_to_blacklist(name)
                reload_list(); self.refresh_jobs()

        def remove() -> None:
            name = selected_company()
            if name:
                self.db.remove_company_from_blacklist(name)
                reload_list(); self.refresh_jobs()

        def edit() -> None:
            old = selected_company()
            if not old:
                return
            new = simpledialog.askstring("Edit company", "Company name:", initialvalue=old, parent=dialog)
            if new and new.strip() != old:
                self.db.rename_blacklisted_company(old, new)
                reload_list(); self.refresh_jobs()

        buttonbar = ttk.Frame(frame)
        buttonbar.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttonbar, text="Add", command=add).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttonbar, text="Edit", command=edit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttonbar, text="Remove", command=remove).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttonbar, text="Close", command=dialog.destroy).pack(side=tk.LEFT)
        reload_list()

    def show_known_companies_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Known companies")
        dialog.geometry("920x620")
        dialog.transient(self)
        self._center_child_window(dialog, width=850, height=650)
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        columns = ("company", "branch", "blacklisted")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {"company": "Company", "branch": "Industry", "blacklisted": "Blacklisted"}
        widths = {"company": 520, "branch": 210, "blacklisted": 100}
        sort_state = {"column": "company", "reverse": False}

        def sort_tree(column: str) -> None:
            if sort_state["column"] == column:
                sort_state["reverse"] = not sort_state["reverse"]
            else:
                sort_state["column"] = column
                sort_state["reverse"] = False
            reload()

        for column in columns:
            tree.heading(column, text=headings[column], command=lambda c=column: sort_tree(c))
            tree.column(column, width=widths[column], stretch=False)
        tree.tag_configure("blacklisted", background="#4a2426", foreground=DARK_THEME["fg"])
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.grid(row=0, column=0, columnspan=5, sticky="nsew")
        yscroll.grid(row=0, column=5, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        branch_var = tk.StringVar()
        blacklisted_var = tk.BooleanVar()
        ttk.Label(frame, text="Industry:").grid(row=1, column=0, sticky="e", pady=(8, 0))
        branch_combo = ttk.Combobox(frame, textvariable=branch_var, values=self.db.list_company_ratings(), width=26)
        branch_combo.grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(frame, text="Blacklisted", variable=blacklisted_var).grid(row=1, column=2, sticky="w", pady=(8, 0))

        def reload() -> None:
            selected = tree.selection()[0] if tree.selection() else None
            rows = self.db.list_company_profiles()
            column = str(sort_state["column"])
            reverse = bool(sort_state["reverse"])

            def value(row: dict[str, object]) -> str:
                if column == "blacklisted":
                    return "yes" if row["blacklisted"] else "no"
                if column == "branch":
                    return str(row["branch"] or "").casefold()
                return str(row["company"] or "").casefold()

            rows = sorted(rows, key=value, reverse=reverse)
            tree.delete(*tree.get_children())
            for row in rows:
                tags = ("blacklisted",) if row["blacklisted"] else ()
                tree.insert(
                    "",
                    tk.END,
                    iid=str(row["company"]),
                    values=(row["company"], row["branch"], "yes" if row["blacklisted"] else "no"),
                    tags=tags,
                )
            if selected and tree.exists(selected):
                tree.selection_set(selected)

        def refresh_industry_values() -> None:
            values = self.db.list_company_ratings()
            branch_combo.configure(values=values)
            self.company_rating_combo.configure(values=values)

        def on_select(_event: object | None = None) -> None:
            sel = tree.selection()
            if not sel:
                return
            values = tree.item(sel[0], "values")
            branch_var.set(str(values[1]))
            blacklisted_var.set(str(values[2]) == "yes")

        def save_company() -> None:
            sel = tree.selection()
            if not sel:
                return
            company = str(sel[0])
            self.db.update_company_profile(company, branch_var.get().strip(), blacklisted_var.get())
            refresh_industry_values()
            reload(); self.refresh_jobs()

        def add_industry() -> None:
            name = simpledialog.askstring("Add industry", "Industry:", parent=dialog)
            if not name or not name.strip():
                return
            self.db.add_company_rating(name.strip())
            branch_var.set(name.strip())
            refresh_industry_values()

        def edit_industry() -> None:
            old = branch_var.get().strip()
            if not old:
                messagebox.showinfo("No industry", "Select or enter an industry first.", parent=dialog)
                return
            new = simpledialog.askstring("Edit industry", "Industry:", initialvalue=old, parent=dialog)
            if not new or not new.strip() or new.strip() == old:
                return
            self.db.rename_company_rating(old, new.strip())
            branch_var.set(new.strip())
            refresh_industry_values()
            reload(); self.refresh_jobs()

        def delete_industry() -> None:
            name = branch_var.get().strip()
            if not name:
                messagebox.showinfo("No industry", "Select or enter an industry first.", parent=dialog)
                return
            if not messagebox.askyesno(
                "Delete industry",
                f"Delete industry '{name}'?\n\nAffected companies and jobs will lose this industry value.",
                parent=dialog,
            ):
                return
            self.db.delete_company_rating(name)
            branch_var.set("")
            refresh_industry_values()
            reload(); self.refresh_jobs()

        tree.bind("<<TreeviewSelect>>", on_select)
        ttk.Button(frame, text="Save company", command=save_company).grid(row=1, column=3, sticky="e", pady=(8, 0), padx=(8, 0))
        ttk.Button(frame, text="Close", command=dialog.destroy).grid(row=1, column=4, sticky="e", pady=(8, 0), padx=(8, 0))

        industry_buttons = ttk.Frame(frame)
        industry_buttons.grid(row=2, column=1, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(industry_buttons, text="Add industry", command=add_industry).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(industry_buttons, text="Edit selected industry", command=edit_industry).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(industry_buttons, text="Delete selected industry", command=delete_industry).pack(side=tk.LEFT)
        reload()


    def _raw_entry_display_payload(self, job: DisplayJob) -> dict:
        data = dict(self._load_raw_entry_for_job(job) or {})
        data.update({
            "source": job.source,
            "source_job_id": job.source_job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
            "description": job.description,
            "formatted_description": job.formatted_description,
            "published_date": job.published_date,
            "status": job.status,
            "score": self._score_for_edit_dialog(job),
            "industry": job.branch,
            "notes": job.notes,
            "min_salary_k": job.min_salary_k,
            "max_salary_k": job.max_salary_k,
            "fixed_term": job.fixed_term,
            "selected_location_index": job.selected_location_index,
            "selected_location_manual": job.selected_location_manual,
            "route_distance_km": job.route_distance_km,
            "route_duration_min": job.route_duration_min,
            "route_address_text": job.route_address_text,
            "route_quality": job.route_quality,
        })
        return data

    def show_raw_json_for_selected_job(self) -> None:
        display_job = self._get_display_job(self.selected_iid)
        if display_job is None:
            return
        raw_payload = self._raw_entry_display_payload(display_job)
        raw_json = json.dumps(raw_payload, ensure_ascii=False, indent=4)
        title_suffix = display_job.iid if display_job.db_id is None else str(display_job.db_id)
        dialog = tk.Toplevel(self)
        dialog.title(f"Raw entry for job {title_suffix}")
        dialog.geometry("1000x700")
        text = tk.Text(dialog, wrap=tk.NONE)
        yscroll = ttk.Scrollbar(dialog, orient=tk.VERTICAL, command=text.yview)
        xscroll = ttk.Scrollbar(dialog, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        if raw_json:
            try:
                pretty = json.dumps(json.loads(raw_json), indent=4, ensure_ascii=False)
                text.insert(tk.END, pretty)
            except Exception:
                text.insert(tk.END, raw_json)
        else:
            text.insert(tk.END, "No raw JSON stored for this job.")
        text.configure(state=tk.DISABLED)
