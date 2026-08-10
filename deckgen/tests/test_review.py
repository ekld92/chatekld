

# --- section-scoped review parsing (piecewise repair, Batch F 2026-07-22) ---

def test_parse_section_review_accepts_excerpt_refuses_whole_doc():
    from deckgen.review import parse_section_review
    reply = (
        "ISSUES:\n- unbalanced brace in the itemize\n\n"
        "```latex\n\\section{Intro}\n\\begin{frame}{Intro}\n fixed \\end{frame}\n```\n"
    )
    r = parse_section_review(reply)
    assert r.issues == ["unbalanced brace in the itemize"]
    assert r.repaired_tex.startswith("\\section{Intro}")
    # A whole-document reply must NOT be spliced into a section span.
    whole = (
        "ISSUES:\n- x\n\n"
        "```latex\n\\documentclass{beamer}\n\\begin{document}\n\\section{A}\n"
        "\\end{document}\n```\n"
    )
    r2 = parse_section_review(whole)
    assert r2.repaired_tex is None


def test_parse_section_review_truncated_fence_and_no_issues():
    from deckgen.review import parse_section_review
    cut = "ISSUES:\n- broken env\n\n```latex\n\\section{A}\n\\begin{frame}"
    r = parse_section_review(cut)
    assert r.repaired_tex is None
    assert r.repair_truncated is True
    none = parse_section_review("ISSUES:\nnone\n")
    assert none.issues == [] and none.repaired_tex is None
