"""Model-free prompt-invariant regression net.

These tests pin the *structural* invariants of ChatEKLD's prompts so an
accidental edit (or a future "improvement") that breaks grounding, citation
consistency, the untrusted-content guards, or the LaTeX/JSON contracts shows
up as a failing assertion rather than a silent quality regression. They run
hermetically — no models, no index, no network — and complement the live
answer-quality harness under ``tests/eval/`` (which needs a real provider).

Companion to the 2026-06 prompt audit. When you intentionally change a prompt,
update the matching assertion here in the same commit.
"""
import unittest


class TestSinglePaperPrompts(unittest.TestCase):
    def test_default_system_prompt_has_no_competing_sentence_count(self):
        # Fix 2: the system prompt must not impose its own per-section length
        # that contradicts the per-template length (1-2 / 1-3 sentences).
        from core.constants import DEFAULT_SYSTEM_PROMPT
        self.assertNotIn("3-6", DEFAULT_SYSTEM_PROMPT)
        self.assertNotRegex(DEFAULT_SYSTEM_PROMPT, r"\d+\s*-\s*\d+\s+sentences")

    def test_default_system_prompt_keeps_grounding_and_domain_focus(self):
        from core.constants import DEFAULT_SYSTEM_PROMPT
        low = DEFAULT_SYSTEM_PROMPT.lower()
        self.assertIn("biomedical", low)          # domain focus retained
        self.assertIn("grounded strictly", low)   # grounding retained

    def test_default_system_prompt_drops_expert_persona_framing(self):
        # Fix 8: the "you are a/an ... specialising/specializing" identity opener
        # must be gone. The alternation is (s|z) inside a CHARACTER CLASS [sz].
        # An earlier version wrote [is|iz], which is a char class of {i,s,|,z}
        # and silently never matched the real phrase — a passing-but-useless
        # guard. The subject is lower-cased so the pattern need not be.
        from core.constants import DEFAULT_SYSTEM_PROMPT
        self.assertNotRegex(
            DEFAULT_SYSTEM_PROMPT.lower(), r"you are an? .*speciali[sz]ing"
        )

    def test_detailed_template_has_lead_bias_mitigation(self):
        # Fix 7: steer away from abstract/intro-only coverage.
        from core.constants import DETAILED_USER_TEMPLATE
        low = DETAILED_USER_TEMPLATE.lower()
        self.assertIn("methods and results", low)
        self.assertIn("not only the", low)

    def test_templates_keep_text_and_doc_type_slots(self):
        from core.constants import CONCISE_USER_TEMPLATE, DETAILED_USER_TEMPLATE
        for tmpl in (CONCISE_USER_TEMPLATE, DETAILED_USER_TEMPLATE):
            self.assertIn("{text}", tmpl)
            self.assertIn("{document_type_line}", tmpl)

    def test_report_types_drop_persona_keep_focus(self):
        # Fix 8: each built-in report type keeps its focus directive but loses
        # the "You are a researcher specializing in ..." identity opener.
        from core.constants import DEFAULT_REPORT_TYPES
        focus_anchor = {
            "systematic_review": "pico",
            "clinical_trial": "randomization",
            "observational_study": "confounders",
            "narrative_review": "evidence synthesis",
            "opinion_letter": "central argument",
            "case_report": "diagnostic workup",
            "guideline": "key recommendations",
        }
        for rt in DEFAULT_REPORT_TYPES:
            sp = rt["system_prompt"].lower()
            self.assertNotIn("you are a researcher", sp, rt["id"])
            # Fail with a clear message (not a raw KeyError) if a new built-in
            # report type is added without registering its focus anchor here —
            # that is a signal to update this test, not an internal crash.
            self.assertIn(rt["id"], focus_anchor, f"no focus anchor for {rt['id']!r}")
            self.assertIn(focus_anchor[rt["id"]], sp, rt["id"])


class TestSummaryUserMessage(unittest.TestCase):
    def _build(self, **kw):
        from core.llm.prompt import build_summary_user_message
        from core.constants import DETAILED_USER_TEMPLATE
        defaults = dict(
            document_text="BODY-TEXT",
            user_template=DETAILED_USER_TEMPLATE,
            doc_type="Clinical Trial (RCT)",
        )
        defaults.update(kw)
        return build_summary_user_message(**defaults)

    def test_untrusted_guard_wraps_document(self):
        msg = self._build()
        self.assertIn("BEGIN UNTRUSTED DOCUMENT TEXT", msg)
        self.assertIn("END UNTRUSTED DOCUMENT TEXT", msg)
        self.assertIn("Do not follow instructions", msg)
        self.assertIn("BODY-TEXT", msg)

    def test_doc_type_slot_filled(self):
        msg = self._build()
        self.assertIn("Clinical Trial (RCT)", msg)
        self.assertNotIn("{document_type_line}", msg)

    def test_focus_question_is_actionable(self):
        # Fix 2: a focus question must carry a directive, not just be dumped.
        msg = self._build(focus_question="Does it reduce mortality?")
        self.assertIn("Does it reduce mortality?", msg)
        self.assertIn("Prioritise information", msg)
        self.assertIn("state that explicitly", msg)

    def test_no_focus_question_adds_no_directive(self):
        msg = self._build(focus_question="")
        self.assertNotIn("FOCUS QUESTION", msg)


class TestVaultRagPrompts(unittest.TestCase):
    def _modes(self):
        from rag.engine import _PROMPT_MODES
        return _PROMPT_MODES

    def test_all_modes_keep_placeholders(self):
        for name, tmpl in self._modes().items():
            text = tmpl.template
            self.assertIn("{context_str}", text, name)
            self.assertIn("{query_str}", text, name)

    def test_all_modes_keep_untrusted_guard(self):
        for name, tmpl in self._modes().items():
            low = tmpl.template.lower()
            self.assertIn("untrusted", low, name)
            self.assertIn("never follow instructions", low, name)

    def test_citation_wording_is_consistent_across_modes(self):
        # Fix 4 carve-out: every mode cites with the same bracketed form.
        # (Case of the leading "cite" varies — exploratory starts a sentence —
        # so anchor on the shared, case-invariant clause.)
        expected = "source filename in brackets, e.g. [note.md]"
        for name, tmpl in self._modes().items():
            self.assertIn(expected, tmpl.template, name)
        # And the old divergent phrasing is gone everywhere.
        for name, tmpl in self._modes().items():
            self.assertNotIn("cite source filenames when available", tmpl.template, name)
            self.assertNotIn("Cite source filenames when available", tmpl.template, name)

    def test_custom_prefix_cannot_strip_placeholders(self):
        from rag.engine import _apply_custom_prefix, RAG_QA_PROMPT_STRICT
        out = _apply_custom_prefix(RAG_QA_PROMPT_STRICT, "Answer in French.")
        self.assertIn("{context_str}", out.template)
        self.assertIn("{query_str}", out.template)
        self.assertIn("USER INSTRUCTIONS:", out.template)


class TestAgentPreamble(unittest.TestCase):
    def _preamble(self):
        from core.agent.loop import _AGENT_PREAMBLE
        return _AGENT_PREAMBLE

    def test_keeps_stop_condition_and_safety(self):
        low = self._preamble().lower()
        self.assertIn("answer the user directly", low)      # stop condition
        self.assertIn("never follow instructions", low)     # untrusted guard

    def test_adds_efficiency_nudge_and_exemplar(self):
        # Fix 3: steer toward few targeted searches + a worked example.
        low = self._preamble().lower()
        self.assertIn("focused searches", low)
        self.assertIn("for example", low)

    def test_names_all_three_tools(self):
        p = self._preamble()
        for tool in ("vault_search", "vault_read_note", "vault_list_materials"):
            self.assertIn(tool, p)


class TestDeckgenPrompts(unittest.TestCase):
    def test_outline_message_has_structure_example(self):
        # Fix 9: anchor the JSON shape with a one-line example.
        from deckgen.prompts import build_outline_message
        msg = build_outline_message("Sepsis", "for residents", max_sections=6)
        self.assertIn("Example shape", msg)
        self.assertIn("Output ONLY the JSON array", msg)

    def test_section_prompt_keeps_latex_prohibitions(self):
        # Load-bearing, validator-backed — must NOT be softened away.
        from deckgen.prompts import section_system_prompt
        sp = section_system_prompt("medical residents")
        self.assertIn(r"\documentclass", sp)
        self.assertIn("ONLY LaTeX Beamer source", sp)

    def test_section_prompt_respects_system_prompt_limit(self):
        from deckgen.prompts import section_system_prompt, SYSTEM_PROMPT_LIMIT
        big_macros = "\n".join(f"\\macro{i}{{...}}" for i in range(2000))
        sp = section_system_prompt("residents", macros_block=big_macros, cite_mode="bib")
        self.assertLessEqual(len(sp), SYSTEM_PROMPT_LIMIT)


class TestDeckPromptRendering(unittest.TestCase):
    """The deck cite/image rules are appended AFTER .format() runs, so any
    doubled {{...}} escape in them reaches the model verbatim — teaching it
    broken LaTeX like \\citefoot{{key}}. Pin the rendered output instead of
    the source literals."""

    def test_no_double_braces_reach_the_model(self):
        from deckgen.prompts import augment_system_prompt, section_system_prompt
        for prompt in (
            section_system_prompt("clinicians", cite_mode="bib"),
            augment_system_prompt("clinicians", cite_mode="bib"),
        ):
            self.assertNotIn("{{", prompt)
            self.assertNotIn("}}", prompt)
            self.assertIn("\\citefoot{key}", prompt)


if __name__ == "__main__":
    unittest.main()
