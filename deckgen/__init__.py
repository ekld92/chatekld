"""deckgen — external orchestrator that turns a topic + the Obsidian vault into a Beamer .tex deck.

This package is intentionally DECOUPLED from the ChatEKLD application: it talks to
the running app only over its local HTTP API and never imports any app module. Do
not add imports of ``app``/``api``/``core``/``rag``/``services``/``audit`` here — the
decoupling guard (``grep -rn "deckgen" app.py api/ core/ rag/ services/ audit/``)
must stay empty in the other direction too.

Run it as a module from the repository root:

    python -m deckgen --topic "Schizophrenia" --instructions @notes.txt \
        --provider ollama --model qwen2.5 --port 5050 --out schizophrenia.tex
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
