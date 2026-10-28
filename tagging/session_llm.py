"""The model a library-maintenance session uses for tag proposals.

Library-workflow plan, lot 3 (2026-09-21). The maintainer picks, for ONE run,
which provider and model propose tags — a local model most days, an online
one when it is worth paying for. Four rules, each enforced here rather than
left to the caller:

1. **The choice never touches the global configuration.** It lives on the
   session record (``accession.imports``) and is applied to a DEEP COPY of the
   config for each call, so the chat panels, the deck generator and every other
   feature keep using the provider the Settings window names.
2. **No silent fallback.** The copy's ``fallback_provider`` is cleared, so
   ``core.llm.chat.stream_chat_messages`` builds a policy with no fallback at
   all: a local model that is down fails visibly instead of quietly sending the
   paper to an online provider — and an online model that errors does not
   switch to some other paid one either. A session names ONE model.
3. **What leaves the machine is disclosed** (:func:`disclosure`): provider,
   model, whether it is online, how many characters of which source.
4. **An online session spends under a ceiling it was given.** Before each call
   the WORST case is priced — the prompt's estimated input tokens at the input
   rate plus the full ``max_tokens`` cap at the output rate — and the call is
   refused when it could push the session past its budget. After the call the
   REAL cost is read back from the usage tracker and added. The guarantee is
   therefore exactly as good as two things this module cannot make better and
   says so: the token ESTIMATE of the prompt (tiktoken ``cl100k_base``, not the
   provider's own tokenizer) and ``PRICING_TABLE`` (a price change upstream is
   invisible). A model with no price cannot be budgeted at all, so it is
   accepted ONLY under ``no_limit`` (audit remediation 2026-09-23, maintainer
   decision): "No spending limit — I accept the cost" is the one honest way to
   run a model whose cost cannot be bounded. With ``no_limit`` no ceiling is
   checked before a call and the spend is still measured and reported
   (unknown when unpriced). A ceiling and ``no_limit`` together are refused:
   the two say opposite things. The retired ``allow_unpriced`` flag (which
   let an unpriced model run under a ceiling it could not honour — the
   budget check then silently compared nothing) is refused with guidance,
   never silently mapped to ``no_limit``.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Optional

from core.llm.factory import ALL_PROVIDER_NAMES, LOCAL_PROVIDER_NAMES

#: Upper bound of a session budget. A typo (``500`` for ``5.00``) should fail
#: loudly, not authorise a run that costs a hundred times what was meant.
MAX_BUDGET_USD = 100.0


class SessionLLMError(ValueError):
    """The session's model choice is invalid or cannot be honoured."""


class BudgetExceeded(SessionLLMError):
    """The next call could take the session past its approved ceiling."""


@dataclass(frozen=True)
class SessionLLM:
    provider: str
    model: str
    budget_usd: Optional[float]
    #: Retired (audit remediation 2026-09-23): always False on a validated
    #: choice. Kept as a field so stored session records keep their shape.
    allow_unpriced: bool
    #: "No spending limit — I accept the cost": the only way to run an
    #: unpriced online model, and never combined with a ceiling.
    no_limit: bool = False

    @property
    def online(self) -> bool:
        return is_online(self.provider, self.model)

    def as_dict(self) -> dict:
        return {'provider': self.provider, 'model': self.model, 'budget_usd': self.budget_usd,
                'allow_unpriced': self.allow_unpriced, 'no_limit': self.no_limit,
                'online': self.online}


def is_online(provider: str, model: str) -> bool:
    """Whether the paper leaves the machine.

    An online provider, obviously — but also an Ollama "cloud" model
    (``gpt-oss:120b-cloud``, ``…:cloud``): Ollama serves it through the local
    daemon while the inference runs on Ollama's servers, so calling it local
    would be exactly the silent online call this module exists to prevent
    (review 2026-09-21). Such a model is unpriced here, so a session must
    accept its cost explicitly (``no_limit``).
    """
    if provider not in LOCAL_PROVIDER_NAMES:
        return True
    m = (model or '').lower()
    return m.endswith('-cloud') or m.endswith(':cloud') or ':cloud' in m


def _price(model: str, cfg: dict):
    """The model's price row, honouring ``llm_pricing_overrides``; None if unpriced."""
    from core.llm.usage import PRICING_TABLE, ModelPricing
    overrides = cfg.get('llm_pricing_overrides') or {}
    if isinstance(overrides, dict) and model in overrides:
        try:
            row = overrides[model]
            return ModelPricing(input=float(row['input']), output=float(row['output']),
                                cached_input=float(row.get('cached_input', row['input'])))
        except (KeyError, TypeError, ValueError):
            return None
    return PRICING_TABLE.get(model)


def validate(raw: dict, cfg: dict) -> SessionLLM:
    """Parse and check a session's model choice. Raises :class:`SessionLLMError`.

    Refused: an unknown provider, an empty or malformed model id, a budget out
    of range, an online choice without a budget (unless ``no_limit``), an
    unpriced online model without ``no_limit`` (a budget cannot bound it),
    ``no_limit`` given together with a budget, and the retired
    ``allow_unpriced`` without ``no_limit``. A local choice needs no budget
    (it costs nothing billable) and any given one is kept only for display.
    """
    if not isinstance(raw, dict):
        raise SessionLLMError('The session model must be an object.')
    provider = str(raw.get('provider') or '').strip().lower()
    if provider not in ALL_PROVIDER_NAMES:
        raise SessionLLMError(f'Unknown provider {provider!r}.')
    model = raw.get('model')
    if (not isinstance(model, str) or not model.strip() or len(model.strip()) > 120
            or any(ch in model for ch in ('\x00', '\n', '\r'))):
        raise SessionLLMError('A model id is required (at most 120 characters).')
    model = model.strip()
    no_limit = raw.get('no_limit') is True
    if raw.get('allow_unpriced') is True and not no_limit:
        # Refused, never mapped: accepting it as "no limit" would silently
        # drop a ceiling the caller believes it set.
        raise SessionLLMError(
            'allow_unpriced is retired: an unpriced model cannot be held to a spending '
            'limit. Choose "No spending limit" (no_limit) to accept its cost, or a priced model.')
    budget = raw.get('budget_usd')
    if budget is not None:
        # bool is an int subclass: `True` must not read as a one-dollar budget.
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
                or not 0.0 <= float(budget) <= MAX_BUDGET_USD or budget != budget:
            raise SessionLLMError(f'budget_usd must be between 0 and {MAX_BUDGET_USD:g}.')
        budget = float(budget)
    if no_limit and budget is not None:
        raise SessionLLMError('no_limit and budget_usd say opposite things; give one of them.')
    choice = SessionLLM(provider=provider, model=model, budget_usd=budget,
                        allow_unpriced=False, no_limit=no_limit)
    if choice.online and not no_limit:
        if _price(model, cfg) is None:
            raise SessionLLMError(
                f'{model} has no price in the pricing table, so a spending limit cannot bound it. '
                'Choose "No spending limit" (no_limit) to accept its cost, or a priced model.')
        if budget is None:
            raise SessionLLMError('An online model needs a spending limit (budget_usd) for the session, '
                                  'or no_limit to accept the cost.')
    return choice


def call_config(cfg: dict) -> dict:
    """A per-call copy of the config with every fallback disabled (rule 2).

    Deep, because callers downstream may mutate nested values; the global
    cached config must never see this session's choice.
    """
    out = copy.deepcopy(dict(cfg))
    out['fallback_provider'] = None
    out['fallback_on'] = []
    # ONE attempt per call: the online adapters retry a transient failure
    # `online_max_retries` times (attempts), each of which may be billed, and
    # the worst case is priced for one. A retry is the session's decision
    # (propose again), made visibly.
    out['online_max_retries'] = 1
    return out


def worst_case_usd(choice: SessionLLM, cfg: dict, *, prompt_text: str,
                   max_tokens: int) -> Optional[float]:
    """Upper-bound cost of one call, or None when the model is unpriced.

    Local models cost 0. Output is priced at the full cap: a reply may use it.
    """
    if not choice.online:
        return 0.0
    price = _price(choice.model, cfg)
    if price is None:
        return None
    from core.llm.usage import estimate_tokens
    # Overrides need not discount cached tokens. A worst case must cover
    # either rate rather than assume that every cache hit is cheaper.
    input_rate = max(price.input, price.cached_input or 0)
    return (estimate_tokens(prompt_text) / 1e6) * input_rate + (max_tokens / 1e6) * price.output


def check_budget(choice: SessionLLM, spent_usd: Optional[float], worst: Optional[float]) -> None:
    """Raise :class:`BudgetExceeded` when the next call could overrun.

    ``spent_usd`` None means a previous call's cost could not be measured
    (unpriced, or its charge could not be recorded): with a budget set, that
    is an unknown spend and further calls are refused rather than guessed —
    ALWAYS (audit remediation 2026-09-23, L3: the retired ``allow_unpriced``
    returned here without comparing anything, so a ceiling set on such a
    session bounded nothing, and a PRICED model chosen after an unknown spend
    was unchecked too). Under ``no_limit`` nothing is checked.
    """
    if choice.no_limit or not choice.online or choice.budget_usd is None:
        return
    if worst is None:
        raise BudgetExceeded(f'{choice.model} is unpriced; its cost cannot be bounded.')
    if spent_usd is None:
        raise BudgetExceeded('This session already has an unmeasured spend; the budget cannot be checked.')
    if spent_usd + worst > choice.budget_usd + 1e-12:
        raise BudgetExceeded(
            f'Stopping before the next proposal: ${spent_usd:.4f} spent, up to ${worst:.4f} '
            f'more for the next call, against a limit of ${choice.budget_usd:.2f}.')


def disclosure(choice: SessionLLM, *, chars: int, source: str) -> dict:
    """What this call sends, and where (rule 3)."""
    return {'provider': choice.provider, 'model': choice.model, 'online': choice.online,
            'chars_sent': int(chars), 'source': source}
