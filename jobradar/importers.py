from __future__ import annotations

import base64
import json
import re
from typing import Protocol

import requests

from .models import JobCandidate


class JobImporter(Protocol):
    name: str

    def search(self, query: str, location: str, radius_km: int, max_results: int) -> list[JobCandidate]:
        ...


class BAJobImporter:
    """Importer for Bundesagentur / Arbeitsagentur job search."""

    name = "ba"
    base_url = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
    headers = {"X-API-Key": "jobboerse-jobsuche"}

    def search(self, query: str, location: str, radius_km: int = 100, max_results: int = 25) -> list[JobCandidate]:
        return self.search_page(query=query, location=location, radius_km=radius_km, page=1, size=max_results)

    def search_page(self, query: str, location: str, radius_km: int = 100, page: int = 1, size: int = 25) -> list[JobCandidate]:
        """Fetch one BA result page.

        This avoids the old pattern of requesting size=25, then size=50, then
        size=100 and repeatedly downloading the same first results. The BA
        endpoint accepts a 1-based page parameter.
        """
        safe_size = max(1, min(int(size or 25), 100))
        safe_page = max(1, int(page or 1))
        params = {
            "angebotsart": 1,
            "was": query,
            "wo": location,
            "umkreis": radius_km,
            "page": safe_page,
            "size": safe_size,
            "pav": "false",
        }
        response = requests.get(
            f"{self.base_url}/pc/v6/jobs",
            headers=self.headers,
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        offers = data.get("ergebnisliste") or data.get("stellenangebote") or []

        result: list[JobCandidate] = []
        for offer in offers[:safe_size]:
            refnr = offer.get("refnr") or offer.get("referenznummer") or ""
            if not refnr:
                continue
            detail = self.fetch_details(refnr)
            result.append(self._details_to_candidate(refnr, detail))
        return result

    def fetch_details(self, refnr: str) -> dict:
        encoded_refnr = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
        response = requests.get(
            f"{self.base_url}/pc/v4/jobdetails/{encoded_refnr}",
            headers=self.headers,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def fetch_candidate_by_refnr(self, refnr: str) -> JobCandidate:
        """Fetch one BA job directly by reference number via the detail endpoint."""
        detail = self.fetch_details(refnr)
        return self._details_to_candidate(refnr, detail)

    @staticmethod
    def _salary_euro_to_k(value: object) -> float | None:
        if value is None:
            return None
        try:
            return round(float(value) / 1000.0, 1)
        except (TypeError, ValueError):
            return None


    @staticmethod
    def _fixed_term_display(detail: dict) -> str:
        duration = str(detail.get("vertragsdauer") or "").upper()
        if duration == "BEFRISTET":
            until = str(detail.get("befristetBis") or "").strip()
            if until:
                return until
            months = detail.get("befristungInMonaten")
            if months is not None:
                try:
                    return f"{int(float(months))} mo"
                except (TypeError, ValueError):
                    pass
            return "FT"
        return "-"

    def _details_to_candidate(self, refnr: str, detail: dict) -> JobCandidate:
        locations = detail.get("stellenlokationen") or []
        location_text = ""
        country = ""
        if locations:
            address = locations[0].get("adresse", {})
            country = str(address.get("land") or "").strip()
            location_text = ", ".join(
                part for part in [address.get("plz", ""), address.get("ort", ""), address.get("region", "")] if part
            )

        published = ""
        if isinstance(detail.get("veroeffentlichungszeitraum"), dict):
            published = detail["veroeffentlichungszeitraum"].get("von", "")
        published = published or detail.get("datumErsteVeroeffentlichung", "")

        return JobCandidate(
            source=self.name,
            source_job_id=refnr,
            title=detail.get("stellenangebotsTitel", ""),
            company=detail.get("firma", ""),
            location=location_text,
            country=country,
            url=detail.get("allianzpartnerUrl", ""),
            description=detail.get("stellenangebotsBeschreibung", ""),
            published_date=published,
            raw_json=json.dumps(detail, ensure_ascii=False),
            min_salary_k=self._salary_euro_to_k(detail.get("gehaltsspanneVon")),
            max_salary_k=self._salary_euro_to_k(detail.get("gehaltsspanneBis")),
            fixed_term=self._fixed_term_display(detail),
        )


class PastedListImporter:
    """
    Minimal parser for pasted job lists.

    Expected examples:
    - Software Engineer | Siemens | Erlangen
    - Software Engineer - Siemens - Erlangen
    - Software Engineer\tSiemens\tErlangen
    """

    name = "pasted"

    def parse(self, text: str) -> list[JobCandidate]:
        result: list[JobCandidate] = []
        for index, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue

            parts = self._split_line(line)
            title = parts[0] if len(parts) >= 1 else line
            company = parts[1] if len(parts) >= 2 else "Unknown company"
            location = parts[2] if len(parts) >= 3 else ""
            stable_id = f"{title.lower()}|{company.lower()}|{location.lower()}"

            result.append(
                JobCandidate(
                    source=self.name,
                    source_job_id=stable_id,
                    title=title,
                    company=company,
                    location=location,
                    description="",
                    raw_json=json.dumps({"line": line}, ensure_ascii=False),
                )
            )
        return result

    @staticmethod
    def _split_line(line: str) -> list[str]:
        if "\t" in line:
            return [p.strip() for p in line.split("\t") if p.strip()]
        if "|" in line:
            return [p.strip() for p in line.split("|") if p.strip()]
        parts = re.split(r"\s+-\s+", line)
        return [p.strip() for p in parts if p.strip()]
