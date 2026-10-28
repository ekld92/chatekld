"""Identity-bound proposal inputs and conservative session pricing."""
from unittest import mock
import test_library_workflow_lot3 as base
from accession import imports
from tagging import session_llm
from core.llm.types import LLMUsage
from core.llm.usage import estimate_cost_usd


def test_pricing_override_cached_rate_is_part_of_worst_case():
    cfg = {'llm_pricing_overrides': {'gpt-4o-mini': {'input': 1, 'output': 1, 'cached_input': 5}}}
    choice = session_llm.validate({'provider': 'openai', 'model': 'gpt-4o-mini', 'budget_usd': 1}, cfg)
    with mock.patch('core.llm.usage.estimate_tokens', return_value=4000):
        ceiling = session_llm.worst_case_usd(choice, cfg, prompt_text='fixture', max_tokens=256)
    actual = estimate_cost_usd(choice.model, LLMUsage(input_tokens=4000, output_tokens=256,
                                                    cached_input_tokens=4000), overrides=cfg['llm_pricing_overrides'])
    assert ceiling >= actual, (ceiling, actual)



def test_proposal_resume_uses_unit_document_after_rename(tmp_path):
    h = base.Harness(tmp_path)
    att, old = base._tracked_attachment(h)
    new = h.lib.rename(att, old, 'renamed.pdf')
    new.write_bytes(b'%PDF ORIGINAL PAPER depression and mood in adult patients ' * 4)
    old.write_bytes(b'%PDF FOREIGN PAPER oncology and brain tumors in children ' * 4)
    sid = imports.begin_session(h.lib.settings)
    imports.record_pass(h.lib.settings, sid, imports.observe(h.lib.settings))
    with h:
        sid = h.session(provider='ollama', model='llama3')
        with mock.patch('api.routes.tagging.text_source.read_pdf_text',
                        side_effect=lambda path, *a, **k: (path.read_text(), False)):
            response = h.propose(session=sid, unit='attachment:1:PDFA0001')
        assert response.status_code == 200, response.get_json()
        message = h.providers['ollama'].calls[0].messages[0]['content']
        assert 'ORIGINAL PAPER' in message and 'FOREIGN PAPER' not in message
