# PDF Extraction, Vision/OCR & Attachment Services (`services/`)

`services/` and `rag/summarizer.py` handle Single-Paper upload processing (`pdf_service.py`), subprocess PDF extraction (`pdf_extractor.py`), OCR and vision model managers (`vision.py`), Plain Chat attachment extraction and persistence (`attachment_service.py`), and the local vision concurrency gate (`local_model.py`).

## Single-Paper Summarisation (`Read` Tab)

1. **Upload & Subprocess Extraction (`services/pdf_service.py`, `pdf_extractor.py`):**
   - Uploads stream to a temporary file (500 MB hard cap) and extract in a child process bounded by `PDF_EXTRACT_TIMEOUT_S` (600 s) and `PDF_MAX_PAGES` (20,000 pages; 1,000 pages per call).
   - Scanned pages trigger `GLMOCRManager` fallback up to `_OCR_MAX_PAGES` (100 pages on interactive uploads; 1,000 pages per range during vault indexing).
   - Trailing reference/bibliography sections are automatically stripped before storing the text in `uploads.db`.
2. **Prompt Presets & Controls (`rag/summarizer.py`):**
   - **Summary length presets:** `concise` (document type, main findings, main limits) and `detailed` (adds objective, methods, key evidence).
   - **Document-type system prompts:** Systematic Review, RCT, Observational Study, Narrative Review, Opinion/Letter, Case Report, Guideline.
   - **Per-request overrides:** `system_prompt`, `focus_question`, `audience`, `language`, plus generation knobs (`temperature`, `num_ctx`, `max_tokens`, `top_p`, `repeat_penalty`), resolved from `request body → persisted paper_* defaults → hard clamp`.
   - Extracted paper text is wrapped in the untrusted-source guard (`wrap_untrusted`).
3. **Context Budgeting & Map-Reduce (`core/llm/budget.py`, `rag/summarizer.py`):**
   - Documents that fit the active context window stream in a single pass.
   - Documents exceeding the window use **map-reduce**: split into window-sized parts (capped at **64 parts** per run with an explicit coverage notice if exceeded), condensed into part notes with UI heartbeats, and synthesized in a single-level reduce pass.
   - An empty map-part response or a final summary with no non-whitespace text aborts as a workflow error (emitting usage before the terminal SSE `error` frame) rather than silently omitting sections.
4. **Rendering & Lifecycle (`static/js/summarizer.js`):**
   - Uses `ui.js::makeAnswerRenderer` (vendored `marked` + `sanitiseHtml`), storing raw Markdown separately for copy and `.txt`/`.md` export to `~/Downloads`.
   - Single-controller ownership per pane; `Stop` or error preserves received partial text for inspection and export. No automatic replay on reload.

## Plain Chat & Server-Side Attachments (`api/routes/chat_attachment.py`, `api/routes/plainchat.py`, `services/attachment_service.py`, `services/local_model.py`)

Plain Chat provides a stateless, RAG-free multi-turn conversation (browser sends the last 20 `{role, content}` turns) over `core.llm.chat.stream_chat_messages`.

- **Attachment Limits & Budgets:**
  - Accepted formats: `.pdf`, `.png`, `.jpg`, `.md`, `.txt` (≤64 MB per upload via its own request envelope; `.svg`/`.heic` refused by extension).
  - Per-message limit: **4 submitted attachments** (`CHAT_ATTACHMENT_MAX_PER_MESSAGE`; same-role turn merges retain both sides' IDs without re-capping).
  - Per-file character cap: `chat_attachment_max_chars` (default **120,000** chars, range `1,000–400,000`). Both page and character truncation are disclosed on the UI chip and in the prompt wrapper.
  - Whole-conversation budget: **240,000 characters** (`CHAT_ATTACHMENT_REQUEST_MAX_CHARS`) of extracted text across the 20-turn window. Uploads or requests that exceed the budget are refused with a 400 naming the figures rather than silently trimmed.
- **Extraction Pipeline at Attach Time (`services/attachment_service.py`):**
  - Runs on `api/bounded.py::run_bounded_action` (one live upload per route+kind; 429 on concurrent upload of the same kind, 504 on deadline).
  - **PDFs:** Page-bounded extraction via `pdf_extractor.py` derived from `chat_attachment_max_chars` (~151 pages at the 120,000-char default, floor 8) returning `(text, meta)` so sparse PDFs cut by page count still disclose truncation.
  - **Images (`.png`, `.jpg`):** Described via the configured **local** vision model (`Ollama` or `LM Studio`) under `services/local_model.py::LOCAL_MODEL_LOCK` (returns 503 if busy), even when the active chat provider is online.
  - **Markdown / Text (`.md`, `.txt`):** Read directly as UTF-8 text. The `_MIN_USEFUL_CHARS` (`16`) minimum applies only to extracted kinds (`pdf`/`image`), never to `.md`/`.txt`.
- **Security & Retention:**
  - All attached text is wrapped in a nonce'd untrusted-content guard (`wrap_untrusted(source=...)` with sanitised filename preamble) before entering the prompt, and `attachment_ids` are stripped before provider dispatch.
  - Extracted text rows live in `uploads.db` (`chat_attachments` table), expire after 24 hours (`CHAT_ATTACHMENT_TTL_S`), and are listed with character counts in `.md`/`.txt` exports.

## OCR & Vision Managers (`services/vision.py`)

- **`GLMOCRManager`:** Transcribes scanned PDF pages using `ocr_provider` / `ocr_model` (default `ollama` / `glm-ocr:latest`, cap `ocr_max_tokens=4096`). Disables reasoning explicitly (`reasoning: off`, `store: false` on LM Studio `/api/v1/chat`; `think=False` on Ollama). If a page overflows the model's context limit, it retries once with a downscaled image.
- **`VisionManager`:** Describes note-referenced vault images and Plain Chat image attachments using `vision_provider` / `vision_model` (default `ollama` / `qwen3-vl:4b`, cap `vision_max_tokens=1536`), pre-emptively downscaling images above `VISION_IMAGE_MAX_SIDE` (`1568` px, multiple of 14). Decodes `.heic` via bundled `pillow-heif`; skips vector formats (`.svg`, `.ico`) or images >20 MB with a warning (`skipped_image_count`).
- **Always-On Call Bounds:** Both managers enforce `vision_timeout_s` (default 120 s, range `5–600`, `max_retries=0`) and a failure cooldown (`vision_failure_cooldown_s`, default 30 s, `0` disables) so a wedged vision runner cannot stall a multi-hour indexing run.
