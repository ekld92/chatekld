"""Run selected integrity tests with disposable data and denied outbound sockets.

Invoke with the project Python 3.12 interpreter and an empty environment. No app
launcher, credentials file, provider service or installed application is used.
"""
import os
from pathlib import Path
import sys
import tempfile


def main():
    import ipaddress

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="papermind-integrity-home-") as home, \
            tempfile.TemporaryDirectory(prefix="papermind-integrity-data-") as base:
        # No inherited credentials, server discovery or production HOME.
        path = os.environ.get("PATH", "/usr/bin:/bin")
        os.environ.clear()
        os.environ.update(HOME=home, PATH=path, CHATEKLD_BASE_DIR=base,
                          PYTHON_DOTENV_DISABLED="1")

        def deny_network(event, args):
            if event == "socket.getaddrinfo":
                try:
                    ipaddress.ip_address(args[0])
                except ValueError:
                    raise OSError("Integrity fixture: DNS denied") from None
            if event in {"socket.connect", "socket.gethostbyname", "socket.gethostbyaddr",
                         "socket.sendto", "socket.sendmsg"}:
                raise OSError("Integrity fixture: outbound network denied")

        sys.addaudithook(deny_network)
        import pytest
        return pytest.main(sys.argv[1:] or ["-q", "tests/test_ui_index_integrity.py"])


if __name__ == "__main__":
    raise SystemExit(main())
