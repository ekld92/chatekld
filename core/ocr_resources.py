"""Read-only macOS memory guard for explicitly invoked page extraction."""
from __future__ import annotations

import re
import subprocess
import sys
import time


class MemoryGuard:
    """System swap is not attributable to this worker. Preserve sampled evidence."""
    MAX_SAMPLES = 256

    def __init__(self, *, allow_initial_pressure: bool = False) -> None:
        self.samples: list[dict] = []
        self.sample_count = 0
        self._last = float('-inf')
        self._baseline: float | None = None
        self._critical_since: float | None = None
        self.reason = ''
        # Retained for old callers/receipts. Warning pressure is now admitted
        # for every bounded extraction; this flag never bypasses critical stops.
        self.allow_initial_pressure = allow_initial_pressure

    def __call__(self) -> str:
        now = time.monotonic()
        if self.reason or now - self._last < 2:
            return self.reason
        self._last = now
        if sys.platform != 'darwin':
            self.reason = 'memory_monitor_unavailable'
            return self.reason
        try:
            result = subprocess.run(['/usr/sbin/sysctl', 'vm.swapusage',
                                     'kern.memorystatus_vm_pressure_level'],
                                    capture_output=True, text=True, timeout=2, check=True)
            swap = re.search(r'used = ([\d.]+)M', result.stdout)
            pressure = re.search(r'pressure_level: (\d+)', result.stdout)
            if not swap or not pressure:
                raise ValueError('Missing memory metrics')
            used = float(swap.group(1))
            level = int(pressure.group(1))
            if level not in (0, 1, 2, 4):
                raise ValueError('Unknown memory pressure level')
            self.samples.append({'monotonic_s': now, 'swap_used_mib': used, 'pressure': level})
            self.sample_count += 1
            # Preserve the initial baseline and the most recent evidence. A
            # batch may run for 24 hours; diagnostics must not grow with it.
            if len(self.samples) > self.MAX_SAMPLES:
                del self.samples[1]
            if self._baseline is None:
                self._baseline = used
                if level not in (0, 1, 2):
                    self.reason = 'initial_memory_pressure'
            if used - self._baseline > 512:
                self.reason = 'swap_growth_over_512_mib'
            if level == 4:
                if self._critical_since is None:
                    self._critical_since = now
                if now - self._critical_since >= 10:
                    self.reason = 'critical_memory_pressure'
            else:
                self._critical_since = None
        except (OSError, ValueError, subprocess.SubprocessError):
            self.reason = 'memory_monitor_unavailable'
        return self.reason


def process_rss_bytes(pid: int) -> int | None:
    """Sample one owned child, not system-wide memory; short peaks may be missed."""
    try:
        result = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(pid)],
                                capture_output=True, text=True, timeout=1, check=True)
        return int(result.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def extraction_stop_diagnostic(reason: str) -> dict:
    """Project a runtime stop without labelling the source or provider defective."""
    messages = {
        'initial_memory_pressure': 'Document preparation stopped because macOS reported memory pressure before processing.',
        'swap_growth_over_512_mib': 'Extraction stopped because system swap grew beyond the memory safety limit.',
        'critical_memory_pressure': 'Extraction stopped because macOS reported sustained critical memory pressure.',
        'memory_monitor_unavailable': 'Extraction stopped because memory safety monitoring is unavailable.',
        'text_memory_budget': 'Extraction stopped because the retained text exceeded its memory budget.',
        'budget': 'Extraction stopped because its processing time limit was reached.',
        'cancelled': 'Extraction was interrupted before all pages were processed.',
    }
    if not reason:
        return {}
    return {
        'stop_reason': reason,
        'failure_scope': 'global',
        'error_detail': messages.get(reason, 'Extraction stopped before completing the document.'),
        'next_step': (
            'Reduce other memory-heavy work, then review a retry of unfinished preparation. '
            'Completed results are retained. This is a resource stop, not a defective document.'
            if reason in {'initial_memory_pressure', 'swap_growth_over_512_mib', 'critical_memory_pressure'} else
            'Restore memory monitoring before retrying; do not bypass the safety guard.'
            if reason == 'memory_monitor_unavailable' else
            'Inspect the saved result, then review an explicit bounded retry. No automatic retry or index update was started.'
        ),
    }
