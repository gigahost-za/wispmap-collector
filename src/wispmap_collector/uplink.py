"""Uplink: collects results into batches, stores them on disk, sends them to the server.

Status handling follows spec doc 03 §3:
  2xx / 409  -> accepted (409 = server already has it) -> delete from the buffer
  413        -> batch too big -> split in half, retry
  400 / 422  -> server says the batch is malformed -> drop it (retrying can never succeed)
  401 / 403  -> token rejected -> pause 5 min, keep buffering
  426        -> collector too old -> pause 5 min, keep buffering
  429 / 5xx / network error -> exponential backoff 2 s ... 5 min with jitter, keep buffering
"""
import asyncio
import gzip
import json
import random
import time

import httpx
from uuid6 import uuid7

from .buffer import Buffer, Item
from .common import VERSION, log
from .runner import now_iso

BACKOFF_MIN_S, BACKOFF_MAX_S = 2.0, 300.0
PAUSE_S = 300.0                          # after 401/403/426
CATCHUP_GAP_S = 1.0                      # replay at most 1 old batch per second


class Uplink:
    def __init__(self, client, buffer: Buffer, clock=time.monotonic, sleep=asyncio.sleep, rand=random.random):
        self.client, self.buffer = client, buffer
        self.clock, self.sleep, self.rand = clock, sleep, rand
        self.started = clock()
        self.ping: list[dict] = []
        self.polls: list[dict] = []
        self.backoff_s = 0.0
        self.retry_at = 0.0               # don't send before this (monotonic) time
        self.problem: str | None = None   # current failure, logged once instead of every cycle
        self.rejected = 0
        self.on_config_version = None     # callback(int): the server reported a config version

    # ----- collecting -----
    def add(self, result: dict) -> None:
        """Sink for the ping/poll loops (replaces printing to stdout)."""
        kind = result.get("type")
        sample = {k: v for k, v in result.items() if k != "type"}
        if kind == "ping":
            self.ping.append(sample)
        elif kind == "poll":
            self.polls.append(sample)
        else:                              # e.g. collector_error from a loop: log it, don't send it
            log(f"{kind}: {sample.get('error', sample)}")

    def stats(self) -> dict:
        return {"version": VERSION, "uptime_s": int(self.clock() - self.started),
                "queue_depth": self.buffer.depth(), "dropped_batches": self.buffer.dropped}

    def _encode(self, ping: list, polls: list) -> tuple[str, bytes]:
        batch_id = str(uuid7())
        batch = {"schema": 1, "batch_id": batch_id, "sent_at": now_iso(),
                 "collector": self.stats(), "ping": ping, "polls": polls}
        return batch_id, gzip.compress(json.dumps(batch).encode())

    def flush(self) -> str | None:
        """Turn everything collected so far into one batch on disk. Returns its batch_id (None if empty)."""
        if not (self.ping or self.polls):
            return None
        batch_id, body = self._encode(self.ping, self.polls)
        self.buffer.put(batch_id, body)
        self.ping, self.polls = [], []
        dropped = self.buffer.enforce_caps()
        if dropped:
            log(f"buffer full: dropped {dropped} oldest batch(es)")
        return batch_id

    # ----- sending -----
    async def drain(self, budget_s: float) -> None:
        """Send the newest batch first (so the live map recovers at once after an outage),
        then work through the backlog oldest-first, at most 1 per second, until the budget is used."""
        if self.clock() < self.retry_at:
            return
        deadline = self.clock() + budget_s
        item = self.buffer.newest()
        if item is None or not await self._send(item):
            return
        while self.clock() + CATCHUP_GAP_S < deadline:
            item = self.buffer.oldest()
            if item is None:
                return
            await self.sleep(CATCHUP_GAP_S)
            if not await self._send(item):
                return

    async def _send(self, item: Item) -> bool:
        """Send one batch. True = carry on with the next one, False = stop for now."""
        try:
            r = await self.client.ingest(item.body)
        except httpx.HTTPError as exc:
            self._backoff(f"server unreachable ({type(exc).__name__}: {exc})")
            return False
        code = r.status_code
        if 200 <= code < 300 or code == 409:
            self.buffer.delete(item.id)
            self._recovered()
            if code == 202 and self.on_config_version:
                version = r.json().get("config_version")
                if version is not None:
                    self.on_config_version(version)
            return True
        if code == 413:
            self._split(item)
            return True
        if code in (400, 422):
            self.buffer.delete(item.id)
            self.rejected += 1
            log(f"server rejected batch {item.batch_id} (HTTP {code}), dropped: {r.text[:300]}")
            return True
        if code in (401, 403):
            self._pause(f"token rejected (HTTP {code}): check WISPMAP_TOKEN; still buffering")
            return False
        if code == 426:
            self._pause(f"server requires a newer collector: {r.text[:200]}; still buffering")
            return False
        self._backoff(f"server error HTTP {code}")        # 429, 5xx, anything unexpected
        return False

    def _split(self, item: Item) -> None:
        data = json.loads(gzip.decompress(item.body))
        samples = [("ping", s) for s in data["ping"]] + [("polls", s) for s in data["polls"]]
        self.buffer.delete(item.id)
        if len(samples) <= 1:
            self.rejected += 1
            log(f"batch {item.batch_id} is too large even with one sample: dropped")
            return
        half = len(samples) // 2
        for part in (samples[:half], samples[half:]):
            batch_id, body = self._encode([s for k, s in part if k == "ping"], [s for k, s in part if k == "polls"])
            self.buffer.put(batch_id, body, created=item.created)       # keeps its age for the 48 h cap
        log(f"batch {item.batch_id} too large (HTTP 413): split into two")

    # ----- failure state -----
    def _backoff(self, why: str) -> None:
        self.backoff_s = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, self.backoff_s * 2))
        delay = self.backoff_s * (0.5 + self.rand())      # jitter: 1000 collectors don't retry in lockstep
        self.retry_at = self.clock() + min(delay, BACKOFF_MAX_S)
        self._report(why)

    def _pause(self, why: str) -> None:
        self.retry_at = self.clock() + PAUSE_S
        self._report(why)

    def _report(self, why: str) -> None:
        if why != self.problem:
            log(why)
            self.problem = why

    def _recovered(self) -> None:
        if self.problem:
            log(f"server reachable again; {self.buffer.depth()} batch(es) still queued")
        self.problem, self.backoff_s, self.retry_at = None, 0.0, 0.0

    # ----- loop -----
    async def run(self, push_s) -> None:
        """Every push_s(): close a batch, then send for up to push_s() - 1 seconds."""
        while True:
            started = self.clock()
            try:
                self.flush()
                await self.drain(budget_s=push_s() - 1)
            except Exception as exc:                        # never let the uplink die
                log(f"uplink error: {type(exc).__name__}: {exc}")
            await self.sleep(max(0.0, push_s() - (self.clock() - started)))
