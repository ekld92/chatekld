import os
import sys
from pathlib import Path

# --- Application Paths ---
def _get_base_dir():
    """Returns the persistent data directory for ChatEKLD.

    ``CHATEKLD_BASE_DIR`` overrides the platform default.  The test suite
    sets it (in conftest.py, before any app import) to a per-session temp
    directory so tests never read or write the user's real config, index,
    or feedback files.
    """
    override = os.environ.get("CHATEKLD_BASE_DIR", "").strip()
    if override:
        os.makedirs(override, mode=0o700, exist_ok=True)
        return override
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/ChatEKLD")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA", ""), "ChatEKLD")
    else:
        base = os.path.expanduser("~/.local/share/ChatEKLD")
    # restrict permissions to owner-only so other local users cannot read stored configs, indexes, or feedback.
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base

BASE_DIR = _get_base_dir()
# Single source of truth for the application log path. launch.py (frozen + `python
# launch.py`) and app.py's dev `__main__` handler both write here, and the in-app
# log viewer (`GET /api/log/tail`) reads it, so all three agree on one location
# under the platform app-data dir.
LOG_FILE = os.path.join(BASE_DIR, "chatekld.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
FEEDBACK_FILE = os.path.join(BASE_DIR, "feedback.jsonl")
OLLAMA_PID_FILE = os.path.join(BASE_DIR, "ollama.pid")
REPORT_TYPES_FILE = os.path.join(BASE_DIR, "report_types.json")
OBSIDIAN_INDEX_DIR = os.path.join(BASE_DIR, "obsidian_storage")
OBSIDIAN_CACHE_DIR = os.path.join(BASE_DIR, "obsidian_cache")
DB_PATH = os.path.join(BASE_DIR, "uploads.db")

# Stable tiktoken BPE-vocab cache.  tiktoken otherwise caches to
# ``$TMPDIR/data-gym-cache``, which macOS evicts after a few days of non-use —
# breaking an *offline* reindex (the LlamaIndex SentenceSplitter needs the
# cl100k_base vocab).  Pinning the cache under BASE_DIR keeps it durable and
# lets the installer pre-populate exactly where the app will read.  launch.py
# exports this into ``TIKTOKEN_CACHE_DIR`` at startup (setdefault, so an
# explicit user override still wins).
TIKTOKEN_CACHE_DIR = os.path.join(BASE_DIR, "tiktoken_cache")

# Durable NLTK data dir.  LlamaIndex's SentenceSplitter eagerly loads NLTK
# punkt + stopwords (for PDF chunking and the markdown secondary-cap pass); if
# the data is absent it falls back to a network ``nltk.download`` — which breaks
# an OFFLINE first index on a fresh machine.  The installer pre-downloads punkt
# + stopwords here and launch.py exports this into ``NLTK_DATA`` at startup
# (setdefault, so a user override wins), mirroring the tiktoken-cache pin.
NLTK_DATA_DIR = os.path.join(BASE_DIR, "nltk_data")

# --- LLM & Embedding Defaults ---
DEFAULT_LLM = "llama3.2"
DEFAULT_EMBED = "nomic-embed-text"
DEFAULT_OCR_MODEL = "glm-ocr:latest"
DEFAULT_VISION_MODEL = "qwen3-vl:4b"

# --- Online Provider Defaults ---
# Per-provider model selections are persisted independently so toggling
# between providers does not lose the user's choice. Defaults pick a
# capable but mid-tier model so a user who plugs in a key without
# selecting a model does not accidentally spend on Opus / GPT-4.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"
DEFAULT_ONLINE_TIMEOUT_S = 60
DEFAULT_ONLINE_MAX_RETRIES = 3
DEFAULT_ONLINE_MAX_TOKENS = 4096

# --- Vision / OCR call bounds ---
# Indexing-time image-description (VisionManager) and scanned-PDF OCR
# (GLMOCRManager) calls are ALWAYS bounded so a runaway / stuck local model
# cannot stall a multi-hour indexing run. These are separate from
# ``local_request_timeout_s`` (chat-only): vision/OCR own their own timeout
# and are never unbounded (the knob's min is 5 s, there is no "0 = off").
DEFAULT_VISION_TIMEOUT_S = 120     # per-call HTTP timeout for vision + OCR
DEFAULT_VISION_MAX_TOKENS = 1536   # caps image-description generation length
DEFAULT_OCR_MAX_TOKENS = 4096      # full-page OCR can be longer than a caption
# Negative-result cooldown after a failed vision/OCR call (fast-fail window so
# per-image traffic can't hammer a missing model). Config-tunable via
# ``vision_failure_cooldown_s`` (0 disables — every image retries immediately);
# during indexing every image inside the window is silently skipped, so a
# shorter value trades retry traffic against fewer dropped images after a blip.
DEFAULT_VISION_FAILURE_COOLDOWN_S = 30
VISION_MAX_RETRIES = 0             # fail fast; an empty image retries next run
# VISION_IMAGE_MAX_SIDE MUST stay a multiple of 14: the downscaler rounds the
# resized longest side UP to the nearest 14 px (Qwen patch alignment), so a
# non-multiple would let the output exceed the cap by up to 13 px. 1568 = 112*14.
VISION_IMAGE_MAX_SIDE = 1568       # description path downscales above this px

# --- Single-paper (/api/summarise) local-generation stall floor ---
# That route streams synchronously in the request generator and has no
# consumer-side stall guard (unlike the vault/plainchat/deck SSE routes), so a
# connected-but-wedged local model could hang it until the browser aborts. When
# ``local_request_timeout_s`` is unset (0 ⇒ SDK default = unbounded), the
# summariser substitutes this per-read timeout (max gap between streamed
# tokens) so the worker thread can't park forever. A user-set positive
# ``local_request_timeout_s`` still wins.
PAPER_LOCAL_STALL_TIMEOUT_S = 120

# --- Markdown chunking secondary cap ---
# MarkdownNodeParser splits .md only at heading boundaries, so a single long
# section can blow past an embedding model's token limit (nomic-embed-text and
# EmbeddingGemma both cap ~2048) and be SILENTLY TRUNCATED — its tail never gets
# embedded. _chunk_raw_documents runs a second, conditional SentenceSplitter
# pass that sub-splits ONLY sections over this cap; every section under it passes
# through byte-for-byte (same node object → identical chunk id → no re-embed).
# 1024 leaves ~2x headroom under the 2048 hard limit to absorb tiktoken (cl100k,
# what SentenceSplitter counts with) vs Gemma SentencePiece tokenizer divergence.
# This is INTENTIONALLY a constant, not a config knob: it changes chunk ids, so
# editing it re-chunks oversized sections (a reindex) — the same pinned contract
# as the 512-token PDF chunk size. Guarded by test_chunker_params_pinned.
MD_MAX_CHUNK_TOKENS = 1024

# --- Provider Hosts ---
OLLAMA_HOST = "http://localhost:11434"
LM_STUDIO_HOST = "http://localhost:1234"

# --- Obsidian Settings ---
OBSIDIAN_INDEX_VERSION = "obsidian-markdown-v3"
OBSIDIAN_EXCLUDED_DIR_NAMES = frozenset({".git", ".obsidian", ".trash"})

# --- File Processing ---
PDF_LIMIT_SIZE_MB = 500
PDF_EXTRACT_TIMEOUT_S = 600
# Hard page ceiling shared by the vault indexer and the single-paper upload
# worker.  Both paths extract in EXTRACT_MAX_PAGES_PER_CALL (1000-page)
# ranges, so memory no longer scales with document size — this cap only
# guards against pathological files, since extraction *time* still scales
# linearly with page count (and the upload path additionally has the
# PDF_EXTRACT_TIMEOUT_S wall clock).
PDF_MAX_PAGES = 20_000

# --- Prompt Limits ---
# Single-paper and vault-chat custom system prompts share the same cap so
# the UI behaves consistently across both tabs.
SYSTEM_PROMPT_LIMIT = 4000
VAULT_SYSTEM_PROMPT_LIMIT = SYSTEM_PROMPT_LIMIT

# --- SSE consumer stall-guard timing (shared by every streaming chat route) ---
# The vault chat (single-shot + agent), Deck Generator, and Plain Chat SSE
# routes all run the same queue + daemon-worker + consumer skeleton. The
# consumer's per-event ``queue.get(timeout=...)`` is the backstop that frees the
# client when the worker goes silent. It waits SSE_STALL_MARGIN_S seconds LONGER
# than the effective stall base so the worker's OWN structured timeout/error
# event (an InfoEvent/ErrorEvent + the _DONE sentinel) fires first — preserving
# the "server emits a clean error" behaviour the frontend's longer fetch-abort
# also relies on. SSE_SINGLE_SHOT_FLOOR_S floors that base: the same consumer
# loop also serves a single-shot path (vault RAG / Plain Chat) whose ONLY time
# guard is this get, and a cold first token (retrieval + rerank + a cold model
# load) can legitimately take minutes — so the backstop never drops below this
# floor even when an agent/per-turn wall-clock cap is lowered to bound the agent
# path. The frontend fetch-abort mirrors this floor so the timeout chain stays
# ordered (inner deadline ≤ consumer ≤ frontend abort). Promoted here from the
# individual route modules so all three share ONE definition (no per-route drift
# and no cross-route private import).
SSE_STALL_MARGIN_S = 30
SSE_SINGLE_SHOT_FLOOR_S = 300

VAULT_MD_EXTS = frozenset({".md"})
VAULT_BINARY_EXTS = frozenset({".pdf"})  # .docx excluded by design
VAULT_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".svg", ".bmp", ".tiff", ".tif", ".heic",
    ".ico", ".img",
})

# --- Note Refactor (Phase 2 vault writes) ---
# In-vault folder (per refactor scope) that holds the small PNG thumbnails the
# archiver writes when it moves a full-res original out of the vault. It is
# auto-added to the user's vault_exclude_dirs on first thumbnail write so the
# indexer never describes thumbnails; the note→thumb embed is a note-relative
# markdown path, which the resolver's direct is_file probe resolves even though
# the dir is excluded.  Leading underscore keeps it visually grouped/sorted.
REFACTOR_THUMBS_DIRNAME = "_thumbs"
# Longest-side ceiling (px) for those thumbnails; overridable per-user via the
# refactor_thumb_max_side config key (clamped 96–1024 in api/routes/config.py).
DEFAULT_REFACTOR_THUMB_MAX_SIDE = 384

# --- Path Validation ---
EXACT_BLOCKED = frozenset([
    Path("/"),
    Path("/Users"),
    Path.home(),
])

SYSTEM_ROOTS = frozenset([
    Path("/etc"), Path("/var"), Path("/usr"),
    Path("/bin"), Path("/sbin"), Path("/lib"),
    Path("/System"), Path("/private"),
    Path("/proc"), Path("/sys"), Path("/dev"),
    Path("C:\\Windows"), Path("C:\\Program Files"),
])

# --- UI & Prompting ---
# Single-paper default system prompt. 2026-06 prompt audit changed three things:
#  * No per-section sentence count lives here — the per-mode user template owns
#    the length (CONCISE 1-2, DETAILED 1-3). A count in BOTH places contradicted
#    itself ("3-6" here vs "1-2"/"1-3" there) and confused small models.
#  * The domain ("medical and biomedical") is kept as a focus cue, but the
#    "expert research assistant" persona framing was dropped: current evidence
#    is that expert personas do not improve factual accuracy and can slightly
#    hurt it (they mainly shape tone/format).
#  * Phrased affirmatively where practical — small local models follow positive
#    instructions more reliably than long lists of prohibitions.
DEFAULT_SYSTEM_PROMPT = (
    "You summarise medical and biomedical literature. "
    "Produce dense, accurate summaries grounded strictly in the provided text. "
    "Use the section headers provided, written in UPPERCASE followed by a colon. "
    "Write each section as continuous plain prose at the length the task specifies; "
    "avoid bullet points, numbered lists, bold, and markdown. "
    "Base every statement on the text, and where the text does not address something, "
    "say so rather than speculating. "
    "Begin directly with the first section header, with no preamble, meta-commentary, or filler."
)

CONCISE_USER_TEMPLATE = (
    "Summarise the article below using ONLY these three sections:\n\n"
    "TYPE OF DOCUMENT: {document_type_line}\n"
    "MAIN FINDINGS:\n"
    "MAIN LIMITS:\n\n"
    "Limit each section to 1-2 sentences. "
    "Base every claim strictly on the text provided. "
    "Do not introduce information absent from the article.\n\n"
    "ARTICLE TEXT:\n{text}"
)

DETAILED_USER_TEMPLATE = (
    "Summarise the article below using ONLY these six sections:\n\n"
    "TYPE OF DOCUMENT: {document_type_line}\n"
    "OBJECTIVE:\n"
    "METHODS:\n"
    "MAIN FINDINGS:\n"
    "MAIN LIMITS:\n"
    "KEY EVIDENCE:\n\n"
    "Limit each section to 1-3 sentences. "
    # 2026-06 audit: an explicit full-document cue counters LLM "lead bias" —
    # the tendency to over-weight the abstract/introduction — on long papers.
    "Draw on the whole article — including the Methods and Results — not only the "
    "abstract and introduction. "
    "Base every claim strictly on the text provided. "
    "Do not speculate beyond what is written. "
    "Do not introduce information absent from the article.\n\n"
    "ARTICLE TEXT:\n{text}"
)

TARGET_AUDIENCE_OPTIONS = {
    "Researcher / Clinician": "",
    "Student / Trainee": " Explain technical terms in parentheses.",
    "General public": " Use simple, non-technical language.",
}

PROMPT_PRESETS = {
    "concise": {
        "description": "Short summary with the essential findings and limits.",
        "system": DEFAULT_SYSTEM_PROMPT,
        "user_template": CONCISE_USER_TEMPLATE,
    },
    "detailed": {
        "description": "Structured six-section analysis for closer reading.",
        "system": DEFAULT_SYSTEM_PROMPT,
        "user_template": DETAILED_USER_TEMPLATE,
    },
}

# 2026-06 prompt audit — convention applied to EVERY built-in report type
# below: the "You are a researcher specializing in X" identity opener was
# replaced with a "When summarising X, ..." scoping clause. Rationale: expert-
# persona framing does not improve factual accuracy, but the domain scope and
# the "pay special attention to / focus on ..." directives (which carry the real
# value) are preserved verbatim. Only the built-in defaults are defined here;
# user-saved/overridden report types in report_types.json are untouched.
DEFAULT_REPORT_TYPES = [
    {
        "id": "systematic_review",
        "name": "Systematic Review / Meta-analysis",
        "builtin": True,
        "system_prompt": (
            "When summarising systematic reviews and meta-analyses, produce accurate "
            "plain-text summaries and use the exact section labels provided. "
            "Pay special attention to the PICO framework, search strategy, inclusion and "
            "exclusion criteria, risk of bias, and heterogeneity."
        ),
    },
    {
        "id": "clinical_trial",
        "name": "Clinical Trial (RCT)",
        "builtin": True,
        "system_prompt": (
            "When summarising randomized controlled trials, produce accurate "
            "plain-text summaries and use the exact section labels provided. "
            "Pay special attention to randomization, blinding, allocation concealment, "
            "intention-to-treat analysis, and effect sizes."
        ),
    },
    {
        "id": "observational_study",
        "name": "Observational Study",
        "builtin": True,
        "system_prompt": (
            "When summarising observational epidemiology studies, produce accurate "
            "plain-text summaries and use the exact section labels provided. "
            "Pay special attention to study design, confounders, selection bias, and "
            "limits of causal inference."
        ),
    },
    {
        "id": "narrative_review",
        "name": "Narrative Review",
        "builtin": True,
        "system_prompt": (
            "When summarising a narrative review, produce accurate plain-text "
            "summaries and use the exact section labels provided. Focus on review "
            "scope, key themes, evidence synthesis, and gaps in the literature."
        ),
    },
    {
        "id": "opinion_letter",
        "name": "Opinion / Letter to the Editor",
        "builtin": True,
        "system_prompt": (
            "When summarising an opinion piece, editorial, or letter, produce accurate "
            "plain-text summaries and use the exact section labels provided. "
            "Focus on the central argument, supporting reasoning, the author's position, "
            "and any counterarguments addressed."
        ),
    },
    {
        "id": "case_report",
        "name": "Case Report / Case Series",
        "builtin": True,
        "system_prompt": (
            "When summarising a clinical case report or case series, produce accurate "
            "plain-text summaries and use the exact section labels provided. "
            "Focus on presentation, diagnostic workup, treatment, outcomes, and the "
            "clinical lesson or novelty."
        ),
    },
    {
        "id": "guideline",
        "name": "Guideline / Consensus Statement",
        "builtin": True,
        "system_prompt": (
            "When summarising a clinical guideline or consensus statement, produce accurate "
            "plain-text summaries and use the exact section labels provided. "
            "Focus on key recommendations, strength of evidence, target population, and "
            "notable changes from prior guidance."
        ),
    },
]
