from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


STATUS_VALUES = [
    "new",
    "needs review",
    "interesting",
    "maybe",
    "apply",
    "applied",
    "interview",
    "rejected by me",
    "rejected by company",
    "expired",
]

# Keep fresh-install industry/branch defaults neutral. Users can add the
# categories that are useful for their own job search in Settings.
DEFAULT_BRANCHES = []

# Backward-compatible name used by older code paths.
DEFAULT_COMPANY_RATINGS = DEFAULT_BRANCHES


@dataclass
class JobCandidate:
    """A raw job found by an importer before it is stored in the database."""

    source: str
    source_job_id: str
    title: str
    company: str
    location: str = ""
    country: str = ""
    url: str = ""
    description: str = ""
    formatted_description: str = ""
    published_date: str = ""
    raw_json: str = ""
    min_salary_k: Optional[float] = None
    max_salary_k: Optional[float] = None
    fixed_term: str = "-"
    created_at: str = ""
    updated_at: str = ""
    status_changed_at: str = ""
    applied_at: str = ""
    interview_at: str = ""
    rejected_by_me_at: str = ""
    rejected_by_company_at: str = ""
    expired_at: str = ""
    contract_at: str = ""
    application_expected_salary: str = ""
    application_available_from: str = ""
    application_via: str = ""
    application_info: str = ""
    application_notes: str = ""
    route_distance_km: Optional[float] = None
    route_duration_min: Optional[float] = None
    route_address_text: str = ""
    route_locked: bool = False
    route_quality: str = ""
    selected_location_index: int = 0
    selected_location_manual: bool = False
    duplicate_score: float = 0.0
    duplicate_of_job_id: Optional[int] = None


@dataclass
class JobRecord:
    """A job as stored in the database."""

    id: int
    source: str
    source_job_id: str
    title: str
    company: str
    location: str
    country: str
    url: str
    description: str
    formatted_description: str
    published_date: str
    first_seen: str
    last_seen: str
    status: str
    manual_score: Optional[int]
    company_rating: str
    notes: str
    content_hash: str
    min_salary_k: Optional[float] = None
    max_salary_k: Optional[float] = None
    fixed_term: str = "-"
    created_at: str = ""
    updated_at: str = ""
    status_changed_at: str = ""
    applied_at: str = ""
    interview_at: str = ""
    rejected_by_me_at: str = ""
    rejected_by_company_at: str = ""
    expired_at: str = ""
    contract_at: str = ""
    application_expected_salary: str = ""
    application_available_from: str = ""
    application_via: str = ""
    application_info: str = ""
    application_notes: str = ""
    route_distance_km: Optional[float] = None
    route_duration_min: Optional[float] = None
    route_address_text: str = ""
    route_locked: bool = False
    route_quality: str = ""
    selected_location_index: int = 0
    selected_location_manual: bool = False
    duplicate_score: float = 0.0
    duplicate_of_job_id: Optional[int] = None


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
