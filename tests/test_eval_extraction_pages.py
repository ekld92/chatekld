"""Disposable page-gold checks keep text and completeness evidence separate."""

from scripts.eval_extraction_pages import evaluate, score_page
import pytest


def test_reading_order_is_source_specific_and_rejects_ambiguous_anchors():
    check = {"kind": "reading_order", "source": "native", "values": ["Left end", "Right start"]}
    for body, reason in [("Left end Right start", "order_matches"),
                         ("Right start Left end", "order_mismatch"),
                         ("Left end", "anchor_missing"),
                         ("Left end Right start Left end", "anchor_ambiguous")]:
        page = {"blocks": [{"text": body}], "ocr_text": "Left end Right start"}
        result = score_page(page, [check])[0]
        assert result["matched"] is (reason == "order_matches")
        assert result["reason"] == reason
    with pytest.raises(ValueError, match="source"):
        score_page({}, [{**check, "source": "combined"}])


def test_source_bound_text_does_not_borrow_another_stream():
    check = {"kind": "text", "source": "ocr", "value": "agoraphobia"}
    assert not score_page({"blocks": [{"text": "agoraphobia"}]}, [check])[0]["matched"]
    assert score_page({"ocr_text": "agoraphobia"}, [check])[0]["sources"] == ["ocr"]


def test_original_and_geometry_matches_are_both_reported():
    rows = [["Label", "42"]]
    page = {"tables": [{"rows": rows, "geometry_rows": rows}]}
    check = {"kind": "table_cell", "row_label": "Label", "column": 1, "value": "42"}
    assert score_page(page, [check])[0]["sources"] == ["table_cell", "table_geometry_candidate"]


def test_ocr_match_does_not_certify_partial_page():
    record = {"schema": 1, "source_sha256": "a" * 64, "profile_identity": "p",
              "status": "partial", "pages": [{"page": 2, "outcome": "truncated",
                                           "warnings": ["token_cap"], "blocks": [],
                                           "ocr_text": "hypothèses"}]}
    gold = {"rows": [{"pdf": "f.pdf", "source_sha256": "a" * 64, "page": 2,
                      "checks": [{"kind": "text", "value": "hypothèses"}]}]}
    result = evaluate(gold, [record])[0]
    assert result["checks"][0]["matched"] is True
    assert result["page_outcome"] == "truncated"
    assert result["document_status"] == "partial"


def test_image_region_match_stays_separate_from_native_and_partial():
    page = {"outcome": "skipped", "blocks": [{"text": "Native sentence"}],
            "ocr_regions": [{"physical_page": 1, "bbox": [1, 2, 3, 4],
                             "ocr_outcome": "completed", "ocr_text": "Image label"}]}
    scored = score_page(page, [{"kind": "text", "value": "Image label"}])
    assert scored[0]["sources"] == ["image_region_ocr"]
    assert page["outcome"] == "skipped"


def test_image_region_only_check_does_not_borrow_native_text():
    page = {"blocks": [{"text": "n = 234"}],
            "ocr_regions": [{"ocr_text": "A - 254", "ocr_outcome": "completed"}]}
    checks = [{"kind": "text", "value": "234"},
              {"kind": "image_region_text", "value": "234"},
              {"kind": "image_region_text", "value": "254"}]
    scored = score_page(page, checks)
    assert [(row["matched"], row["sources"]) for row in scored] == [
        (True, ["native"]), (False, []), (True, ["image_region_ocr"])]
    split_regions = {"ocr_regions": [{"ocr_text": "All -"}, {"ocr_text": "234"}]}
    assert score_page(split_regions, [
        {"kind": "image_region_text", "value": "All - 234"}])[0]["matched"] is False


def test_flattened_numeric_row_fails_column_check():
    page = {"blocks": [{"text": "Prospective 250 (70.8%)"}],
            "tables": [{"rows": [["Prospective 250 (70.8%)", None, None]]}]}
    checks = [{"kind": "text", "value": "250 (70.8%)"},
              {"kind": "table_cell", "row_label": "Prospective", "column": 2,
               "value": "250 (70.8%)"}, {"kind": "no_table"}]
    assert [row["matched"] for row in score_page(page, checks)] == [True, False, False]


def test_geometry_candidate_is_scored_separately_from_original_table():
    page = {"blocks": [], "tables": [{
        "rows": [["Prospective 250 (70.8%)", None, None]],
        "geometry_rows": [["Prospective", None, "250 (70.8%)"]],
        "geometry_status": "unverified_word_position_candidate"}]}
    check = {"kind": "table_cell", "row_label": "Prospective", "column": 2,
             "value": "250 (70.8%)"}
    assert score_page(page, [check])[0] == {
        "check": check, "matched": True, "sources": ["table_geometry_candidate"]}


def test_unrequested_gold_page_remains_unscored():
    record = {"schema": 1, "source_sha256": "b" * 64, "profile_identity": "p",
              "status": "completed", "pages": [{"page": 1, "outcome": "completed"}]}
    gold = {"rows": [{"pdf": "f.pdf", "source_sha256": "b" * 64, "page": 3,
                      "checks": [{"kind": "text", "value": "x"}]}]}
    result = evaluate(gold, [record])[0]
    assert result["scored"] is False
    assert result["page_outcome"] == "not_requested"


def test_missing_checkpoint_is_reported():
    gold = {"rows": [{"pdf": "f.pdf", "source_sha256": "c" * 64, "page": 1,
                      "checks": [{"kind": "text", "value": "x"}]}]}
    result = evaluate(gold, [])[0]
    assert result["scored"] is False
    assert result["document_status"] == "checkpoint_missing"
