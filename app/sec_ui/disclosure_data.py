"""Single-filing disclosure-type review inputs, independent of alignment runs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
import sys

from . import config, data


@dataclass
class Source:
    scopes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def companies(self):
        return sorted({company for company, _ in self.scopes})

    def years(self, company):
        return sorted(year for co, year in self.scopes if co == company)


def discover_source():
    source = Source()
    mode = config.data_source_mode()
    if mode in {"auto", "postgres"}:
        try:
            with data._postgres_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT DISTINCT f.company, f.fiscal_year FROM filings f "
                            "JOIN disclosures d ON d.filing_id=f.filing_id WHERE f.form_type='10-K' "
                            "AND EXISTS (SELECT 1 FROM pipeline_stage_snapshots s WHERE s.company=f.company "
                            "AND s.kind='disclosures' AND s.previous_year=f.fiscal_year AND s.current_year=f.fiscal_year "
                            "AND s.documents->'token_usage.json'->'payload'->>'run_complete'='true')")
                source.scopes.update({(company, int(year)): "postgres" for company, year in cur.fetchall()})
        except Exception as error:
            source.notes.append(f"Disclosure database not used ({type(error).__name__}).")
    root = config.disclosures_dir()
    if mode in {"auto", "files"} and root is not None and root.is_dir():
        for company in sorted(path for path in root.iterdir() if path.is_dir()):
            for year in sorted(path for path in company.iterdir() if path.is_dir() and re.fullmatch(r"\d{4}", path.name)):
                if any((year / name).is_file() for name in ("disclosures.json", "review_candidates.json")):
                    source.scopes.setdefault((company.name.lower(), int(year.name)), "files")
    return source


def load_disclosures(company, year, source=None, *, include_all=False):
    source = source or discover_source()
    mode = source.scopes.get((company, year))
    if mode is None:
        raise data.DataError(f"No extracted disclosures for {company.upper()} FY{year}.")
    project_src = str(config.ROOT / "src")
    if project_src not in sys.path:
        sys.path.insert(0, project_src)
    from sec_disclosure.llm.disclosures import apply_source_unit_policy

    if mode == "postgres":
        from sec_disclosure.storage.exports import get_disclosure_reports, DownstreamExportError
        try:
            reports = get_disclosure_reports(company, year)
        except DownstreamExportError as error:
            raise data.DataError(str(error)) from None
    else:
        folder = config.disclosures_dir() / company / str(year)
        reports = {name: data._read_json(folder / name) for name in ("disclosures.json", "review_candidates.json")
                   if (folder / name).is_file()}
    records = []
    seen = set()
    for name, report in reports.items():
        if not isinstance(report, dict) or not isinstance(report.get("disclosures"), list):
            raise data.DataError(f"Invalid disclosure report: {name}.")
        for raw in report.get("disclosures", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("disclosure_id"), str):
                raise data.DataError(f"Invalid disclosure entry in {name}.")
            if raw["disclosure_id"] in seen:
                raise data.DataError("A disclosure ID appears more than once in the extraction reports.")
            seen.add(raw["disclosure_id"])
            verification = apply_source_unit_policy(raw.get("verification") or {})
            needs_review = (verification.get("status") == "needs_review" or bool(verification.get("review_reasons"))
                            or bool((raw.get("human_review") or {}).get("was_needs_review"))
                            or not verification and name == "review_candidates.json")
            if needs_review or include_all:
                row = deepcopy(raw)
                row.update(company=company, fiscal_year=int(year), verification=verification, needs_review=needs_review)
                records.append(row)
    return sorted(records, key=lambda row: row["disclosure_id"])
