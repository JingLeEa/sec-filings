import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import psycopg
from sec_disclosure.storage.disclosures import connection_arguments
from synthesis_dev.retrieval import fetch_change_evidence

with psycopg.connect(**connection_arguments()) as conn:
    changes = fetch_change_evidence(
        conn,
        company="amd",
        previous_year=2023,
        current_year=2024,
        limit=500,
    )

print(f"Retrieved {len(changes)} AMD disclosure changes.")

target = next(
    (change for change in changes if change["match_id"] == "amd_2023_2024_M0269"),
    None,
)

if target is None:
    print("Target M0269 was not found. Available examples:")
    for change in changes[:5]:
        print(change["match_id"], change["taxonomy"])
    raise SystemExit(1)

print("\n=== TARGET CHANGE ===")
print("Match ID:", target["match_id"])
print("Alignment status:", target["alignment_status"])
print("Relationship:", target["relationship"])
print("Taxonomy:", target["taxonomy"])
print("Materiality:")
print(json.dumps(target["materiality"], indent=2, default=str))

print("\n=== DISCLOSURE MEMBERS ===")
for member in target["members"]:
    print(
        f'{member["side"]} | {member["item"]} | '
        f'{member["section"]} | {member["taxonomy"]}'
    )
    print("Summary:", member["summary"])

print(f"\n=== SOURCE SENTENCES ({target['evidence_count']} total) ===")
for evidence in target["source_evidence"][:6]:
    print(f'\n[{evidence["disclosure_id"]}]')
    print(evidence["text"])
    print("Source:", evidence["source_url"])
