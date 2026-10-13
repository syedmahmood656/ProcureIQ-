"""
Phase 8 — small thread-safe TTL + LRU cache for finished answers.

Why cache at all: one multi-hop question costs several LLM calls (tens of seconds to minutes on a
free tier), and the same question is asked again and again (demos, dashboards, retries by an
impatient client). Why only a short TTL: the answers depend on ERP data and contract text that can
change, so entries expire after PROCUREIQ_CACHE_TTL_S (default 10 minutes) and a client can force
a fresh run with {"refresh": true}.

Pure Python, injectable clock -> unit-testable without sleeping.
"""
import threading
import time
from collections import OrderedDict


def make_key(question: str, max_retries: int) -> str:
    """Normalise so 'Top 5 vendors?' and '  top   5 VENDORS? ' share one cache entry."""
    return f"{max_retries}|{' '.join(question.split()).casefold()}"


class TTLCache:
    def __init__(self, max_entries: int = 256, ttl_s: float = 600.0, clock=time.monotonic):
        self.max_entries, self.ttl_s, self._clock = max_entries, ttl_s, clock
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if item is None or self._clock() - item[0] > self.ttl_s:
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)  # mark as recently used
            self.hits += 1
            return item[1]

    def set(self, key: str, value) -> None:
        if self.max_entries <= 0 or self.ttl_s <= 0:  # caching disabled
            return
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)  # evict least recently used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
