"""CHERQAiN branding and backward-compatible environment lookup."""
import os

APP_NAME = "CHERQAiN"

def env_value(name: str, default=None):
    """Prefer CHERQAIN_<suffix>; retain the exact legacy variable as fallback.

    An explicitly set empty value wins, just as with os.environ.get.
    """
    suffix = name.removeprefix("CHATEKLD_").removeprefix("PAPERMIND_")
    return os.environ.get("CHERQAIN_" + suffix, os.environ.get(name, default))
