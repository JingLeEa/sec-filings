"""Map displayed text to saved disclosure/paragraph sources without guessing ownership."""
from __future__ import annotations


def _sources_match(member):
    sources = member.get("sources") or []
    return bool(sources) and all(source.get("selected_text") for source in sources) and (
        "\n\n".join(source["selected_text"] for source in sources) == member.get("content"))


def source_blocks(record: dict, side: str) -> list:
    """Offsets refer to the original compared text; source labels are never diffed."""
    text = record[f"{side}_text"]
    members = record[f"{side}_members"]
    full = [member for member in members if member.get("content")]
    blocks = []
    if full and "\n\n".join(member["content"] for member in full) == text:
        offset = 0
        for member in members:
            content = member.get("content") or ""
            if content and offset:
                offset += 2
            sources = member["sources"] if _sources_match(member) else [{"selected_text": content}]
            within = 0
            for source in sources:
                selected = source["selected_text"]
                blocks.append({"disclosure_id": member["disclosure_id"], "paragraph_id": source.get("paragraph_id") or "",
                               "start": offset + within, "end": offset + within + len(selected)})
                within += len(selected) + 2
            offset += len(content)
        return blocks

    evidence_side = "earlier" if side == "previous" else "later"
    evidence = [entry for entry in record["evidence"] if entry["side"] == evidence_side and entry.get("text")]
    if evidence and " ".join(entry["text"] for entry in evidence) == text:
        offset = 0
        for entry in evidence:
            owner = (entry["disclosure_id"], entry.get("paragraph_id") or "")
            end = offset + len(entry["text"])
            if blocks and (blocks[-1]["disclosure_id"], blocks[-1]["paragraph_id"]) == owner:
                blocks[-1]["end"] = end
            else:
                blocks.append({"disclosure_id": owner[0], "paragraph_id": owner[1], "start": offset, "end": end})
            offset = end + 1
        represented = {block["disclosure_id"] for block in blocks}
        blocks.extend({"disclosure_id": member["disclosure_id"], "paragraph_id": "", "start": len(text), "end": len(text)}
                      for member in members if member["disclosure_id"] not in represented)
        return blocks

    if not text:
        return [{"disclosure_id": member["disclosure_id"], "paragraph_id": "", "start": 0, "end": 0} for member in members]
    return [{"disclosure_id": members[0]["disclosure_id"] if len(members) == 1 else "",
             "paragraph_id": "", "start": 0, "end": len(text)}]


def sentence_rows(record: dict) -> list:
    """Keep every selected sentence's owner, including repeated text in different disclosures."""
    cited = {(entry["disclosure_id"], entry.get("sentence_id")) for entry in record["evidence"]}
    rows, seen = [], set()
    for side, label in (("previous", "earlier"), ("current", "later")):
        for member in record[f"{side}_members"]:
            if not _sources_match(member):
                continue
            for source in member["sources"]:
                for sentence in source.get("sentences") or []:
                    key = (member["disclosure_id"], sentence.get("sentence_id"))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({"side": label, "disclosure_id": key[0], "sentence_id": key[1],
                                 "paragraph_id": source.get("paragraph_id") or sentence.get("paragraph_id"),
                                 "source_url": source.get("source_url") or sentence.get("source_url"),
                                 "text": sentence.get("text", ""), "cited": key in cited})
    for entry in record["evidence"]:
        key = (entry["disclosure_id"], entry.get("sentence_id"))
        if key not in seen:
            rows.append({**entry, "cited": True})
            seen.add(key)
    return rows
