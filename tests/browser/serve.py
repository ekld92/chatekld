"""Boot the real app shell on loopback for the Playwright matrix.

Deliberately the SAME entry point the product uses (``core.wsgi.serve_loopback``)
rather than ``app.run``: the response headers under test — the CSP above all —
are attached by ``create_app``'s ``after_request`` hook, so anything that starts
the app differently would be testing a different stack. ``CHATEKLD_BASE_DIR`` is
set by the caller to a throwaway directory, matching the hermeticity contract of
the root ``conftest.py``: no user vault, config, or index is touched.
"""

from __future__ import annotations

import sys
import os
import ipaddress


def isolate_network():
    """Deny outbound sockets/DNS before importing any application module.

    The server may bind/listen/accept browser requests, but cannot probe local
    model runners or reach remote providers. Python audit hooks also cover
    connect_ex and SDK-created sockets, not just requests/urllib wrappers.
    This hook is intentionally process-lifetime and test-only.
    """
    for name in tuple(os.environ):
        if name.endswith(('_API_KEY', '_TOKEN', '_SECRET')) or name in {'API_KEY', 'TOKEN'}:
            os.environ.pop(name, None)

    def deny_outbound(event, args):
        if event == 'socket.getaddrinfo':
            # Waitress resolves its numeric bind address; this performs no DNS.
            try:
                ipaddress.ip_address(args[0])
            except ValueError:
                raise OSError('Browser fixture: outbound network disabled (no real providers).') from None
            return
        if event in {'socket.connect', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg'}:
            raise OSError('Browser fixture: outbound network disabled (no real providers).')
        # An exec'd child does not inherit Python audit hooks. Refuse external
        # helpers rather than letting them bypass the fixture's network guard.
        # multiprocessing spawn uses its internal fork_exec path and reimports
        # this entry point before running the PDF worker (covered separately).
        if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'}:
            raise OSError('Browser fixture: external child processes disabled.')

    sys.addaudithook(deny_outbound)


# Spawned multiprocessing workers re-import this entry point as __mp_main__.
# Install there too, before any PDF/OCR worker could reach a provider.
isolate_network()

if __name__ == "__main__":
    from app import create_app
    from core.wsgi import serve_loopback

    application = create_app()
    if os.environ.get('PAPERMIND_PDF_FIXTURE') == '1':
        from pdf_fixture import install
        install()
    serve_loopback(application, host="127.0.0.1", port=int(sys.argv[1]))
