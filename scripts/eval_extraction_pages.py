"""Score explicitly reviewed physical PDF pages in isolated candidate records.

This read-only checker never extracts, embeds, loads the application, or treats a
text match as proof that an incomplete page/document is complete.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import unicodedata


def _normal(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def score_page(page: dict, checks: list[dict]) -> list[dict]:
    native = _normal(" ".join(str(block.get("text", "")) for block in page.get("blocks", [])))
    ocr = _normal(str(page.get("ocr_text", "")))
    image_ocr = _normal(" ".join(str(region.get("ocr_text", ""))
                                 for region in page.get("ocr_regions", [])))
    tables = page.get("tables", [])
    scored = []
    for check in checks:
        kind = check["kind"]
        evidence = {}
        if kind == "reading_order":
            # One evidence stream only. Never repair a native ordering failure
            # by borrowing OCR or concatenating unrelated image regions.
            source = check["source"]
            if source not in {"native", "ocr"}:
                raise ValueError("Reading order requires native or ocr source")
            values = [_normal(value) for value in check["values"]]
            if len(values) < 2 or any(not value for value in values):
                raise ValueError("Reading order requires at least two nonempty anchors")
            body = native if source == "native" else ocr
            positions = [body.find(value) for value in values]
            counts = [body.count(value) for value in values]
            # Repeated anchors are ambiguous, not evidence of a unique order.
            matched = (all(count == 1 for count in counts)
                       and all(a + len(value) <= b for a, b, value
                               in zip(positions, positions[1:], values)))
            sources = [source] if matched else []
            evidence = {"anchor_positions": positions, "anchor_counts": counts,
                        "reason": ("anchor_missing" if 0 in counts else
                                   "anchor_ambiguous" if any(count > 1 for count in counts) else
                                   "order_matches" if matched else "order_mismatch")}
        elif kind == "text":
            needle = _normal(check["value"])
            sources = [label for label, body in (("native", native), ("ocr", ocr),
                                                ("image_region_ocr", image_ocr))
                       if needle in body and check.get("source", label) == label]
            if check.get("source", "native") not in {"native", "ocr", "image_region_ocr"}:
                raise ValueError("Unknown text evidence source")
            matched = bool(sources)
        elif kind == "image_region_text":
            needle = _normal(check["value"])
            matched = any(needle in _normal(str(region.get("ocr_text", "")))
                          for region in page.get("ocr_regions", []))
            sources = ["image_region_ocr"] if matched else []
        elif kind == "table_cell":
            sources = []
            column = check["column"]
            original_match = any(
                isinstance(row, list) and len(row) > column
                and _normal(str(row[0] or "")).startswith(_normal(check["row_label"]))
                and _normal(str(row[column] or "")) == _normal(check["value"])
                for table in tables for row in table.get("rows", [])
            )
            geometry_match = any(
                isinstance(row, list) and len(row) > column
                and _normal(str(row[0] or "")).startswith(_normal(check["row_label"]))
                and _normal(str(row[column] or "")) == _normal(check["value"])
                for table in tables for row in table.get("geometry_rows", [])
            )
            matched = original_match or geometry_match
            if original_match:
                sources.append("table_cell")
            if geometry_match:
                sources.append("table_geometry_candidate")
        elif kind == "no_table":
            matched = not tables
            sources = ["table_structure"] if matched else []
        else:
            raise ValueError(f"Unknown gold check kind: {kind}")
        scored.append({"check": check, "matched": matched, "sources": sources, **evidence})
    return scored


def evaluate(gold: dict, checkpoints: list[dict]) -> list[dict]:
    results = []
    for record in checkpoints:
        if record.get("schema") != 1:
            raise ValueError("Unsupported candidate checkpoint schema")
    for item in gold["rows"]:
        matching = [record for record in checkpoints
                    if item["source_sha256"] == record["source_sha256"]]
        if not matching:
            results.append({"pdf": item["pdf"], "physical_page": item["page"],
                            "profile_identity": None, "document_status": "checkpoint_missing",
                            "page_outcome": "not_attempted", "checks": [], "scored": False})
            continue
        for record in matching:
            pages = {page["page"]: page for page in record["pages"]}
            page = pages.get(item["page"])
            if page is None:
                results.append({"pdf": item["pdf"], "physical_page": item["page"],
                                "profile_identity": record["profile_identity"],
                                "document_status": record["status"], "page_outcome": "not_requested",
                                "checks": [], "scored": False})
                continue
            results.append({"pdf": item["pdf"], "physical_page": item["page"],
                            "profile_identity": record["profile_identity"],
                            "document_status": record["status"], "page_outcome": page["outcome"],
                            "warnings": page.get("warnings", []),
                            "checks": score_page(page, item["checks"]), "scored": True})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    args = parser.parse_args()
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    if gold.get("schema") != 1:
        raise ValueError("Unsupported gold schema")
    checkpoints = [json.loads(path.read_text(encoding="utf-8")) for path in args.checkpoint]
    results = evaluate(gold, checkpoints)
    summary = {"pages_scored": sum(row["scored"] for row in results),
               "checks_matched": sum(check["matched"] for row in results for check in row["checks"]),
               "checks_total": sum(len(row["checks"]) for row in results),
               "results": results}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
