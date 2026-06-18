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

VAULT_MD_EXTS = frozenset({".md"})
VAULT_BINARY_EXTS = frozenset({".pdf"})  # .docx excluded by design
VAULT_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".svg", ".bmp", ".tiff", ".tif", ".heic",
    ".ico", ".img",
})

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
DEFAULT_SYSTEM_PROMPT = (
    "You are a scientific research assistant specialising in medical and biomedical literature. "
    "Produce dense, accurate summaries grounded strictly in the provided text. "
    "Use the section headers provided, written in UPPERCASE followed by a colon. "
    "Write in plain prose. Each section: 3-6 concise sentences. "
    "Do not use bullet points, numbered lists, bold, or markdown formatting. "
    "Do not speculate beyond what is written in the text. "
    "No preamble, no meta-commentary, no filler phrases."
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

DEFAULT_REPORT_TYPES = [
    {
        "id": "systematic_review",
        "name": "Systematic Review / Meta-analysis",
        "builtin": True,
        "system_prompt": (
            "You are a researcher specializing in systematic reviews and meta-analyses. "
            "Produce accurate, plain-text summaries. Use exact section labels provided. "
            "Pay special attention to the PICO framework, search strategy, inclusion and "
            "exclusion criteria, risk of bias, and heterogeneity."
        ),
    },
    {
        "id": "clinical_trial",
        "name": "Clinical Trial (RCT)",
        "builtin": True,
        "system_prompt": (
            "You are a researcher specializing in randomized controlled trials. "
            "Produce accurate, plain-text summaries. Use exact section labels provided. "
            "Pay special attention to randomization, blinding, allocation concealment, "
            "intention-to-treat analysis, and effect sizes."
        ),
    },
    {
        "id": "observational_study",
        "name": "Observational Study",
        "builtin": True,
        "system_prompt": (
            "You are a researcher specializing in observational epidemiology. "
            "Produce accurate, plain-text summaries. Use exact section labels provided. "
            "Pay special attention to study design, confounders, selection bias, and "
            "limits of causal inference."
        ),
    },
    {
        "id": "narrative_review",
        "name": "Narrative Review",
        "builtin": True,
        "system_prompt": (
            "You are a researcher summarizing a narrative review. Produce accurate, "
            "plain-text summaries. Use exact section labels provided. Focus on review "
            "scope, key themes, evidence synthesis, and gaps in the literature."
        ),
    },
    {
        "id": "opinion_letter",
        "name": "Opinion / Letter to the Editor",
        "builtin": True,
        "system_prompt": (
            "You are a researcher summarizing an opinion piece, editorial, or letter. "
            "Produce accurate, plain-text summaries. Use exact section labels provided. "
            "Focus on the central argument, supporting reasoning, the author's position, "
            "and any counterarguments addressed."
        ),
    },
    {
        "id": "case_report",
        "name": "Case Report / Case Series",
        "builtin": True,
        "system_prompt": (
            "You are a researcher summarizing a clinical case report or case series. "
            "Produce accurate, plain-text summaries. Use exact section labels provided. "
            "Focus on presentation, diagnostic workup, treatment, outcomes, and the "
            "clinical lesson or novelty."
        ),
    },
    {
        "id": "guideline",
        "name": "Guideline / Consensus Statement",
        "builtin": True,
        "system_prompt": (
            "You are a researcher summarizing a clinical guideline or consensus statement. "
            "Produce accurate, plain-text summaries. Use exact section labels provided. "
            "Focus on key recommendations, strength of evidence, target population, and "
            "notable changes from prior guidance."
        ),
    },
]
