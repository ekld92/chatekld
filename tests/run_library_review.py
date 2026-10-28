"""Run focused Library fixtures without real app data, credentials or networking.

Usage: python tests/run_library_review.py -q tests/test_daily_library.py [...]
The process-lifetime audit hook also covers sockets created by SDKs and threads.
Provider/Zotero/embedding doubles still belong at each test's external boundary.
"""
import os
import sys
import tempfile
import ipaddress
from pathlib import Path
os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
os.environ.pop('CHERQAIN_BASE_DIR', None)
os.environ['CHATEKLD_BASE_DIR'] = tempfile.mkdtemp(prefix='cherqain-review-')
for key in tuple(os.environ):
    if key.endswith(('_API_KEY', '_TOKEN', '_SECRET')) or key in {'API_KEY', 'TOKEN'}:
        os.environ.pop(key, None)
def guard(event, args):
    if event in {'socket.connect','socket.gethostbyname','socket.gethostbyaddr','socket.sendto','socket.sendmsg'}:
        raise OSError('Review validation: outbound network denied')
    if event == 'socket.getaddrinfo':
        try:
            ipaddress.ip_address(args[0])
        except ValueError:
            raise OSError('Review validation: DNS denied') from None
    if event == 'open' and isinstance(args[0], (str, bytes)) and Path(os.fsdecode(args[0])).name == '.env':
        raise OSError('Review validation: live .env denied')
sys.addaudithook(guard)
sys.path.insert(0, os.getcwd())
import pytest  # noqa: E402 — isolation must precede application/test imports
raise SystemExit(pytest.main(sys.argv[1:]))
