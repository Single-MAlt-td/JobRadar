from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import DEFAULT_BRANCHES, JobCandidate, JobRecord, utc_now_iso


class JobDatabase:
    """
    Small wrapper around SQLite.

    You do not need to know SQL in the rest of the app.
    Use methods like add_or_update_job(), list_jobs(), save_user_fields().
    """

    def __init__(self, db_path: str | Path = "jobradar.db") -> None:
        self.db_path = Path(db_path)
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.initialize()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_job_id TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT DEFAULT '',
                country TEXT DEFAULT '',
                url TEXT DEFAULT '',
                description TEXT DEFAULT '',
                formatted_description TEXT DEFAULT '',
                published_date TEXT DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                status_changed_at TEXT DEFAULT '',
                applied_at TEXT DEFAULT '',
                interview_at TEXT DEFAULT '',
                rejected_by_me_at TEXT DEFAULT '',
                rejected_by_company_at TEXT DEFAULT '',
                expired_at TEXT DEFAULT '',
                contract_at TEXT DEFAULT '',
                application_expected_salary TEXT DEFAULT '',
                application_available_from TEXT DEFAULT '',
                application_via TEXT DEFAULT '',
                application_info TEXT DEFAULT '',
                application_notes TEXT DEFAULT '',
                route_distance_km REAL,
                route_duration_min REAL,
                route_address_text TEXT DEFAULT '',
                route_locked INTEGER NOT NULL DEFAULT 0,
                route_quality TEXT DEFAULT '',
                selected_location_index INTEGER NOT NULL DEFAULT 0,
                selected_location_manual INTEGER NOT NULL DEFAULT 0,
                fixed_term TEXT DEFAULT '-',
                status TEXT NOT NULL DEFAULT 'new',
                manual_score INTEGER,
                company_rating TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                content_hash TEXT NOT NULL,
                duplicate_score REAL NOT NULL DEFAULT 0,
                duplicate_of_job_id INTEGER,
                UNIQUE(source, source_job_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                seen_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT DEFAULT '',
                description TEXT DEFAULT '',
                raw_json TEXT DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                old_status TEXT DEFAULT '',
                new_status TEXT NOT NULL,
                reason TEXT DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_duplicate_relations (
                job_id INTEGER NOT NULL,
                candidate_job_id INTEGER NOT NULL,
                score REAL NOT NULL DEFAULT 0,
                decision TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, candidate_job_id),
                FOREIGN KEY(job_id) REFERENCES jobs(id),
                FOREIGN KEY(candidate_job_id) REFERENCES jobs(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                ai_name TEXT NOT NULL,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                summary TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER,
                decision TEXT DEFAULT '',
                raw_response TEXT DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                category TEXT DEFAULT 'preference',
                source TEXT DEFAULT 'auto',
                confidence REAL DEFAULT 1.0,
                importance REAL DEFAULT 1.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                embedding_json TEXT DEFAULT ''
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT DEFAULT '',
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS company_ratings (
                name TEXT PRIMARY KEY
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS company_profiles (
                company TEXT PRIMARY KEY,
                branch TEXT DEFAULT '',
                blacklisted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS company_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                homepage_url TEXT DEFAULT '',
                career_url TEXT NOT NULL,
                career_system TEXT DEFAULT 'unknown',
                priority INTEGER NOT NULL DEFAULT 3,
                locations TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT DEFAULT '',
                last_status TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(company_name, career_url)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address_text TEXT NOT NULL,
                normalized_address TEXT NOT NULL UNIQUE,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                display_name TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                geocoded_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_address_id INTEGER NOT NULL,
                to_address_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                quality TEXT DEFAULT 'exact',
                distance_km REAL NOT NULL,
                duration_min REAL NOT NULL,
                calculated_at TEXT NOT NULL,
                UNIQUE(from_address_id, to_address_id, provider),
                FOREIGN KEY(from_address_id) REFERENCES addresses(id),
                FOREIGN KEY(to_address_id) REFERENCES addresses(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS location_aliases (
                display_location TEXT PRIMARY KEY,
                routing_location TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._migrate_schema()
        self._insert_default_branches()
        self._backfill_salary_from_raw_json()
        self._backfill_fixed_term_from_raw_json()
        self._backfill_country_from_raw_json()
        self.connection.commit()

    def _migrate_schema(self) -> None:
        """Add columns to older databases without requiring manual SQL work."""
        self._ensure_column("jobs", "company_rating", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "country", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "formatted_description", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "min_salary_k", "REAL")
        self._ensure_column("jobs", "max_salary_k", "REAL")
        self._ensure_column("jobs", "fixed_term", "TEXT DEFAULT '-'")
        self._ensure_column("jobs", "reject_reason", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "created_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "updated_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "status_changed_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "applied_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "interview_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "rejected_by_me_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "duplicate_score", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("jobs", "duplicate_of_job_id", "INTEGER")
        self._ensure_column("jobs", "rejected_by_company_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "expired_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "contract_at", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "application_expected_salary", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "application_available_from", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "application_via", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "application_info", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "application_notes", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "route_distance_km", "REAL")
        self._ensure_column("jobs", "route_duration_min", "REAL")
        self._ensure_column("jobs", "route_address_text", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "route_locked", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("ai_evaluations", "reject_reason", "TEXT DEFAULT ''")
        self._ensure_column("ai_evaluations", "rejection_tags_json", "TEXT DEFAULT '[]'")
        self._ensure_column("jobs", "route_quality", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "selected_location_index", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("jobs", "selected_location_manual", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("routes", "quality", "TEXT DEFAULT 'exact'")
        self._ensure_column("ai_memories", "category", "TEXT DEFAULT 'preference'")
        self._ensure_column("ai_memories", "source", "TEXT DEFAULT 'auto'")
        self._ensure_column("ai_memories", "confidence", "REAL DEFAULT 1.0")
        self._ensure_column("ai_memories", "importance", "REAL DEFAULT 1.0")
        self._ensure_column("ai_memories", "active", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("ai_memories", "created_at", "TEXT DEFAULT ''")
        self._ensure_column("ai_memories", "updated_at", "TEXT DEFAULT ''")
        self._ensure_column("ai_memories", "embedding_json", "TEXT DEFAULT ''")
        self._ensure_column("ai_chat_messages", "job_id", "INTEGER")
        self._ensure_column("ai_chat_messages", "created_at", "TEXT DEFAULT ''")
        self._ensure_column("ai_chat_messages", "role", "TEXT DEFAULT ''")
        self._ensure_column("ai_chat_messages", "content", "TEXT DEFAULT ''")
        self._ensure_column("ai_chat_messages", "model", "TEXT DEFAULT ''")

        self._ensure_column("company_profiles", "branch", "TEXT DEFAULT ''")
        self._ensure_column("company_profiles", "blacklisted", "INTEGER NOT NULL DEFAULT 0")

        # Older versions had an optional UI value "ignored". Treat it like manually rejected.
        self.connection.execute("UPDATE jobs SET status = 'rejected_by_me' WHERE status = 'ignored'")
        self.connection.execute("UPDATE jobs SET created_at = first_seen WHERE created_at = '' OR created_at IS NULL")
        self.connection.execute("UPDATE jobs SET updated_at = last_seen WHERE updated_at = '' OR updated_at IS NULL")
        self.connection.execute("UPDATE jobs SET status_changed_at = updated_at WHERE status_changed_at = '' OR status_changed_at IS NULL")
        self.connection.execute("UPDATE jobs SET applied_at = updated_at WHERE status = 'applied' AND (applied_at = '' OR applied_at IS NULL)")
        self.connection.execute("UPDATE jobs SET interview_at = updated_at WHERE status = 'interview' AND (interview_at = '' OR interview_at IS NULL)")
        self.connection.execute("UPDATE jobs SET rejected_by_me_at = updated_at WHERE status = 'rejected_by_me' AND (rejected_by_me_at = '' OR rejected_by_me_at IS NULL)")
        self.connection.execute("UPDATE jobs SET rejected_by_company_at = updated_at WHERE status = 'rejected_by_company' AND (rejected_by_company_at = '' OR rejected_by_company_at IS NULL)")
        self.connection.execute("UPDATE jobs SET expired_at = updated_at WHERE status = 'expired' AND (expired_at = '' OR expired_at IS NULL)")
        self.connection.execute("UPDATE jobs SET contract_at = updated_at WHERE status = 'contract' AND (contract_at = '' OR contract_at IS NULL)")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row["name"]) for row in rows}
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _insert_default_branches(self) -> None:
        for branch in DEFAULT_BRANCHES:
            self.connection.execute(
                "INSERT OR IGNORE INTO company_ratings (name) VALUES (?)",
                (branch,),
            )

    @staticmethod
    def _salary_euro_to_k(value: object) -> Optional[float]:
        if value is None:
            return None
        try:
            return round(float(value) / 1000.0, 1)
        except (TypeError, ValueError):
            return None

    def _backfill_salary_from_raw_json(self) -> None:
        rows = self.connection.execute(
            """
            SELECT j.id, v.raw_json
            FROM jobs j
            JOIN job_versions v ON v.job_id = j.id
            WHERE (j.min_salary_k IS NULL OR j.max_salary_k IS NULL)
              AND v.raw_json != ''
            ORDER BY v.id DESC
            """
        ).fetchall()
        seen: set[int] = set()
        for row in rows:
            job_id = int(row["id"])
            if job_id in seen:
                continue
            seen.add(job_id)
            try:
                data = json.loads(row["raw_json"])
            except Exception:
                continue
            min_salary = self._salary_euro_to_k(data.get("gehaltsspanneVon"))
            max_salary = self._salary_euro_to_k(data.get("gehaltsspanneBis"))
            if min_salary is not None or max_salary is not None:
                self.connection.execute(
                    """
                    UPDATE jobs
                    SET min_salary_k = COALESCE(min_salary_k, ?),
                        max_salary_k = COALESCE(max_salary_k, ?)
                    WHERE id = ?
                    """,
                    (min_salary, max_salary, job_id),
                )

    @staticmethod
    def _status_key(status: str) -> str:
        return "".join(ch for ch in str(status or "").lower() if ch.isalnum())

    @classmethod
    def _timestamp_column_for_status(cls, status: str) -> Optional[str]:
        mapping = {
            "applied": "applied_at",
            "interview": "interview_at",
            "rejectedbyme": "rejected_by_me_at",
            "rejectedbycompany": "rejected_by_company_at",
            "expired": "expired_at",
            "contract": "contract_at",
        }
        return mapping.get(cls._status_key(status))

    @classmethod
    def _is_rejected_by_me_status(cls, status: str) -> bool:
        return cls._status_key(status) == "rejectedbyme"

    def migrate_status_aliases(self, aliases: dict[str, str]) -> None:
        """Rename existing DB status labels when the GUI switches to nicer labels."""
        for old, new in aliases.items():
            old = str(old or "").strip()
            new = str(new or "").strip()
            if old and new and old != new:
                self.connection.execute("UPDATE jobs SET status = ? WHERE status = ?", (new, old))
                self.connection.execute("UPDATE status_history SET old_status = ? WHERE old_status = ?", (new, old))
                self.connection.execute("UPDATE status_history SET new_status = ? WHERE new_status = ?", (new, old))
        self.connection.commit()

    def _record_status_change(self, job_id: int, old_status: str, new_status: str, reason: str = "", changed_at: Optional[str] = None) -> None:
        if old_status == new_status:
            return
        now = changed_at or utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO status_history (job_id, changed_at, old_status, new_status, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, now, old_status or "", new_status, reason.strip()),
        )
        timestamp_column = self._timestamp_column_for_status(new_status)
        if timestamp_column:
            self.connection.execute(
                f"UPDATE jobs SET {timestamp_column} = COALESCE(NULLIF({timestamp_column}, ''), ?) WHERE id = ?",
                (now, job_id),
            )

    def list_status_history(self, job_id: int) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT changed_at, old_status, new_status, reason
            FROM status_history
            WHERE job_id = ?
            ORDER BY changed_at ASC, id ASC
            """,
            (job_id,),
        ).fetchall()
        return [
            {
                "changed_at": str(row["changed_at"] or ""),
                "old_status": str(row["old_status"] or ""),
                "new_status": str(row["new_status"] or ""),
                "reason": str(row["reason"] or ""),
            }
            for row in rows
        ]

    @staticmethod
    def _fixed_term_from_raw_json(data: dict) -> str:
        manual = str(data.get("manual_fixed_term") or data.get("fixed_term") or "").strip()
        if manual and manual != "-":
            return manual
        duration = str(data.get("vertragsdauer") or "").upper()
        if duration == "BEFRISTET":
            until = str(data.get("befristetBis") or "").strip()
            if until:
                return until
            months = data.get("befristungInMonaten")
            if months is not None:
                try:
                    return f"{int(float(months))} mo"
                except (TypeError, ValueError):
                    pass
            return "FT"
        return "-"

    def _backfill_fixed_term_from_raw_json(self) -> None:
        rows = self.connection.execute(
            """
            SELECT j.id, v.raw_json
            FROM jobs j
            JOIN job_versions v ON v.job_id = j.id
            WHERE (j.fixed_term IS NULL OR j.fixed_term = '')
              AND v.raw_json != ''
            ORDER BY v.id DESC
            """
        ).fetchall()
        seen: set[int] = set()
        for row in rows:
            job_id = int(row["id"])
            if job_id in seen:
                continue
            seen.add(job_id)
            try:
                data = json.loads(row["raw_json"])
            except Exception:
                continue
            self.connection.execute(
                "UPDATE jobs SET fixed_term = ? WHERE id = ?",
                (self._fixed_term_from_raw_json(data), job_id),
            )

    def _backfill_country_from_raw_json(self) -> None:
        rows = self.connection.execute(
            "SELECT id FROM jobs WHERE country = '' OR country IS NULL"
        ).fetchall()
        for row in rows:
            raw = self.get_latest_raw_json(int(row["id"]))
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            country = str(data.get("country") or "").strip() if isinstance(data, dict) else ""
            if not country and isinstance(data, dict):
                locations = data.get("stellenlokationen") or []
                if isinstance(locations, list) and locations:
                    address = (locations[0] or {}).get("adresse") or {}
                    if isinstance(address, dict):
                        country = str(address.get("land") or "").strip()
            if country:
                job_id = int(row["id"])
                self.connection.execute("UPDATE jobs SET country = ? WHERE id = ?", (country, job_id))
                normalized = country.strip().casefold()
                if normalized not in {"deutschland", "germany", "de"}:
                    self.connection.execute(
                        """
                        UPDATE jobs
                        SET route_distance_km = NULL, route_duration_min = NULL,
                            route_address_text = '', route_locked = 0, route_quality = ''
                        WHERE id = ? AND LOWER(COALESCE(route_address_text, '')) LIKE '%germany%'
                        """,
                        (job_id,),
                    )

    @staticmethod
    def compute_content_hash(candidate: JobCandidate) -> str:
        text = "\n".join(
            [
                candidate.title.strip().lower(),
                candidate.company.strip().lower(),
                candidate.location.strip().lower(),
                candidate.description.strip().lower(),
            ]
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def add_or_update_job(self, candidate: JobCandidate) -> tuple[int, bool, bool]:
        """
        Add or update a job.

        Returns: (job_id, is_new_job, content_changed)
        """
        now = utc_now_iso()
        content_hash = self.compute_content_hash(candidate)
        profile = self.get_company_profile(candidate.company)
        existing = self.connection.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (candidate.source, candidate.source_job_id),
        ).fetchone()

        if existing is None:
            status = "rejected by me" if profile["blacklisted"] else "new"
            company_rating = profile["branch"] or ""
            cursor = self.connection.execute(
                """
                INSERT INTO jobs (
                    source, source_job_id, title, company, location, country, url, description, formatted_description,
                    published_date, first_seen, last_seen, created_at, updated_at, status_changed_at,
                    applied_at, interview_at, rejected_by_me_at, rejected_by_company_at, expired_at, contract_at,
                    application_expected_salary, application_available_from, application_via, application_info, application_notes,
                    status, company_rating, min_salary_k, max_salary_k, fixed_term,
                    route_distance_km, route_duration_min, route_address_text, route_locked, route_quality, selected_location_index, selected_location_manual,
                    content_hash, duplicate_score, duplicate_of_job_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.source,
                    candidate.source_job_id,
                    candidate.title,
                    candidate.company,
                    candidate.location,
                    candidate.country,
                    candidate.url,
                    candidate.description,
                    candidate.formatted_description,
                    candidate.published_date,
                    now,
                    now,
                    now,
                    now,
                    now,
                    "",
                    "",
                    now if self._is_rejected_by_me_status(status) else "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    status,
                    company_rating,
                    candidate.min_salary_k,
                    candidate.max_salary_k,
                    candidate.fixed_term or "-",
                    candidate.route_distance_km,
                    candidate.route_duration_min,
                    candidate.route_address_text,
                    int(candidate.route_locked),
                    candidate.route_quality or "",
                    int(candidate.selected_location_index),
                    int(candidate.selected_location_manual),
                    content_hash,
                    float(getattr(candidate, "duplicate_score", 0.0) or 0.0),
                    getattr(candidate, "duplicate_of_job_id", None),
                ),
            )
            job_id = int(cursor.lastrowid)
            self._insert_version(job_id, candidate, content_hash, now)
            if status != "new":
                self._record_status_change(job_id, "", status, "Created with initial status", now)
            self.connection.commit()
            return job_id, True, True

        job_id = int(existing["id"])
        content_changed = existing["content_hash"] != content_hash
        latest_raw_json = self.get_latest_raw_json(job_id)
        raw_changed = bool(candidate.raw_json) and candidate.raw_json != latest_raw_json
        old_status = str(existing["status"] or "")
        new_status = "rejected by me" if profile["blacklisted"] else old_status
        new_company_rating = existing["company_rating"] or profile["branch"] or ""
        status_changed_at = now if new_status != old_status else existing["status_changed_at"]

        self.connection.execute(
            """
            UPDATE jobs
            SET title = ?, company = ?, location = ?, country = CASE WHEN ? != '' THEN ? ELSE country END, url = ?, description = ?,
                formatted_description = CASE WHEN ? != '' THEN ? ELSE formatted_description END,
                published_date = ?, last_seen = ?, updated_at = ?, status = ?, status_changed_at = ?, company_rating = ?,
                min_salary_k = COALESCE(?, min_salary_k),
                max_salary_k = COALESCE(?, max_salary_k),
                fixed_term = CASE WHEN ? != '' THEN ? ELSE fixed_term END,
                route_distance_km = CASE WHEN route_locked = 0 THEN COALESCE(?, route_distance_km) ELSE route_distance_km END,
                route_duration_min = CASE WHEN route_locked = 0 THEN COALESCE(?, route_duration_min) ELSE route_duration_min END,
                route_address_text = CASE WHEN route_locked = 0 AND ? != '' THEN ? ELSE route_address_text END,
                route_quality = CASE WHEN route_locked = 0 AND ? != '' THEN ? ELSE route_quality END,
                content_hash = ?
            WHERE id = ?
            """,
            (
                candidate.title,
                candidate.company,
                candidate.location,
                candidate.country or "",
                candidate.country or "",
                candidate.url,
                candidate.description,
                candidate.formatted_description or "",
                candidate.formatted_description or "",
                candidate.published_date,
                now,
                now,
                new_status,
                status_changed_at,
                new_company_rating,
                candidate.min_salary_k,
                candidate.max_salary_k,
                candidate.fixed_term or "-",
                candidate.fixed_term or "-",
                candidate.route_distance_km,
                candidate.route_duration_min,
                candidate.route_address_text,
                candidate.route_address_text,
                candidate.route_quality or "",
                candidate.route_quality or "",
                content_hash,
                job_id,
            ),
        )
        if new_status != old_status:
            self._record_status_change(job_id, old_status, new_status, "Company is blacklisted", now)
        if content_changed or raw_changed:
            self._insert_version(job_id, candidate, content_hash, now)
        self.connection.commit()
        return job_id, False, (content_changed or raw_changed)

    def _insert_version(self, job_id: int, candidate: JobCandidate, content_hash: str, seen_at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO job_versions (
                job_id, seen_at, content_hash, title, company, location, description, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                seen_at,
                content_hash,
                candidate.title,
                candidate.company,
                candidate.location,
                candidate.description,
                candidate.raw_json,
            ),
        )

    def list_jobs(
        self,
        status_filter: str = "all",
        text_filter: str = "",
        hide_rejected: bool = True,
        show_only_new: bool = False,
    ) -> list[JobRecord]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[object] = []

        if status_filter != "all":
            query += " AND status = ?"
            params.append(status_filter)

        if hide_rejected:
            query += " AND status NOT IN ('rejected_by_me', 'rejected_by_company', 'ignored', 'expired')"

        if show_only_new:
            query += " AND status = 'new'"

        if text_filter.strip():
            pattern = f"%{text_filter.strip()}%"
            query += " AND (title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ? OR company_rating LIKE ? OR notes LIKE ?)"
            params.extend([pattern, pattern, pattern, pattern, pattern, pattern])

        # Deliberately no ORDER BY here.
        # The GUI preserves the current visible order unless the user sorts manually.
        rows = self.connection.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_latest_raw_json_many(self, job_ids: list[int]) -> dict[int, str]:
        """Return the latest raw JSON for many jobs in one database query."""
        clean_ids = sorted({int(job_id) for job_id in job_ids if job_id is not None})
        if not clean_ids:
            return {}
        placeholders = ",".join("?" for _ in clean_ids)
        rows = self.connection.execute(
            f"""
            SELECT v.job_id, v.raw_json
            FROM job_versions v
            JOIN (
                SELECT job_id, MAX(id) AS max_id
                FROM job_versions
                WHERE job_id IN ({placeholders})
                GROUP BY job_id
            ) latest ON latest.max_id = v.id
            """,
            clean_ids,
        ).fetchall()
        return {int(row["job_id"]): str(row["raw_json"] or "") for row in rows}

    def get_job(self, job_id: int) -> Optional[JobRecord]:
        row = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def find_job_by_source_id(self, source: str, source_job_id: str) -> Optional[JobRecord]:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (source, source_job_id),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_company_profiles(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT company, branch, blacklisted
            FROM company_profiles
            WHERE branch != '' OR blacklisted = 1
            ORDER BY company COLLATE NOCASE
            """
        ).fetchall()
        return [
            {
                "company": str(row["company"]),
                "branch": str(row["branch"] or ""),
                "blacklisted": bool(row["blacklisted"]),
            }
            for row in rows
        ]

    def update_company_profile(self, company: str, branch: str, blacklisted: bool | None = None) -> None:
        if not company.strip():
            return
        company = company.strip()
        branch = branch.strip()
        self.add_company_rating(branch, commit=False)
        current = self.get_company_profile(company)
        new_blacklisted = int(current["blacklisted"] if blacklisted is None else blacklisted)
        self.connection.execute(
            """
            INSERT INTO company_profiles (company, branch, blacklisted)
            VALUES (?, ?, ?)
            ON CONFLICT(company) DO UPDATE SET branch = excluded.branch, blacklisted = excluded.blacklisted
            """,
            (company, branch, new_blacklisted),
        )
        now = utc_now_iso()
        self.connection.execute("UPDATE jobs SET company_rating = ?, updated_at = ? WHERE company = ?", (branch, now, company))
        if new_blacklisted:
            rows = self.connection.execute("SELECT id, status FROM jobs WHERE company = ?", (company,)).fetchall()
            for row in rows:
                old_status = str(row["status"] or "")
                if not self._is_rejected_by_me_status(old_status):
                    existing = self.connection.execute("SELECT notes FROM jobs WHERE id = ?", (int(row["id"]),)).fetchone()
                    notes = self._append_reject_reason(str(existing["notes"] or "") if existing else "", "blacklisted company")
                    self.connection.execute(
                        "UPDATE jobs SET status = ?, notes = ?, updated_at = ?, status_changed_at = ? WHERE id = ?",
                        ("rejected by me", notes, now, now, int(row["id"])),
                    )
                    self._record_status_change(int(row["id"]), old_status, "rejected by me", "blacklisted company", now)
        self.connection.commit()

    def save_user_fields(
        self,
        job_id: int,
        status: str,
        score: Optional[int],
        company_rating: str,
        notes: str,
        reject_reason: str = "",
    ) -> None:
        if status == "ignored":
            status = "rejected by me"
        job = self.get_job(job_id)
        if job is None:
            return
        if company_rating.strip():
            self.add_company_rating(company_rating.strip(), commit=False)
            self.set_company_branch(job.company, company_rating.strip(), commit=False)
        full_notes = self._append_reject_reason(notes, reject_reason) if self._is_rejected_by_me_status(status) else notes
        now = utc_now_iso()
        old_status = job.status
        status_changed_at = now if status != old_status else job.status_changed_at
        self.connection.execute(
            """
            UPDATE jobs
            SET status = ?, manual_score = ?, company_rating = ?, notes = ?,
                updated_at = ?, status_changed_at = ?
            WHERE id = ?
            """,
            (status, score, company_rating, full_notes, now, status_changed_at, job_id),
        )
        self._record_status_change(job_id, old_status, status, reject_reason, now)
        if company_rating.strip():
            self.connection.execute(
                "UPDATE jobs SET company_rating = ?, updated_at = ? WHERE company = ?",
                (company_rating.strip(), utc_now_iso(), job.company),
            )
        self.connection.commit()

    @staticmethod
    def _append_reject_reason(notes: str, reason: str) -> str:
        clean_reason = reason.strip()
        if not clean_reason:
            return notes.strip()
        clean_notes = notes.strip()
        marker = f"Reject reason: {clean_reason}"
        if marker in clean_notes:
            return clean_notes
        if clean_notes:
            return f"{clean_notes}\n\n{marker}"
        return marker

    def update_jobs_status(self, job_ids: list[int], status: str, reject_reason: str = "") -> None:
        if status == "ignored":
            status = "rejected by me"
        now = utc_now_iso()
        for job_id in job_ids:
            job = self.get_job(job_id)
            if job is None:
                continue
            old_status = job.status
            notes = self._append_reject_reason(job.notes, reject_reason) if self._is_rejected_by_me_status(status) else job.notes
            status_changed_at = now if status != old_status else job.status_changed_at
            self.connection.execute(
                """
                UPDATE jobs
                SET status = ?, notes = ?, updated_at = ?, status_changed_at = ?
                WHERE id = ?
                """,
                (status, notes, now, status_changed_at, job_id),
            )
            self._record_status_change(job_id, old_status, status, reject_reason, now)
        self.connection.commit()

    def set_duplicate_info(self, job_id: int, score: float, duplicate_of_job_id: int | None) -> None:
        self.connection.execute(
            "UPDATE jobs SET duplicate_score = ?, duplicate_of_job_id = ?, updated_at = ? WHERE id = ?",
            (float(score), duplicate_of_job_id, utc_now_iso(), int(job_id)),
        )
        self.connection.commit()

    def save_duplicate_relation(self, job_id: int, candidate_job_id: int, score: float, decision: str = "candidate") -> None:
        if int(job_id) == int(candidate_job_id):
            return
        a, b = sorted((int(job_id), int(candidate_job_id)))
        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO job_duplicate_relations(job_id, candidate_job_id, score, decision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, candidate_job_id) DO UPDATE SET
                score = excluded.score, decision = excluded.decision, updated_at = excluded.updated_at
            """,
            (a, b, float(score), str(decision), now, now),
        )
        self.connection.commit()

    def list_duplicate_relations(self, job_id: int) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT r.score, r.decision, j.*
            FROM job_duplicate_relations r
            JOIN jobs j ON j.id = CASE WHEN r.job_id = ? THEN r.candidate_job_id ELSE r.job_id END
            WHERE r.job_id = ? OR r.candidate_job_id = ?
            ORDER BY r.score DESC
            """,
            (int(job_id), int(job_id), int(job_id)),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_duplicate_relation_decision(self, job_id: int, candidate_job_id: int, decision: str) -> None:
        a, b = sorted((int(job_id), int(candidate_job_id)))
        self.connection.execute(
            "UPDATE job_duplicate_relations SET decision = ?, updated_at = ? WHERE job_id = ? AND candidate_job_id = ?",
            (str(decision), utc_now_iso(), a, b),
        )
        self.connection.commit()

    def save_application_details(
        self,
        job_id: int,
        expected_salary: str = "",
        available_from: str = "",
        applied_via: str = "",
        additional_info: str = "",
        application_notes: str = "",
    ) -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE jobs
            SET application_expected_salary = ?, application_available_from = ?,
                application_via = ?, application_info = ?, application_notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(expected_salary or "").strip(),
                str(available_from or "").strip(),
                str(applied_via or "").strip(),
                str(additional_info or "").strip(),
                str(application_notes or "").strip(),
                now,
                int(job_id),
            ),
        )
        self.connection.commit()

    def count_jobs_with_status(self, status: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status = ?",
            (status,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def add_company_rating(self, name: str, commit: bool = True) -> None:
        if not name.strip():
            return
        self.connection.execute(
            "INSERT OR IGNORE INTO company_ratings (name) VALUES (?)",
            (name.strip(),),
        )
        if commit:
            self.connection.commit()

    def list_company_ratings(self) -> list[str]:
        rows = self.connection.execute("SELECT name FROM company_ratings ORDER BY name COLLATE NOCASE").fetchall()
        return [str(row["name"]) for row in rows]

    def rename_company_rating(self, old_name: str, new_name: str) -> None:
        old_name = old_name.strip()
        new_name = new_name.strip()
        if not old_name or not new_name or old_name == new_name:
            return
        self.add_company_rating(new_name, commit=False)
        self.connection.execute("DELETE FROM company_ratings WHERE name = ?", (old_name,))
        self.connection.execute("UPDATE company_profiles SET branch = ? WHERE branch = ?", (new_name, old_name))
        self.connection.execute("UPDATE jobs SET company_rating = ?, updated_at = ? WHERE company_rating = ?", (new_name, utc_now_iso(), old_name))
        self.connection.commit()

    def delete_company_rating(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        self.connection.execute("DELETE FROM company_ratings WHERE name = ?", (name,))
        self.connection.execute("UPDATE company_profiles SET branch = '' WHERE branch = ?", (name,))
        self.connection.execute("UPDATE jobs SET company_rating = '', updated_at = ? WHERE company_rating = ?", (utc_now_iso(), name))
        self.connection.commit()

    def get_company_profile(self, company: str) -> dict[str, object]:
        if not company.strip():
            return {"branch": "", "blacklisted": False}
        row = self.connection.execute(
            "SELECT branch, blacklisted FROM company_profiles WHERE company = ?",
            (company.strip(),),
        ).fetchone()
        if row is None:
            return {"branch": "", "blacklisted": False}
        return {"branch": row["branch"] or "", "blacklisted": bool(row["blacklisted"])}

    def set_company_branch(self, company: str, branch: str, commit: bool = True) -> None:
        if not company.strip():
            return
        self.add_company_rating(branch, commit=False)
        self.connection.execute(
            """
            INSERT INTO company_profiles (company, branch, blacklisted)
            VALUES (?, ?, COALESCE((SELECT blacklisted FROM company_profiles WHERE company = ?), 0))
            ON CONFLICT(company) DO UPDATE SET branch = excluded.branch
            """,
            (company.strip(), branch.strip(), company.strip()),
        )
        self.connection.execute(
            "UPDATE jobs SET company_rating = ?, updated_at = ? WHERE company = ?", (branch.strip(), utc_now_iso(), company.strip()))
        if commit:
            self.connection.commit()

    def list_blacklisted_companies(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT company FROM company_profiles WHERE blacklisted = 1 ORDER BY company COLLATE NOCASE"
        ).fetchall()
        return [str(row["company"]) for row in rows]

    def add_company_to_blacklist(self, company: str, commit: bool = True) -> None:
        if not company.strip():
            return
        company = company.strip()
        self.connection.execute(
            """
            INSERT INTO company_profiles (company, branch, blacklisted)
            VALUES (?, COALESCE((SELECT branch FROM company_profiles WHERE company = ?), ''), 1)
            ON CONFLICT(company) DO UPDATE SET blacklisted = 1
            """,
            (company, company),
        )
        now = utc_now_iso()
        rows = self.connection.execute("SELECT id, status FROM jobs WHERE company = ?", (company,)).fetchall()
        for row in rows:
            old_status = str(row["status"] or "")
            if not self._is_rejected_by_me_status(old_status):
                existing = self.connection.execute("SELECT notes FROM jobs WHERE id = ?", (int(row["id"]),)).fetchone()
                notes = self._append_reject_reason(str(existing["notes"] or "") if existing else "", "blacklisted company")
                self.connection.execute(
                    "UPDATE jobs SET status = ?, notes = ?, updated_at = ?, status_changed_at = ? WHERE id = ?",
                    ("rejected by me", notes, now, now, int(row["id"])),
                )
                self._record_status_change(int(row["id"]), old_status, "rejected by me", "blacklisted company", now)
        if commit:
            self.connection.commit()

    def remove_company_from_blacklist(self, company: str, commit: bool = True) -> None:
        if not company.strip():
            return
        self.connection.execute(
            "UPDATE company_profiles SET blacklisted = 0 WHERE company = ?",
            (company.strip(),),
        )
        if commit:
            self.connection.commit()

    def rename_blacklisted_company(self, old_name: str, new_name: str) -> None:
        old_name = old_name.strip()
        new_name = new_name.strip()
        if not old_name or not new_name:
            return
        profile = self.get_company_profile(old_name)
        self.connection.execute("DELETE FROM company_profiles WHERE company = ?", (old_name,))
        self.connection.execute(
            """
            INSERT INTO company_profiles (company, branch, blacklisted)
            VALUES (?, ?, 1)
            ON CONFLICT(company) DO UPDATE SET blacklisted = 1, branch = excluded.branch
            """,
            (new_name, str(profile["branch"] or "")),
        )
        self.connection.commit()

    @staticmethod
    def normalize_address_text(address_text: str) -> str:
        return " ".join(address_text.strip().casefold().split())

    # ------------------------------------------------------------------
    # Company watchlist
    # ------------------------------------------------------------------

    def list_company_watchlist(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT * FROM company_watchlist
            ORDER BY priority ASC, company_name COLLATE NOCASE ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_company_watchlist_entry(self, entry_id: int) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM company_watchlist WHERE id = ?", (int(entry_id),)
        ).fetchone()
        return dict(row) if row else None

    def save_company_watchlist_entry(
        self,
        *,
        entry_id: int | None = None,
        company_name: str,
        homepage_url: str = "",
        career_url: str,
        career_system: str = "unknown",
        priority: int = 3,
        locations: str = "",
        notes: str = "",
        enabled: bool = True,
    ) -> int:
        company_name = str(company_name or "").strip()
        career_url = str(career_url or "").strip()
        if not company_name or not career_url:
            raise ValueError("Company name and career URL are required.")
        now = utc_now_iso()
        values = (
            company_name, str(homepage_url or "").strip(), career_url,
            str(career_system or "unknown").strip() or "unknown",
            max(1, int(priority)), str(locations or "").strip(),
            str(notes or "").strip(), int(bool(enabled)), now, now,
        )
        if entry_id is None:
            cur = self.connection.execute(
                """
                INSERT INTO company_watchlist (
                    company_name, homepage_url, career_url, career_system, priority,
                    locations, notes, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_name, career_url) DO UPDATE SET
                    homepage_url = excluded.homepage_url,
                    career_system = excluded.career_system,
                    priority = excluded.priority,
                    locations = excluded.locations,
                    notes = excluded.notes,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            row = self.connection.execute(
                "SELECT id FROM company_watchlist WHERE company_name = ? AND career_url = ?",
                (company_name, career_url),
            ).fetchone()
            saved_id = int(row["id"])
        else:
            self.connection.execute(
                """
                UPDATE company_watchlist SET
                    company_name = ?, homepage_url = ?, career_url = ?, career_system = ?,
                    priority = ?, locations = ?, notes = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                values[:8] + (now, int(entry_id)),
            )
            saved_id = int(entry_id)
        self.connection.commit()
        return saved_id

    def delete_company_watchlist_entries(self, entry_ids: list[int]) -> int:
        clean_ids = [int(value) for value in entry_ids]
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        cur = self.connection.execute(
            f"DELETE FROM company_watchlist WHERE id IN ({placeholders})", clean_ids
        )
        self.connection.commit()
        return int(cur.rowcount or 0)

    def set_company_watchlist_enabled(self, entry_ids: list[int], enabled: bool) -> int:
        clean_ids = [int(value) for value in entry_ids]
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        now = utc_now_iso()
        cur = self.connection.execute(
            f"UPDATE company_watchlist SET enabled = ?, updated_at = ? "
            f"WHERE id IN ({placeholders})",
            (int(bool(enabled)), now, *clean_ids),
        )
        self.connection.commit()
        return int(cur.rowcount or 0)

    def update_company_watchlist_detection(
        self, entry_id: int, career_system: str, status: str, error: str = ""
    ) -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE company_watchlist
            SET career_system = ?, last_status = ?, last_error = ?,
                last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (career_system, status, error, now, now, int(entry_id)),
        )
        self.connection.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    def get_geo_settings(self) -> dict[str, object]:
        # Backward compatible keys: older GUI versions used geo.provider and geo.request_delay.
        routing_provider = self.get_setting("geo.routing_provider", self.get_setting("geo.provider", "direct"))
        osrm_delay = self.get_setting("geo.osrm_delay", self.get_setting("geo.request_delay", "1.1"))
        ors_delay = self.get_setting("geo.ors_delay", "1.6")
        return {
            "home_address": self.get_setting("geo.home_address", ""),
            "geocoding_provider": self.get_setting("geo.geocoding_provider", "nominatim"),
            "routing_provider": routing_provider or "direct",
            "provider": routing_provider or "direct",
            "osrm_delay": osrm_delay or "1.1",
            "ors_delay": ors_delay or "1.6",
            "request_delay": osrm_delay or "1.1",
            "ors_env_var": self.get_setting("geo.ors_env_var", "ORS_API_KEY"),
        }

    def save_geo_settings(
        self,
        home_address: str,
        geocoding_provider: str,
        routing_provider: str,
        osrm_delay: str,
        ors_delay: str,
        ors_env_var: str = "ORS_API_KEY",
    ) -> None:
        self.set_setting("geo.home_address", home_address.strip())
        self.set_setting("geo.geocoding_provider", (geocoding_provider or "nominatim").strip() or "nominatim")
        self.set_setting("geo.routing_provider", (routing_provider or "direct").strip() or "direct")
        # Keep old keys for backward compatibility with older helper code.
        self.set_setting("geo.provider", (routing_provider or "direct").strip() or "direct")
        self.set_setting("geo.osrm_delay", str(osrm_delay).strip() or "1.1")
        self.set_setting("geo.ors_delay", str(ors_delay).strip() or "1.6")
        self.set_setting("geo.request_delay", str(osrm_delay).strip() or "1.1")
        self.set_setting("geo.ors_env_var", (ors_env_var or "ORS_API_KEY").strip() or "ORS_API_KEY")

    def _get_address_row_by_text(self, address_text: str) -> Optional[sqlite3.Row]:
        normalized = self.normalize_address_text(address_text)
        if not normalized:
            return None
        return self.connection.execute(
            "SELECT * FROM addresses WHERE normalized_address = ?",
            (normalized,),
        ).fetchone()

    def get_cached_address_by_text(self, address_text: str) -> Optional[dict[str, object]]:
        row = self._get_address_row_by_text(address_text)
        if row is None:
            return None
        return {
            "address_text": str(row["address_text"] or address_text),
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "display_name": str(row["display_name"] or ""),
            "provider": str(row["provider"] or ""),
            "geocoded_at": str(row["geocoded_at"] or ""),
        }

    def save_cached_address_by_text(self, address_text: str, lat: float, lon: float, display_name: str = "", provider: str = "") -> None:
        self._upsert_address(address_text, lat, lon, display_name, provider, utc_now_iso())
        self.connection.commit()

    def get_location_alias(self, display_location: str) -> str:
        key = str(display_location or "").strip()
        if not key:
            return ""
        row = self.connection.execute(
            "SELECT routing_location FROM location_aliases WHERE display_location = ?",
            (key,),
        ).fetchone()
        return str(row["routing_location"] or "") if row else ""

    def set_location_alias(self, display_location: str, routing_location: str) -> None:
        key = str(display_location or "").strip()
        value = str(routing_location or "").strip()
        if not key:
            return
        if not value:
            self.connection.execute("DELETE FROM location_aliases WHERE display_location = ?", (key,))
        else:
            self.connection.execute(
                """
                INSERT INTO location_aliases (display_location, routing_location, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(display_location)
                DO UPDATE SET routing_location = excluded.routing_location,
                              updated_at = excluded.updated_at
                """,
                (key, value, utc_now_iso()),
            )
        self.connection.commit()

    def list_location_aliases(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT display_location, routing_location, updated_at FROM location_aliases ORDER BY display_location COLLATE NOCASE"
        ).fetchall()
        return [dict(row) for row in rows]


    def get_cached_route_by_text(self, from_address_text: str, to_address_text: str, provider: str = "direct") -> Optional[dict[str, object]]:
        from_row = self._get_address_row_by_text(from_address_text)
        to_row = self._get_address_row_by_text(to_address_text)
        if from_row is None or to_row is None:
            return None

        requested = (provider or "direct").strip().lower()

        # Exact routes from OSRM/ORS are considered interchangeable enough for
        # JobRadar. Estimated/direct routes are only reused when no exact route
        # exists or direct is explicitly selected.
        if requested not in {"direct", "direct_estimate", "estimate"}:
            row = self.connection.execute(
                """
                SELECT distance_km, duration_min, calculated_at, provider, quality
                FROM routes
                WHERE from_address_id = ? AND to_address_id = ? AND quality = 'exact'
                ORDER BY calculated_at DESC
                LIMIT 1
                """,
                (int(from_row["id"]), int(to_row["id"])),
            ).fetchone()
            if row is not None:
                return {
                    "distance_km": float(row["distance_km"]),
                    "duration_min": float(row["duration_min"]),
                    "calculated_at": str(row["calculated_at"] or ""),
                    "provider": str(row["provider"] or ""),
                    "quality": str(row["quality"] or "exact"),
                }

        # Prefer an exact route if it already exists, even in direct mode, because
        # it costs no additional API request and is better than an estimate.
        row = self.connection.execute(
            """
            SELECT distance_km, duration_min, calculated_at, provider, quality
            FROM routes
            WHERE from_address_id = ? AND to_address_id = ? AND quality = 'exact'
            ORDER BY calculated_at DESC
            LIMIT 1
            """,
            (int(from_row["id"]), int(to_row["id"])),
        ).fetchone()
        if row is not None:
            return {
                "distance_km": float(row["distance_km"]),
                "duration_min": float(row["duration_min"]),
                "calculated_at": str(row["calculated_at"] or ""),
                "provider": str(row["provider"] or ""),
                "quality": str(row["quality"] or "exact"),
            }

        row = self.connection.execute(
            """
            SELECT distance_km, duration_min, calculated_at, provider, quality
            FROM routes
            WHERE from_address_id = ? AND to_address_id = ? AND provider = ?
            ORDER BY calculated_at DESC
            LIMIT 1
            """,
            (int(from_row["id"]), int(to_row["id"]), provider),
        ).fetchone()
        if row is None:
            return None
        return {
            "distance_km": float(row["distance_km"]),
            "duration_min": float(row["duration_min"]),
            "calculated_at": str(row["calculated_at"] or ""),
            "provider": str(row["provider"] or ""),
            "quality": str(row["quality"] or "estimated"),
        }

    def delete_cached_route_by_text(
        self,
        from_address_text: str,
        to_address_text: str,
        provider: str = "",
        clear_destination_geocode: bool = True,
    ) -> int:
        """Delete a cached route and optionally the destination geocode entry.

        Clearing the destination geocode is useful when the cached coordinates were
        created from an unsuitable ATS location string and would otherwise be reused.
        """
        from_row = self._get_address_row_by_text(from_address_text)
        to_row = self._get_address_row_by_text(to_address_text)
        if from_row is None or to_row is None:
            return 0
        from_id = int(from_row["id"])
        to_id = int(to_row["id"])
        if provider:
            cursor = self.connection.execute(
                "DELETE FROM routes WHERE from_address_id = ? AND to_address_id = ? AND provider = ?",
                (from_id, to_id, provider),
            )
        else:
            cursor = self.connection.execute(
                "DELETE FROM routes WHERE from_address_id = ? AND to_address_id = ?",
                (from_id, to_id),
            )
        deleted = max(0, int(cursor.rowcount or 0))
        if clear_destination_geocode:
            self.connection.execute(
                "DELETE FROM routes WHERE from_address_id = ? OR to_address_id = ?",
                (to_id, to_id),
            )
            self.connection.execute("DELETE FROM addresses WHERE id = ?", (to_id,))
        self.connection.commit()
        return deleted

    def save_cached_route_by_text(
        self,
        from_address_text: str,
        from_lat: float,
        from_lon: float,
        from_display_name: str,
        to_address_text: str,
        to_lat: float,
        to_lon: float,
        to_display_name: str,
        provider: str,
        quality: str,
        distance_km: float,
        duration_min: float,
    ) -> None:
        now = utc_now_iso()
        from_id = self._upsert_address(from_address_text, from_lat, from_lon, from_display_name, provider, now)
        to_id = self._upsert_address(to_address_text, to_lat, to_lon, to_display_name, provider, now)
        self.connection.execute(
            """
            INSERT INTO routes (from_address_id, to_address_id, provider, quality, distance_km, duration_min, calculated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_address_id, to_address_id, provider)
            DO UPDATE SET quality = excluded.quality,
                          distance_km = excluded.distance_km,
                          duration_min = excluded.duration_min,
                          calculated_at = excluded.calculated_at
            """,
            (from_id, to_id, provider, quality, distance_km, duration_min, now),
        )
        self.connection.commit()

    def _upsert_address(self, address_text: str, lat: float, lon: float, display_name: str, provider: str, now: str) -> int:
        normalized = self.normalize_address_text(address_text)
        self.connection.execute(
            """
            INSERT INTO addresses (address_text, normalized_address, lat, lon, display_name, provider, geocoded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_address)
            DO UPDATE SET address_text = excluded.address_text,
                          lat = excluded.lat,
                          lon = excluded.lon,
                          display_name = excluded.display_name,
                          provider = excluded.provider,
                          geocoded_at = excluded.geocoded_at
            """,
            (address_text.strip(), normalized, lat, lon, display_name, provider, now),
        )
        row = self.connection.execute(
            "SELECT id FROM addresses WHERE normalized_address = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to store address")
        return int(row["id"])

    def save_job_selected_location(
        self,
        job_id: int,
        selected_location_index: int,
        location_text: str,
        manual: bool = True,
        country: str = "",
    ) -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE jobs
            SET selected_location_index = ?, selected_location_manual = ?, location = ?,
                country = CASE WHEN ? != '' THEN ? ELSE country END,
                route_distance_km = NULL, route_duration_min = NULL,
                route_address_text = '', route_locked = 0, route_quality = '', updated_at = ?
            WHERE id = ?
            """,
            (int(selected_location_index), int(manual), location_text.strip(), country.strip(), country.strip(), now, job_id),
        )
        self.connection.commit()

    def save_job_description(self, job_id: int, description: str) -> None:
        now = utc_now_iso()
        self.connection.execute(
            "UPDATE jobs SET description = ?, formatted_description = '', updated_at = ? WHERE id = ?",
            (str(description or "").strip(), now, int(job_id)),
        )
        self.connection.commit()

    def save_job_formatted_description(self, job_id: int, formatted_description: str) -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE jobs
            SET formatted_description = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(formatted_description or "").strip(), now, int(job_id)),
        )
        self.connection.commit()

    def save_job_country(self, job_id: int, country: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET country = ?, updated_at = ? WHERE id = ?",
            (str(country or "").strip(), utc_now_iso(), int(job_id)),
        )
        self.connection.commit()

    def clear_job_route(self, job_id: int) -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE jobs
            SET route_address_text = '', route_distance_km = NULL, route_duration_min = NULL,
                route_locked = 0, route_quality = '', updated_at = ?
            WHERE id = ?
            """,
            (now, int(job_id)),
        )
        self.connection.commit()

    def save_job_route(self, job_id: int, destination_text: str, distance_km: float, duration_min: float, locked: bool, quality: str = "") -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE jobs
            SET route_address_text = ?, route_distance_km = ?, route_duration_min = ?,
                route_locked = ?, route_quality = ?, updated_at = ?
            WHERE id = ?
            """,
            (destination_text.strip(), distance_km, duration_min, int(locked), quality, now, job_id),
        )
        self.connection.commit()

    def save_ai_evaluation(
        self,
        job_id: int,
        ai_name: str,
        provider: str,
        model: str,
        summary: str = "",
        rating: str = "",
        score: Optional[int] = None,
        decision: str = "",
        raw_response: str = "",
        reject_reason: str = "",
        rejection_tags: Optional[list[str]] = None,
    ) -> int:
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO ai_evaluations (
                job_id, ai_name, provider, model, created_at, summary, rating, score, decision, raw_response,
                reject_reason, rejection_tags_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, ai_name, provider, model, now, summary, rating, score, decision, raw_response,
                str(reject_reason or ""), json.dumps(rejection_tags or [], ensure_ascii=False),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def get_latest_ai_evaluation(self, job_id: int) -> Optional[dict]:
        row = self.connection.execute(
            """
            SELECT id, job_id, ai_name, provider, model, created_at, summary, rating, score, decision, raw_response,
                   reject_reason, rejection_tags_json
            FROM ai_evaluations
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["rejection_tags"] = json.loads(result.get("rejection_tags_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["rejection_tags"] = []
        return result

    def list_ai_evaluations(self, job_id: int) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, job_id, ai_name, provider, model, created_at, summary, rating, score, decision, raw_response,
                   reject_reason, rejection_tags_json
            FROM ai_evaluations
            WHERE job_id = ?
            ORDER BY id DESC
            """,
            (job_id,),
        ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            try:
                result["rejection_tags"] = json.loads(result.get("rejection_tags_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                result["rejection_tags"] = []
            results.append(result)
        return results

    def save_ai_chat_message(self, job_id: int, role: str, content: str, model: str = "") -> int:
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO ai_chat_messages (job_id, created_at, role, content, model)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(job_id), now, str(role or ""), str(content or ""), str(model or "")),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_ai_chat_messages(self, job_id: int, limit: int = 200) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, job_id, created_at, role, content, model
            FROM ai_chat_messages
            WHERE job_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(job_id), int(limit or 200)),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_raw_json(self, job_id: int) -> str:
        row = self.connection.execute(
            """
            SELECT raw_json
            FROM job_versions
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        return row["raw_json"] if row else ""

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            source=row["source"],
            source_job_id=row["source_job_id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            country=row["country"] if "country" in row.keys() else "",
            url=row["url"],
            description=row["description"],
            formatted_description=row["formatted_description"] if "formatted_description" in row.keys() else "",
            published_date=row["published_date"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            status=row["status"],
            manual_score=row["manual_score"],
            company_rating=row["company_rating"],
            notes=row["notes"],
            content_hash=row["content_hash"],
            min_salary_k=row["min_salary_k"],
            max_salary_k=row["max_salary_k"],
            fixed_term=row["fixed_term"] or "-",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status_changed_at=row["status_changed_at"],
            applied_at=row["applied_at"],
            interview_at=row["interview_at"],
            rejected_by_me_at=row["rejected_by_me_at"],
            rejected_by_company_at=row["rejected_by_company_at"],
            expired_at=row["expired_at"],
            contract_at=row["contract_at"] if "contract_at" in row.keys() else "",
            application_expected_salary=row["application_expected_salary"] if "application_expected_salary" in row.keys() else "",
            application_available_from=row["application_available_from"] if "application_available_from" in row.keys() else "",
            application_via=row["application_via"] if "application_via" in row.keys() else "",
            application_info=row["application_info"] if "application_info" in row.keys() else "",
            application_notes=row["application_notes"] if "application_notes" in row.keys() else "",
            route_distance_km=row["route_distance_km"],
            route_duration_min=row["route_duration_min"],
            route_address_text=row["route_address_text"],
            route_locked=bool(row["route_locked"]),
            route_quality=row["route_quality"] or "",
            selected_location_index=int(row["selected_location_index"] or 0),
            selected_location_manual=bool(row["selected_location_manual"]),
            duplicate_score=float(row["duplicate_score"] or 0.0),
            duplicate_of_job_id=int(row["duplicate_of_job_id"]) if row["duplicate_of_job_id"] is not None else None,
        )


    def add_ai_memory(
        self,
        text: str,
        category: str = "preference",
        source: str = "auto",
        confidence: float = 1.0,
        importance: float = 1.0,
        embedding_json: str = "",
        active: bool = True,
    ) -> int:
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO ai_memories (
                text, category, source, confidence, importance, active, created_at, updated_at, embedding_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                text.strip(),
                category.strip() or "preference",
                source.strip() or "auto",
                float(confidence),
                float(importance),
                1 if active else 0,
                now,
                now,
                embedding_json,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_ai_memories(self, active_only: bool = True) -> list:
        from .ai.models import AiMemory

        if active_only:
            rows = self.connection.execute(
                """
                SELECT id, text, category, source, confidence, importance, active, created_at, updated_at, embedding_json
                FROM ai_memories
                WHERE active = 1
                ORDER BY importance DESC, updated_at DESC, id DESC
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT id, text, category, source, confidence, importance, active, created_at, updated_at, embedding_json
                FROM ai_memories
                ORDER BY active DESC, importance DESC, updated_at DESC, id DESC
                """
            ).fetchall()
        return [
            AiMemory(
                id=int(row["id"]),
                text=str(row["text"] or ""),
                category=str(row["category"] or "preference"),
                source=str(row["source"] or "auto"),
                confidence=float(row["confidence"] if row["confidence"] is not None else 1.0),
                importance=float(row["importance"] if row["importance"] is not None else 1.0),
                active=bool(row["active"]),
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                embedding_json=str(row["embedding_json"] or ""),
            )
            for row in rows
        ]

    def update_ai_memory_embedding(self, memory_id: int | None, embedding_json: str) -> None:
        if memory_id is None:
            return
        self.connection.execute(
            "UPDATE ai_memories SET embedding_json = ?, updated_at = ? WHERE id = ?",
            (embedding_json, utc_now_iso(), int(memory_id)),
        )
        self.connection.commit()

    def set_ai_memory_active(self, memory_id: int, active: bool) -> None:
        self.connection.execute(
            "UPDATE ai_memories SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, utc_now_iso(), int(memory_id)),
        )
        self.connection.commit()
