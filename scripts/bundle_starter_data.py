#!/usr/bin/env python3
"""Bundle saved test-data reports for offline, PostgreSQL-backed app startup."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sec_disclosure.alignment_policy import EXCLUSION_POLICY, exclude_from_reports


def bundle(ref, destination):
    commit = subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", commit, "test_data"], cwd=ROOT, text=True).splitlines()
    names = {"alignments": {"alignments.json", "needs_review.json"},
             "disclosures": {"disclosures.json", "review_candidates.json"},
             "peer_comparison": {"peer_comparison.json"}}
    selected = [name for name in paths if Path(name).name in names.get(name.split("/")[1], set())
                or name.startswith("test_data/synthesis/") and name.endswith("_synthesis_output.json")]
    order = {"disclosures": 0, "alignments": 1, "synthesis": 2, "peer_comparison": 3}
    selected.sort(key=lambda name: (order[name.split("/")[1]], name))
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "source_branch": "test-data", "source_commit": commit,
                "alignment_exclusion_policy": EXCLUSION_POLICY, "excluded_unreviewable_alignments": 0, "files": []}
    companies = set()
    alignment_group, alignment_reports = None, {}
    with (destination / "reports.tar.gz").open("wb") as output, \
            gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for original in selected:
            if original.startswith("test_data/alignments/"):
                group_path = str(Path(original).parent)
                if alignment_group != group_path:
                    alignment_group = group_path
                    alignment_reports = {name: json.loads(subprocess.check_output(
                        ["git", "show", f"{commit}:{group_path}/{name}"], cwd=ROOT))
                        for name in sorted(names["alignments"]) if f"{group_path}/{name}" in selected}
                    manifest["excluded_unreviewable_alignments"] += len(exclude_from_reports(alignment_reports))
                report = alignment_reports[Path(original).name]
            else:
                raw = subprocess.check_output(["git", "show", f"{commit}:{original}"], cwd=ROOT)
                report = json.loads(raw)
            raw = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode()
            name = original.removeprefix("test_data/")
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(raw))
            entry = {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            group = name.split("/")[0]
            if group == "disclosures":
                scope = {"kind": group, "company": report["company"].lower(), "years": [int(report["fiscal_year"])]}
            elif group == "alignments":
                scope = {"kind": group, "company": report["company"].lower(),
                         "years": [int(report["previous_year"]), int(report["current_year"])]}
            elif group == "synthesis":
                scope = {"kind": group, "company": report["company"].lower(), "years": sorted(map(int, report["years"]))}
            else:
                scope = {"kind": group, "company": report["target"].lower(), "years": [int(report["fiscal_year"])],
                         "peers": sorted(p.lower() for p in report["peers"]), "items": sorted(report["items"]),
                         "target_selection": report.get("target_selection") or "all"}
            entry["scope"] = scope
            if name.startswith(("disclosures/", "alignments/")):
                companies.add(name.split("/")[1])
            manifest["files"].append(entry)
    archive_path = destination / "reports.tar.gz"
    manifest.update(companies=sorted(companies), archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest())
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Bundled {len(selected)} reports for {len(companies)} companies; {archive_path.stat().st_size / 1024**2:.1f} MiB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="origin/test-data")
    parser.add_argument("--output", type=Path, default=ROOT / "starter_data")
    args = parser.parse_args()
    bundle(args.ref, args.output)
