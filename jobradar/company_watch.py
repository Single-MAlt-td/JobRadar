from __future__ import annotations

import base64
import html
import json
import re
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

from .models import JobCandidate


USER_AGENT = "JobRadar/0.6.9.26 (+company-watch)"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
}




COUNTRY_ALIASES = {
    "de": "germany",
    "deu": "germany",
    "deutschland": "germany",
    "germany": "germany",
    "at": "austria",
    "aut": "austria",
    "österreich": "austria",
    "oesterreich": "austria",
    "austria": "austria",
    "ch": "switzerland",
    "che": "switzerland",
    "schweiz": "switzerland",
    "switzerland": "switzerland",
    "fr": "france",
    "fra": "france",
    "frankreich": "france",
    "france": "france",
    "nl": "netherlands",
    "nld": "netherlands",
    "niederlande": "netherlands",
    "netherlands": "netherlands",
    "uk": "united kingdom",
    "gb": "united kingdom",
    "gbr": "united kingdom",
    "vereinigtes königreich": "united kingdom",
    "united kingdom": "united kingdom",
    "usa": "united states",
    "us": "united states",
    "united states": "united states",
    "vereinigte staaten": "united states",
}


def normalize_country_name(value: object) -> str:
    text = " ".join(str(value or "").split()).strip().casefold()
    return COUNTRY_ALIASES.get(text, text)


def candidate_matches_country(candidate: JobCandidate, requested_country: str) -> bool:
    """Return whether a candidate belongs to the requested country.

    German/English names and common ISO-style abbreviations are accepted. Jobs
    without any country information are excluded when a filter is active, since
    otherwise global ATS boards can flood the result list.
    """
    wanted = normalize_country_name(requested_country)
    if not wanted:
        return True
    values = [candidate.country, candidate.location]
    for value in values:
        raw = " ".join(str(value or "").split()).strip()
        if not raw:
            continue
        normalized = normalize_country_name(raw)
        if normalized == wanted:
            return True
        parts = re.split(r"[,;/|()]", raw)
        if any(normalize_country_name(part) == wanted for part in parts if part.strip()):
            return True
        if wanted in raw.casefold():
            return True
    return False


class CompanyWatchError(RuntimeError):
    pass


@dataclass
class AdapterResult:
    system: str
    candidates: list[JobCandidate]
    coverage: str = "complete listing"
    note: str = ""


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        self._href = str(values.get("href") or "").strip()
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def _get(url: str, *, timeout: int = 30, params: dict | None = None, headers: dict | None = None) -> requests.Response:
    merged = dict(REQUEST_HEADERS)
    if headers:
        merged.update(headers)
    response = requests.get(url, timeout=timeout, params=params, headers=merged, allow_redirects=True)
    response.raise_for_status()
    return response


def _strip_html(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|h[1-6]|tr|section)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _json_ld_objects(markup: str) -> Iterable[dict]:
    for match in re.finditer(
        r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        markup,
    ):
        raw = html.unescape(match.group(1)).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        queue = data if isinstance(data, list) else [data]
        for item in queue:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                queue.extend(item["@graph"])
            if isinstance(item, dict):
                yield item


def _location_entries_from_json_ld(value: object) -> list[dict]:
    """Convert schema.org JobPosting locations to JobRadar's multi-location shape."""
    locations = value if isinstance(value, list) else [value]
    result: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address") or {}
        if isinstance(address, str):
            clean = " ".join(address.split()).strip()
            if clean:
                key = ("", clean, "", "")
                if key not in seen:
                    seen.add(key)
                    result.append({"adresse": {"ort": clean}})
            continue
        if not isinstance(address, dict):
            continue
        current_country = address.get("addressCountry") or ""
        if isinstance(current_country, dict):
            current_country = current_country.get("name") or current_country.get("value") or ""
        postal = str(address.get("postalCode") or "").strip()
        city = str(address.get("addressLocality") or "").strip()
        region = str(address.get("addressRegion") or "").strip()
        country = str(current_country or "").strip()
        key = (postal.casefold(), city.casefold(), region.casefold(), country.casefold())
        if not any(key) or key in seen:
            continue
        seen.add(key)
        entry: dict = {
            "adresse": {
                "plz": postal,
                "ort": city,
                "region": region,
                "land": country,
            }
        }
        geo = location.get("geo") or {}
        if isinstance(geo, dict):
            lat = geo.get("latitude")
            lon = geo.get("longitude")
            if lat not in (None, "") and lon not in (None, ""):
                entry["breite"] = lat
                entry["laenge"] = lon
        result.append(entry)
    return result


def _format_location_entry(location: dict) -> tuple[str, str]:
    address = location.get("adresse") or {}
    if not isinstance(address, dict):
        return "", ""
    country = str(address.get("land") or "").strip()
    text = ", ".join(
        part for part in (
            str(address.get("plz") or "").strip(),
            str(address.get("ort") or "").strip(),
            str(address.get("region") or "").strip(),
        ) if part
    )
    return text, country


def _location_from_json_ld(value: object) -> tuple[str, str]:
    entries = _location_entries_from_json_ld(value)
    formatted = [_format_location_entry(item) for item in entries]
    texts = [text for text, _country in formatted if text]
    country = next((country for _text, country in formatted if country), "")
    return "; ".join(texts), country




def _normalize_location_country(location: object, country: object = "") -> tuple[str, str]:
    """Normalize common ATS location formats for display and geocoding.

    Examples:
      "Germany, Munich (HQ)" -> ("Munich", "Germany")
      "Munich, Germany" -> ("Munich", "Germany")
    """
    text = " ".join(str(location or "").split()).strip()
    country_text = " ".join(str(country or "").split()).strip()
    if not text:
        return "", country_text

    aliases = {
        "de": "Germany", "deutschland": "Germany", "germany": "Germany",
        "at": "Austria", "österreich": "Austria", "austria": "Austria",
        "ch": "Switzerland", "schweiz": "Switzerland", "switzerland": "Switzerland",
        "united states": "United States", "usa": "United States", "us": "United States",
        "united kingdom": "United Kingdom", "uk": "United Kingdom",
    }

    def clean_part(value: str) -> str:
        value = re.sub(
            r"\s*\((?:hq|headquarters|office|hybrid|remote|on[- ]?site|vor ort|m/f/d|f/m/d)\)\s*$",
            "",
            value,
            flags=re.I,
        )
        return value.strip()

    parts = [clean_part(part) for part in text.split(",") if clean_part(part)]
    if not parts:
        return text, country_text

    first_country = aliases.get(parts[0].casefold())
    last_country = aliases.get(parts[-1].casefold())
    if first_country and len(parts) >= 2:
        country_text = country_text or first_country
        parts = parts[1:]
    elif last_country and len(parts) >= 2:
        country_text = country_text or last_country
        parts = parts[:-1]

    if country_text:
        country_text = aliases.get(country_text.casefold(), country_text)
    return ", ".join(parts).strip(), country_text


def _description_from_job_page(url: str) -> str:
    """Fetch a job detail page and extract its description as a fallback."""
    response = _get(url, timeout=40)
    for item in _json_ld_objects(response.text):
        kind = item.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(value).casefold() == "jobposting" for value in kinds):
            description = _strip_html(item.get("description"))
            if description:
                return description
    main = re.search(r"(?is)<main\b[^>]*>(.*?)</main>", response.text)
    if main:
        description = _strip_html(main.group(1))
        if description:
            return description
    return ""

def _relevant_script_urls(markup: str, base_url: str, keywords: tuple[str, ...]) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", markup, re.I):
        absolute = urljoin(base_url, html.unescape(match.group(1)))
        folded = absolute.casefold()
        if any(keyword.casefold() in folded for keyword in keywords) and absolute not in urls:
            urls.append(absolute)
    return urls


def _safe_id(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip()).strip("-")


class GreenhouseAdapter:
    system = "greenhouse"

    @staticmethod
    def _board_token(career_url: str) -> str:
        parsed = urlparse(career_url)
        host = parsed.netloc.casefold()
        parts = [part for part in parsed.path.split("/") if part]
        if "greenhouse.io" in host and parts:
            if parts[0] in {"embed", "job_board"}:
                query = parse_qs(parsed.query)
                return str((query.get("for") or [""])[0])
            return parts[0]
        response = _get(career_url)
        sample = f"{response.url}\n{response.text}"
        patterns = (
            r"boards(?:-api)?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)",
            r"boards\.greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)",
            r"boards\.greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)",
            r"greenhouse\.io[^\"']*[?&]for=([A-Za-z0-9_-]+)",
            r"data-board-token=[\"']([^\"']+)",
        )
        for pattern in patterns:
            match = re.search(pattern, sample, re.I)
            if match:
                return match.group(1)
        raise CompanyWatchError("Could not determine the Greenhouse board token.")

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        token = self._board_token(str(entry.get("career_url") or ""))
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        data = _get(endpoint, params={"content": "true"}).json()
        jobs = data.get("jobs") or []
        candidates: list[JobCandidate] = []
        for job in jobs:
            job_id = str(job.get("id") or job.get("internal_job_id") or "").strip()
            if not job_id:
                continue
            location = str((job.get("location") or {}).get("name") or "")
            candidates.append(JobCandidate(
                source="direct",
                source_job_id=f"{int(entry['id'])}:greenhouse:{job_id}",
                title=str(job.get("title") or ""),
                company=str(entry.get("company_name") or ""),
                location=location,
                url=str(job.get("absolute_url") or ""),
                description=_strip_html(job.get("content")),
                published_date=str(job.get("updated_at") or "")[:10],
                raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": self.system, "board_token": token, "job": job}, ensure_ascii=False),
            ))
        return AdapterResult(self.system, candidates)


class SmartRecruitersAdapter:
    system = "smartrecruiters"

    @staticmethod
    def _company_identifier(career_url: str, company_name: str = "") -> str:
        parsed = urlparse(career_url)
        parts = [part for part in parsed.path.split("/") if part]
        if "smartrecruiters.com" in parsed.netloc.casefold() and parts:
            return parts[0]
        response = _get(career_url)
        sample = html.unescape(f"{response.url}\n{response.text}").replace("\\/", "/")
        patterns = (
            r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
            r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)/postings",
            r"[\"'](?:companyIdentifier|company_identifier|companyId|company_id)[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]+)[\"']",
        )
        for pattern in patterns:
            match = re.search(pattern, sample, re.I)
            if match:
                return match.group(1)
        for script_url in _relevant_script_urls(response.text, response.url, ("job", "career", "nuxt"))[:8]:
            try:
                decoded = html.unescape(_get(script_url, timeout=30).text).replace("\\/", "/")
            except Exception:
                continue
            for pattern in patterns:
                match = re.search(pattern, decoded, re.I)
                if match:
                    return match.group(1)
        # Some custom career pages only reveal that SmartRecruiters is used,
        # but hide the identifier in runtime requests. Try conservative company-name
        # variants and only accept one after the public Posting API validates it.
        raw_name = " ".join(str(company_name or "").split()).strip()
        variants = []
        if raw_name:
            variants.extend((
                raw_name,
                re.sub(r"[^A-Za-z0-9]+", "", raw_name),
                re.sub(r"[^A-Za-z0-9]+", "-", raw_name).strip("-"),
            ))
        for candidate in dict.fromkeys(value for value in variants if value):
            try:
                probe = _get(
                    f"https://api.smartrecruiters.com/v1/companies/{candidate}/postings",
                    params={"limit": 1, "offset": 0},
                    timeout=20,
                ).json()
                if isinstance(probe, dict) and "content" in probe:
                    return candidate
            except Exception:
                continue
        raise CompanyWatchError("Could not determine the SmartRecruiters company identifier.")

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        company_id = self._company_identifier(
            str(entry.get("career_url") or ""),
            str(entry.get("company_name") or ""),
        )
        base = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
        offset = 0
        limit = 100
        summaries: list[dict] = []
        while True:
            data = _get(base, params={"offset": offset, "limit": limit}).json()
            batch = data.get("content") or []
            summaries.extend(item for item in batch if isinstance(item, dict))
            offset += len(batch)
            total = int(data.get("totalFound") or len(summaries))
            if not batch or offset >= total:
                break
        requested_country = str(entry.get("_country_filter") or "").strip()
        if requested_country:
            filtered_summaries: list[dict] = []
            for summary in summaries:
                location_data = summary.get("location") or {}
                if isinstance(location_data, dict):
                    location = ", ".join(
                        str(location_data.get(key) or "").strip()
                        for key in ("city", "region", "country")
                        if str(location_data.get(key) or "").strip()
                    )
                    country = str(location_data.get("country") or "")
                else:
                    location, country = str(location_data or ""), ""
                probe = JobCandidate(
                    source="direct", source_job_id="", title="", company="",
                    location=location, country=country,
                )
                if candidate_matches_country(probe, requested_country):
                    filtered_summaries.append(summary)
            summaries = filtered_summaries

        candidates: list[JobCandidate] = []
        for summary in summaries:
            posting_id = str(summary.get("id") or summary.get("uuid") or "").strip()
            if not posting_id:
                continue
            detail = _get(f"{base}/{posting_id}").json()
            location_data = detail.get("location") or summary.get("location") or {}
            if isinstance(location_data, dict):
                location = ", ".join(str(location_data.get(key) or "").strip() for key in ("city", "region", "country") if str(location_data.get(key) or "").strip())
                country = str(location_data.get("country") or "")
            else:
                location, country = str(location_data or ""), ""
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            description_parts: list[str] = []
            if isinstance(sections, dict):
                for value in sections.values():
                    if isinstance(value, dict):
                        content = value.get("text") or value.get("title") or ""
                    else:
                        content = value
                    cleaned = _strip_html(content)
                    if cleaned:
                        description_parts.append(cleaned)
            description = "\n\n".join(description_parts)
            url = str(detail.get("applyUrl") or detail.get("postingUrl") or summary.get("ref") or "")
            if not url:
                url = f"https://jobs.smartrecruiters.com/{company_id}/{posting_id}"
            candidates.append(JobCandidate(
                source="direct",
                source_job_id=f"{int(entry['id'])}:smartrecruiters:{posting_id}",
                title=str(detail.get("name") or summary.get("name") or ""),
                company=str(entry.get("company_name") or ""),
                location=location,
                country=country,
                url=url,
                description=description,
                published_date=str(detail.get("releasedDate") or summary.get("releasedDate") or "")[:10],
                raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": self.system, "company_identifier": company_id, "job": detail}, ensure_ascii=False),
            ))
        return AdapterResult(self.system, candidates)


class PersonioAdapter:
    system = "personio"

    @staticmethod
    def _personio_host(career_url: str) -> str:
        parsed = urlparse(career_url)
        if ".jobs.personio." in parsed.netloc.casefold():
            return parsed.netloc
        response = _get(career_url)
        sample = html.unescape(f"{response.url}\n{response.text}")
        # Embedded widgets may keep their URL in iframe attributes or JSON/script
        # payloads with escaped slashes and Unicode escape sequences.
        sample = sample.replace("\\/", "/")
        sample = sample.replace("\\u002f", "/").replace("\\u002F", "/")
        sample = sample.replace("\\u003a", ":").replace("\\u003A", ":")
        sample = unquote(sample)
        match = re.search(r"(?:https?:)?//([A-Za-z0-9.-]+\.jobs\.personio\.(?:de|com))", sample, re.I)
        if match:
            return match.group(1)
        for script_url in _relevant_script_urls(response.text, response.url, ("job", "career", "personio"))[:8]:
            try:
                decoded = unquote(html.unescape(_get(script_url, timeout=30).text)).replace("\\/", "/")
            except Exception:
                continue
            decoded = decoded.replace("\\u002f", "/").replace("\\u003a", ":")
            match = re.search(r"(?:https?:)?//([A-Za-z0-9.-]+\.jobs\.personio\.(?:de|com))", decoded, re.I)
            if match:
                return match.group(1)
        raise CompanyWatchError("Could not find the linked Personio career page.")

    @staticmethod
    def _child_text(node: ET.Element, name: str) -> str:
        child = node.find(name)
        return "" if child is None else "".join(child.itertext()).strip()

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        host = self._personio_host(str(entry.get("career_url") or ""))
        response = _get(f"https://{host}/xml", headers={"Accept": "application/xml,text/xml,*/*"})
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise CompanyWatchError(f"Personio XML feed could not be parsed: {exc}") from exc
        positions = root.findall(".//position")
        candidates: list[JobCandidate] = []
        for position in positions:
            job_id = self._child_text(position, "id")
            title = self._child_text(position, "name")
            if not job_id or not title:
                continue
            office = self._child_text(position, "office")
            location, country = _normalize_location_country(office)
            descriptions: list[str] = []
            for section in position.findall(".//jobDescription"):
                heading = self._child_text(section, "name")
                value = _strip_html(self._child_text(section, "value"))
                if value:
                    descriptions.append((f"{heading}\n{value}" if heading else value).strip())
            raw = ET.tostring(position, encoding="unicode")
            candidates.append(JobCandidate(
                source="direct",
                source_job_id=f"{int(entry['id'])}:personio:{job_id}",
                title=title,
                company=str(entry.get("company_name") or ""),
                location=location,
                country=country,
                url=f"https://{host}/job/{job_id}",
                description="\n\n".join(descriptions),
                published_date=self._child_text(position, "createdAt")[:10],
                fixed_term=_normalize_fixed_term(self._child_text(position, "employmentType")),
                raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": self.system, "personio_host": host, "position_xml": raw}, ensure_ascii=False),
            ))
        missing = [candidate for candidate in candidates if not candidate.description and candidate.url]
        if missing:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(_description_from_job_page, candidate.url): candidate for candidate in missing}
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        candidate.description = future.result()
                    except Exception:
                        # Keep the job, but do not let one malformed detail page fail the whole company.
                        pass
        return AdapterResult(self.system, candidates)


class SuccessFactorsAdapter:
    system = "successfactors"

    @staticmethod
    def _search_url(career_url: str) -> str:
        response = _get(career_url)
        final_url = response.url
        parsed = urlparse(final_url)
        if "/search" in parsed.path.casefold():
            return final_url
        parser = _LinkParser()
        parser.feed(response.text)
        for href, _text in parser.links:
            absolute = urljoin(final_url, href)
            if "/search" in urlparse(absolute).path.casefold():
                return absolute
        # Career Site Builder normally exposes a search path on the same host.
        base_path = parsed.path.rstrip("/")
        if base_path and base_path not in {"/de", "/en"}:
            candidate = urljoin(final_url, base_path + "/search/")
        else:
            candidate = urljoin(final_url, "search/")
        return candidate

    @staticmethod
    def _job_links(markup: str, base_url: str) -> list[tuple[str, str]]:
        decoded = html.unescape(markup).replace("\\/", "/")
        parser = _LinkParser()
        parser.feed(decoded)
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href, text in parser.links:
            absolute = urljoin(base_url, href)
            path = urlparse(absolute).path
            if "/job/" not in path.casefold():
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            result.append((absolute, text))
        for match in re.finditer(r"(?:https?:)?//[^\"'<>\s]+/job/[^\"'<>\s]+|/job/[^\"'<>\s]+", decoded, re.I):
            raw = match.group(0).rstrip("\\,)]}")
            absolute = urljoin(base_url, raw if not raw.startswith("//") else "https:" + raw)
            if absolute not in seen:
                seen.add(absolute)
                result.append((absolute, ""))
        return result

    @staticmethod
    def _with_startrow(url: str, startrow: int) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["startrow"] = [str(startrow)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @staticmethod
    def _candidate_from_detail(entry: dict[str, object], url: str, title_hint: str, markup: str) -> JobCandidate:
        posting: dict = {}
        for item in _json_ld_objects(markup):
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(value).casefold() == "jobposting" for value in kinds):
                posting = item
                break
        path = urlparse(url).path
        id_matches = re.findall(r"(?:/|[-_])(\d{4,})(?:/|$|[?])", path + "/")
        job_id = id_matches[-1] if id_matches else _safe_id(path)
        title = str(posting.get("title") or title_hint or "").strip()
        description = _strip_html(posting.get("description"))
        location_entries = _location_entries_from_json_ld(posting.get("jobLocation"))
        formatted_locations = [_format_location_entry(item) for item in location_entries]
        selected_index = 0
        requested_country = normalize_country_name(entry.get("_country_filter"))
        if requested_country:
            for index, (_text, current_country) in enumerate(formatted_locations):
                if normalize_country_name(current_country) == requested_country:
                    selected_index = index
                    break
        if formatted_locations:
            location, country = formatted_locations[selected_index]
        else:
            location, country = "", ""
        if not title:
            match = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", markup)
            title = _strip_html(match.group(1)) if match else ""
        if not description:
            main = re.search(r"(?is)<(?:main|div)[^>]+(?:job|content)[^>]*>(.*?)</(?:main|div)>", markup)
            description = _strip_html(main.group(1) if main else markup)
        return JobCandidate(
            source="direct",
            source_job_id=f"{int(entry['id'])}:successfactors:{job_id}",
            title=title,
            company=str(entry.get("company_name") or ""),
            location=location,
            country=country,
            url=url,
            description=description,
            published_date=str(posting.get("datePosted") or "")[:10],
            fixed_term=_normalize_fixed_term(posting.get("employmentType")),
            selected_location_index=selected_index,
            raw_json=json.dumps({
                "company_watchlist_id": entry.get("id"),
                "career_system": "successfactors",
                "job_url": url,
                "json_ld": posting,
                "stellenlokationen": location_entries,
                "country": country,
                "selected_location_index": selected_index,
            }, ensure_ascii=False),
        )

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        search_url = self._search_url(str(entry.get("career_url") or ""))
        links: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        page_size = 25
        for page in range(40):
            page_url = self._with_startrow(search_url, page * page_size)
            response = _get(page_url, timeout=40)
            current = self._job_links(response.text, response.url)
            new_count = 0
            for url, title in current:
                if url not in seen_urls:
                    seen_urls.add(url)
                    links.append((url, title))
                    new_count += 1
            if new_count == 0:
                break
            if len(current) < 10 and page > 0:
                break
        if not links:
            parsed = urlparse(search_url)
            for sitemap_path in ("/sitemap.xml", "/sitemap_index.xml", "/job-sitemap.xml"):
                try:
                    sitemap = _get(urlunparse(parsed._replace(path=sitemap_path, query="", fragment="")), timeout=40)
                except Exception:
                    continue
                for url, title in self._job_links(sitemap.text, sitemap.url):
                    if url not in seen_urls:
                        seen_urls.add(url)
                        links.append((url, title))
                if links:
                    break
        if not links:
            raise CompanyWatchError("No SuccessFactors job links were found on the search page or sitemap.")
        candidates: list[JobCandidate] = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_get, url, timeout=40): (url, title) for url, title in links[:1000]}
            for future in as_completed(futures):
                url, title = futures[future]
                try:
                    response = future.result()
                    candidate = self._candidate_from_detail(entry, response.url, title, response.text)
                    if candidate.title:
                        candidates.append(candidate)
                except Exception:
                    continue
        return AdapterResult(self.system, candidates, coverage="complete listing through public search pagination")


def _normalize_fixed_term(value: object) -> str:
    """Return '-' for permanent/unknown employment types and a label only for fixed-term work."""
    text = " ".join(str(value or "").split()).strip()
    folded = text.casefold().replace("_", "-")
    if not folded or folded in {"-", "unknown", "n/a", "none", "null"}:
        return "-"
    permanent_markers = ("permanent", "unbefrist", "indefinite", "unlimited", "regular")
    if any(marker in folded for marker in permanent_markers):
        return "-"
    fixed_markers = ("fixed-term", "fixed term", "befrist", "temporary", "temp", "contract", "limited")
    if any(marker in folded for marker in fixed_markers) or re.search(r"\b\d+\s*(?:month|months|monat|monate|year|years|jahr|jahre)\b", folded):
        return text
    return "-"


class WorkdayAdapter:
    system = "workday"

    @staticmethod
    def _site_info(career_url: str) -> tuple[str, str, str, str]:
        response = _get(career_url, timeout=40)
        final_url = response.url
        parsed = urlparse(final_url)
        host = parsed.netloc
        parts = [part for part in parsed.path.split("/") if part]
        tenant = host.split(".", 1)[0]
        site = ""
        locale = ""
        # Typical URL: /en-US/<site>/jobs or /<site>/job/...
        if parts and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", parts[0]):
            locale = parts.pop(0)
        if parts:
            site = parts[0]
        sample = f"{final_url}\n{response.text}"
        cxs = re.search(r"/wday/cxs/([^/]+)/([^/]+)/jobs", sample, re.I)
        if cxs:
            tenant, site = cxs.group(1), cxs.group(2)
        if not tenant or not site:
            raise CompanyWatchError("Could not determine the Workday tenant and career-site name.")
        return host, tenant, site, locale

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        career_url = str(entry.get("career_url") or "")
        host, tenant, site, locale = self._site_info(career_url)
        endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        offset = 0
        limit = 20
        summaries: list[dict] = []
        while True:
            payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
            response = requests.post(endpoint, headers={**REQUEST_HEADERS, "Content-Type": "application/json"}, json=payload, timeout=40)
            response.raise_for_status()
            data = response.json()
            batch = data.get("jobPostings") or []
            summaries.extend(item for item in batch if isinstance(item, dict))
            offset += len(batch)
            total = int(data.get("total") or len(summaries))
            if not batch or offset >= total:
                break
            if offset > 10000:
                raise CompanyWatchError("Workday returned more than 10,000 jobs; pagination was stopped.")

        candidates: list[JobCandidate] = []
        for summary in summaries:
            external_path = str(summary.get("externalPath") or "").strip()
            if not external_path:
                continue
            detail_url = f"https://{host}/wday/cxs/{tenant}/{site}/job/{external_path.lstrip('/')}"
            try:
                detail = _get(detail_url, timeout=40).json()
                info = detail.get("jobPostingInfo") or detail
            except Exception:
                info = summary
            job_id = str(info.get("jobReqId") or summary.get("bulletFields", [""])[0] or external_path).strip()
            locations = info.get("additionalLocations") or []
            location_parts = [str(info.get("location") or "").strip()]
            if isinstance(locations, list):
                location_parts.extend(str(v).strip() for v in locations if str(v or "").strip())
            location = "; ".join(dict.fromkeys(v for v in location_parts if v))
            public_path = str(info.get("externalUrl") or external_path)
            if public_path.startswith("http"):
                public_url = public_path
            else:
                prefix = f"/{locale}" if locale else ""
                public_url = urljoin(f"https://{host}", f"{prefix}/{site}{public_path if public_path.startswith('/') else '/' + public_path}")
            candidates.append(JobCandidate(
                source="direct",
                source_job_id=f"{int(entry['id'])}:workday:{_safe_id(job_id)}",
                title=str(info.get("title") or summary.get("title") or ""),
                company=str(entry.get("company_name") or ""),
                location=location,
                url=public_url,
                description=_strip_html(info.get("jobDescription") or info.get("description") or ""),
                published_date=str(info.get("startDate") or summary.get("postedOn") or "")[:10],
                fixed_term=_normalize_fixed_term(info.get("timeType") or info.get("workerType") or info.get("employmentType")),
                raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": self.system, "tenant": tenant, "site": site, "job": info}, ensure_ascii=False),
            ))
        return AdapterResult(self.system, candidates)


class RecruiteeAdapter:
    system = "recruitee"

    @staticmethod
    def _base(career_url: str) -> tuple[str, str]:
        response = _get(career_url, timeout=40)
        parsed = urlparse(response.url)
        host = parsed.netloc
        sample = f"{response.url}\n{response.text}"
        match = re.search(r"https?://([A-Za-z0-9.-]+\.recruitee\.com)", sample, re.I)
        if match:
            host = match.group(1)
        if ".recruitee.com" not in host.casefold():
            raise CompanyWatchError("Could not find the linked Recruitee career page.")
        return f"https://{host}", host.split(".", 1)[0]

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        base, account = self._base(str(entry.get("career_url") or ""))
        data = _get(f"{base}/api/offers/", timeout=40).json()
        offers = data.get("offers") if isinstance(data, dict) else data
        if not isinstance(offers, list):
            raise CompanyWatchError("The Recruitee public offers endpoint returned an unexpected response.")
        candidates: list[JobCandidate] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            job_id = str(offer.get("id") or offer.get("slug") or "").strip()
            if not job_id:
                continue
            locations = offer.get("locations") or []
            location_parts: list[str] = []
            if isinstance(locations, list):
                for loc in locations:
                    if isinstance(loc, dict):
                        value = loc.get("city") or loc.get("name") or loc.get("location") or ""
                    else:
                        value = loc
                    if str(value or "").strip():
                        location_parts.append(str(value).strip())
            location = "; ".join(dict.fromkeys(location_parts)) or str(offer.get("location") or "")
            url = str(offer.get("careers_url") or offer.get("url") or offer.get("apply_url") or "")
            if not url:
                slug = str(offer.get("slug") or job_id)
                url = f"{base}/o/{slug}"
            description = "\n\n".join(filter(None, [
                _strip_html(offer.get("description")),
                _strip_html(offer.get("requirements")),
            ]))
            candidates.append(JobCandidate(
                source="direct",
                source_job_id=f"{int(entry['id'])}:recruitee:{_safe_id(job_id)}",
                title=str(offer.get("title") or ""),
                company=str(entry.get("company_name") or ""),
                location=location,
                url=url,
                description=description,
                published_date=str(offer.get("published_at") or offer.get("created_at") or "")[:10],
                fixed_term=_normalize_fixed_term(offer.get("employment_type") or offer.get("contract_type")),
                raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": self.system, "account": account, "offer": offer}, ensure_ascii=False),
            ))
        return AdapterResult(self.system, candidates)



class AshbyAdapter:
    system = "ashby"

    @staticmethod
    def _slug_variants(company_name: str) -> list[str]:
        raw = " ".join(str(company_name or "").split()).strip()
        folded = raw.casefold()
        folded = re.sub(r"\b(gmbh|ag|se|inc|ltd|llc|group)\b", " ", folded)
        folded = " ".join(folded.split())
        return list(dict.fromkeys(filter(None, (
            re.sub(r"[^a-z0-9]+", "-", folded).strip("-"),
            re.sub(r"[^a-z0-9]+", "", folded),
            re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-"),
        ))))

    @classmethod
    def _board_name(cls, entry: dict[str, object]) -> tuple[str, dict]:
        career_url = str(entry.get("career_url") or "")
        parsed = urlparse(career_url)
        parts = [part for part in parsed.path.split("/") if part]
        if "ashbyhq.com" in parsed.netloc.casefold() and parts:
            board = parts[0]
            return board, _get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", timeout=30).json()
        response = _get(career_url, timeout=40)
        sample = html.unescape(response.text).replace("\\/", "/")
        patterns = (
            r"jobs\.ashbyhq\.com/([^/\\\"'?&#]+)",
            r"api\.ashbyhq\.com/posting-api/job-board/([^/\\\"'?&#]+)",
            r"[\\\"']jobBoardName[\\\"']\s*[:=]\s*[\\\"']([^\\\"']+)",
        )
        candidates: list[str] = []
        for pattern in patterns:
            candidates.extend(match.group(1) for match in re.finditer(pattern, sample, re.I))
        candidates.extend(cls._slug_variants(str(entry.get("company_name") or "")))
        for board in dict.fromkeys(unquote(value).strip() for value in candidates if value.strip()):
            try:
                data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", timeout=30).json()
                if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                    return board, data
            except Exception:
                continue
        raise CompanyWatchError("Could not determine the Ashby job board name.")

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        board, data = self._board_name(entry)
        candidates: list[JobCandidate] = []
        for job in data.get("jobs") or []:
            if not isinstance(job, dict) or not bool(job.get("isListed", True)):
                continue
            job_id = str(job.get("id") or job.get("jobUrl") or job.get("applyUrl") or "").strip()
            if not job_id:
                continue
            locations: list[dict] = []
            primary = str(job.get("location") or "").strip()
            if primary:
                loc, country = _normalize_location_country(primary)
                locations.append({"adresse": {"ort": loc, "land": country}})
            for secondary in job.get("secondaryLocations") or []:
                if not isinstance(secondary, dict):
                    continue
                address = secondary.get("address") or {}
                if isinstance(address, dict):
                    locations.append({"adresse": {
                        "plz": str(address.get("postalCode") or ""),
                        "ort": str(address.get("addressLocality") or secondary.get("location") or ""),
                        "region": str(address.get("addressRegion") or ""),
                        "land": str(address.get("addressCountry") or ""),
                    }})
            formatted = [_format_location_entry(item) for item in locations]
            location = next((text for text, _ in formatted if text), primary)
            country = next((country for _, country in formatted if country), "")
            raw = {"company_watchlist_id": entry.get("id"), "career_system": self.system, "board_name": board, "job": job}
            if locations:
                raw["arbeitsorte"] = locations
            candidates.append(JobCandidate(
                source="direct",
                source_job_id=f"{int(entry['id'])}:ashby:{_safe_id(job_id)}",
                title=str(job.get("title") or ""),
                company=str(entry.get("company_name") or ""),
                location=location,
                country=country,
                url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
                description=_strip_html(job.get("descriptionHtml") or job.get("descriptionPlain") or job.get("description") or ""),
                published_date=str(job.get("publishedAt") or job.get("updatedAt") or "")[:10],
                fixed_term=_normalize_fixed_term(job.get("employmentType")),
                raw_json=json.dumps(raw, ensure_ascii=False),
            ))
        return AdapterResult(self.system, candidates, coverage="complete listing from Ashby public Job Postings API")


class BiteAdapter:
    system = "b-ite"

    @staticmethod
    def _listing_key(markup: str) -> str:
        decoded = html.unescape(markup).replace("\\/", "/")
        patterns = (
            r"data-bite-jobs-api-listing=[\"']([^\"']+)",
            r"[\"'](?:listingKey|listing-key)[\"']\s*[:=]\s*[\"']([^\"']+)",
        )
        for pattern in patterns:
            match = re.search(pattern, decoded, re.I)
            if match:
                return match.group(1).strip()
        raise CompanyWatchError("Could not determine the BITE Job Listing Key.")

    @staticmethod
    def _extract_jobs(data: object) -> list[dict]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("jobPostings", "postings", "jobs", "items", "results", "content"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                nested = BiteAdapter._extract_jobs(value)
                if nested:
                    return nested
        return []

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        response = _get(str(entry.get("career_url") or ""), timeout=40)
        key = self._listing_key(response.text)
        endpoint = "https://jobs.b-ite.com/api/v1/postings/search"
        data = None
        attempts = (
            ({"listingKey": key}, {}),
            ({"jobListingKey": key}, {}),
            ({"key": key}, {}),
            ({}, {"X-Job-Listing-Key": key}),
        )
        errors: list[str] = []
        for payload, headers in attempts:
            try:
                result = requests.post(endpoint, json=payload, headers={**REQUEST_HEADERS, **headers}, timeout=40)
                result.raise_for_status()
                candidate_data = result.json()
                if self._extract_jobs(candidate_data) or isinstance(candidate_data, dict):
                    data = candidate_data
                    break
            except Exception as exc:
                errors.append(str(exc))
        if data is None:
            raise CompanyWatchError("BITE search request failed: " + " | ".join(errors[-2:]))
        candidates: list[JobCandidate] = []
        for job in self._extract_jobs(data):
            job_id = str(job.get("hash") or job.get("id") or job.get("uuid") or "").strip()
            title = str(job.get("title") or job.get("jobTitle") or job.get("name") or "").strip()
            if not job_id or not title:
                continue
            address = job.get("address") or job.get("location") or {}
            if isinstance(address, dict):
                location = ", ".join(str(address.get(k) or "").strip() for k in ("postalCode", "city", "region") if str(address.get(k) or "").strip())
                country = str(address.get("country") or address.get("countryName") or "")
            else:
                location, country = str(address or ""), ""
            detail_url = str(job.get("url") or job.get("detailUrl") or "")
            if not detail_url and job.get("hash"):
                detail_url = f"https://jobs.b-ite.com/jobposting/{job['hash']}"
            candidates.append(JobCandidate(
                source="direct", source_job_id=f"{int(entry['id'])}:b-ite:{_safe_id(job_id)}",
                title=title, company=str(entry.get("company_name") or ""), location=location,
                country=country, url=detail_url,
                description=_strip_html(job.get("description") or job.get("content") or job.get("text") or ""),
                published_date=str(job.get("publishedAt") or job.get("publicationDate") or job.get("date") or "")[:10],
                raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": self.system, "listing_key": key, "job": job}, ensure_ascii=False),
            ))
        return AdapterResult(self.system, candidates, coverage="complete listing from BITE Job Search API")


class JoinAdapter:
    system = "join"

    @staticmethod
    def _jobposting_from_markup(markup: str) -> list[dict]:
        result: list[dict] = []
        for item in _json_ld_objects(markup):
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(value).casefold() == "jobposting" for value in kinds):
                result.append(item)
        return result

    @staticmethod
    def _job_links(markup: str, base_url: str) -> list[str]:
        parser = _LinkParser()
        parser.feed(markup)
        result: list[str] = []
        for href, _ in parser.links:
            url = urljoin(base_url, href)
            parsed = urlparse(url)
            if "join.com" not in parsed.netloc.casefold() and "join.jobs" not in parsed.netloc.casefold():
                continue
            path = parsed.path.casefold()
            if "/jobs/" in path or re.search(r"/companies/[^/]+/[^/]*job", path):
                if url not in result:
                    result.append(url)
        return result

    @staticmethod
    def _candidate(entry: dict[str, object], posting: dict, fallback_url: str) -> JobCandidate | None:
        title = str(posting.get("title") or "").strip()
        url = str(posting.get("url") or posting.get("sameAs") or fallback_url).strip()
        identifier = posting.get("identifier") or ""
        if isinstance(identifier, dict):
            identifier = identifier.get("value") or identifier.get("name") or ""
        job_id = str(identifier or urlparse(url).path).strip()
        if not title or not job_id:
            return None
        location, country = _location_from_json_ld(posting.get("jobLocation"))
        return JobCandidate(
            source="direct",
            source_job_id=f"{int(entry['id'])}:join:{_safe_id(job_id)}",
            title=title,
            company=str(entry.get("company_name") or ""),
            location=location,
            country=country,
            url=url,
            description=_strip_html(posting.get("description")),
            published_date=str(posting.get("datePosted") or "")[:10],
            fixed_term=_normalize_fixed_term(posting.get("employmentType")),
            raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": "join", "json_ld": posting}, ensure_ascii=False),
        )

    @staticmethod
    def _widget_urls(markup: str) -> list[str]:
        decoded = html.unescape(markup).replace("\\/", "/")
        urls: list[str] = []
        for match in re.finditer(r"https?://join\.com/api/widget/bundle/[A-Za-z0-9._~-]+", decoded, re.I):
            url = match.group(0)
            if url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _decode_widget_payload(widget_url: str) -> dict:
        token = urlparse(widget_url).path.rsplit("/", 1)[-1]
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _public_jobs(company_public_id: str) -> list[dict]:
        data = _get(f"https://join.com/api/public/companies/{company_public_id}/jobs", timeout=40).json()
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("jobs", "items", "data", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    for nested in ("jobs", "items", "results"):
                        items = value.get(nested)
                        if isinstance(items, list):
                            return [item for item in items if isinstance(item, dict)]
        return []

    @staticmethod
    def _candidate_from_public_job(entry: dict[str, object], job: dict) -> JobCandidate | None:
        job_id = str(job.get("id") or job.get("publicId") or job.get("slug") or "").strip()
        title = str(job.get("title") or job.get("name") or "").strip()
        if not job_id or not title:
            return None
        location_data = job.get("location") or job.get("office") or {}
        if isinstance(location_data, dict):
            location = ", ".join(str(location_data.get(k) or "").strip() for k in ("city", "state", "country") if str(location_data.get(k) or "").strip())
            country = str(location_data.get("country") or "")
        else:
            location, country = str(location_data or ""), ""
        url = str(job.get("url") or job.get("jobUrl") or job.get("applicationUrl") or "") or f"https://join.com/companies/job/{job_id}"
        return JobCandidate(
            source="direct", source_job_id=f"{int(entry['id'])}:join:{_safe_id(job_id)}",
            title=title, company=str(entry.get("company_name") or ""), location=location,
            country=country, url=url,
            description=_strip_html(job.get("description") or job.get("content") or ""),
            published_date=str(job.get("publishedAt") or job.get("createdAt") or job.get("datePosted") or "")[:10],
            fixed_term=_normalize_fixed_term(job.get("employmentType") or job.get("contractType")),
            raw_json=json.dumps({"company_watchlist_id": entry.get("id"), "career_system": "join", "job": job}, ensure_ascii=False),
        )

    @staticmethod
    def _next_data_jobs(markup: str) -> list[dict]:
        match = re.search(r'(?is)<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', markup)
        if not match:
            return []
        try:
            root = json.loads(html.unescape(match.group(1)))
        except Exception:
            return []
        found: list[dict] = []
        seen: set[str] = set()
        def walk(value: object) -> None:
            if isinstance(value, dict):
                title = value.get("title") or value.get("name")
                ident = value.get("id") or value.get("publicId") or value.get("slug")
                if title and ident and any(key in value for key in ("location", "employmentType", "description", "jobUrl", "applicationUrl")):
                    key = str(ident)
                    if key not in seen:
                        seen.add(key); found.append(value)
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(root)
        return found

    def fetch(self, entry: dict[str, object]) -> AdapterResult:
        response = _get(str(entry.get("career_url") or ""), timeout=40)
        next_jobs = self._next_data_jobs(response.text)
        next_candidates = [self._candidate_from_public_job(entry, job) for job in next_jobs]
        next_candidates = [candidate for candidate in next_candidates if candidate is not None]
        if next_candidates:
            return AdapterResult(self.system, next_candidates, coverage="complete listing from JOIN Next.js page data")
        response_links: list[str] = []
        for widget_url in self._widget_urls(response.text):
            payload = self._decode_widget_payload(widget_url)
            company_public_id = str(payload.get("companyPublicId") or "").strip()
            if company_public_id:
                try:
                    public_jobs = self._public_jobs(company_public_id)
                except Exception:
                    public_jobs = []
                candidates = [self._candidate_from_public_job(entry, job) for job in public_jobs]
                candidates = [candidate for candidate in candidates if candidate is not None]
                if candidates:
                    return AdapterResult(self.system, candidates, coverage="complete listing from JOIN public widget API")
            try:
                bundle = _get(widget_url, timeout=40)
                response_links.extend(self._job_links(bundle.text, bundle.url))
            except Exception:
                pass

        pages: list[tuple[str, str]] = [(response.url, response.text)]
        links = list(dict.fromkeys(response_links or self._job_links(response.text, response.url)))
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_get, url, timeout=40): url for url in links[:1000]}
            for future in as_completed(futures):
                try:
                    detail = future.result()
                    pages.append((detail.url, detail.text))
                except Exception:
                    continue
        candidates: list[JobCandidate] = []
        seen: set[str] = set()
        for page_url, markup in pages:
            for posting in self._jobposting_from_markup(markup):
                candidate = self._candidate(entry, posting, page_url)
                if candidate and candidate.source_job_id not in seen:
                    seen.add(candidate.source_job_id)
                    candidates.append(candidate)
        if not candidates and links:
            raise CompanyWatchError("JOIN job links were found, but their detail pages contained no JobPosting data.")
        if not candidates:
            raise CompanyWatchError("No JOIN jobs were found via the widget API or public company page.")
        return AdapterResult(self.system, candidates, coverage="complete listing from public company page")


def build_career_source_diagnostic(
    entry: dict[str, object],
    stored_system: str,
    registry: "CompanyWatchAdapterRegistry",
) -> dict[str, object]:
    """Collect a shareable diagnostic report for one company career source.

    The report deliberately avoids cookies and full page dumps. It contains the
    redirect chain, safe response metadata, discovered integration URLs, a short
    markup excerpt and the result (or traceback) of the configured adapter.
    """
    career_url = str(entry.get("career_url") or "").strip()
    report: dict[str, object] = {
        "diagnostic_version": 2,
        "jobradar_version": "0.6.9.26",
        "company": str(entry.get("company_name") or ""),
        "watchlist_id": entry.get("id"),
        "configured_career_url": career_url,
        "stored_career_system": str(stored_system or "unknown"),
        "request_user_agent": USER_AGENT,
    }
    if not career_url:
        report["request_error"] = "No career URL is configured."
        return report

    try:
        response = requests.get(
            career_url,
            timeout=30,
            allow_redirects=True,
            headers=REQUEST_HEADERS,
        )
        report["http"] = {
            "status_code": response.status_code,
            "reason": response.reason,
            "final_url": response.url,
            "redirect_chain": [
                {
                    "status_code": item.status_code,
                    "url": item.url,
                    "location": item.headers.get("Location", ""),
                }
                for item in response.history
            ],
            "content_type": response.headers.get("Content-Type", ""),
            "content_length_header": response.headers.get("Content-Length", ""),
            "downloaded_bytes": len(response.content),
            "server": response.headers.get("Server", ""),
        }
        response.raise_for_status()
        markup = response.text[:1_000_000]
        normalized = html.unescape(markup)
        normalized = normalized.replace("\\/", "/")
        normalized = normalized.replace("\\u002f", "/").replace("\\u002F", "/")
        normalized = normalized.replace("\\u003a", ":").replace("\\u003A", ":")
        normalized = unquote(normalized)

        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", normalized)
        report["page_title"] = _strip_html(title_match.group(1)) if title_match else ""

        url_pattern = re.compile(r"(?i)https?://[^\\s<>\"']+")
        discovered: list[str] = []
        for raw in url_pattern.findall(normalized):
            url = raw.rstrip("),.;]}")
            if url not in discovered:
                discovered.append(url)
            if len(discovered) >= 250:
                break
        integration_markers = (
            "personio", "greenhouse", "smartrecruiters", "workday",
            "successfactors", "recruitee", "join.com", "join.jobs", "lever", "ashbyhq", "b-ite",
            "/api/", "/wday/", "/search/",
        )
        report["discovered_integration_urls"] = [
            url for url in discovered
            if any(marker in url.casefold() for marker in integration_markers)
        ][:150]

        iframe_urls = re.findall(r"(?is)<iframe[^>]+src=[\"']([^\"']+)[\"']", normalized)
        script_urls = re.findall(r"(?is)<script[^>]+src=[\"']([^\"']+)[\"']", normalized)
        report["iframe_urls"] = [urljoin(response.url, value) for value in iframe_urls[:100]]
        report["script_urls"] = [urljoin(response.url, value) for value in script_urls[:100]]
        report["markup_excerpt"] = _strip_html(normalized[:12000])[:6000]
        report["marker_counts"] = {
            marker: normalized.casefold().count(marker)
            for marker in (
                "personio", "greenhouse", "smartrecruiters", "workday",
                "successfactors", "recruitee", "join", "ashby", "b-ite", "jobposting",
            )
        }
    except Exception as exc:
        report["request_error"] = str(exc)
        report["request_traceback"] = traceback.format_exc()

    system = str(stored_system or "unknown").casefold()
    report["adapter_test"] = {"system": system}
    if system not in registry.supported_systems:
        report["adapter_test"].update({
            "status": "skipped",
            "reason": f"No implemented adapter for '{system}'.",
        })
        return report
    try:
        result = registry.fetch(entry, system)
        report["adapter_test"].update({
            "status": "ok",
            "coverage": result.coverage,
            "note": result.note,
            "candidate_count": len(result.candidates),
            "candidate_samples": [
                {
                    "source_job_id": candidate.source_job_id,
                    "title": candidate.title,
                    "location": candidate.location,
                    "country": candidate.country,
                    "url": candidate.url,
                    "description_length": len(candidate.description or ""),
                }
                for candidate in result.candidates[:10]
            ],
        })
    except Exception as exc:
        report["adapter_test"].update({
            "status": "failed",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
    return report


class CompanyWatchAdapterRegistry:
    def __init__(self) -> None:
        self._adapters = {
            "greenhouse": GreenhouseAdapter(),
            "smartrecruiters": SmartRecruitersAdapter(),
            "personio": PersonioAdapter(),
            "successfactors": SuccessFactorsAdapter(),
            "workday": WorkdayAdapter(),
            "recruitee": RecruiteeAdapter(),
            "join": JoinAdapter(),
            "ashby": AshbyAdapter(),
            "b-ite": BiteAdapter(),
        }

    @property
    def supported_systems(self) -> set[str]:
        return set(self._adapters)

    def fetch(self, entry: dict[str, object], system: str) -> AdapterResult:
        adapter = self._adapters.get(str(system or "").casefold())
        if adapter is None:
            raise CompanyWatchError(f"No adapter is implemented for career system '{system}'.")
        return adapter.fetch(entry)

    def fetch_description(self, candidate: JobCandidate) -> str:
        """Reload a job description directly from its public detail page."""
        if not candidate.url:
            raise CompanyWatchError("The selected job has no detail URL.")
        description = _description_from_job_page(candidate.url)
        if not description:
            raise CompanyWatchError("No job description was found on the detail page.")
        return description
