#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append-only JSONL item store + cursor state for the flash-news skill.

Design (from the 2026-08-31 research):
- Ingestion cron appends every discovered item to store.jsonl keyed by `id`.
- Digest runs read items in [window_start, window_end], excluding already
  delivered ids; on success they advance last_digest_ts and mark items
  delivered. Effective window = max(last_digest_ts, now - window_hours),
  so a skipped Sunday automatically widens Monday's window without gaps
  and without repeats.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path
from typing import Any, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def stable_id(*parts: Any) -> str:
    return md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


class FlashStore:
    def __init__(self, cache_dir: Path, delivered_cap: int = 5000):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.store_file = self.cache_dir / "store.jsonl"
        self.state_file = self.cache_dir / "state.json"
        self.delivered_cap = int(delivered_cap)

    # ---------------- state ----------------

    def load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    # ---------------- store ----------------

    def append(self, items: list[dict[str, Any]]) -> int:
        """Append items (dicts must carry id). Returns count of NEW lines."""
        if not items:
            return 0
        known = self._known_ids()
        fresh = [it for it in items if it.get("id") and it["id"] not in known]
        if fresh:
            with self.store_file.open("a", encoding="utf-8") as fh:
                for it in fresh:
                    fh.write(json.dumps(it, ensure_ascii=False) + "\n")
        return len(fresh)

    def _known_ids(self) -> set[str]:
        ids: set[str] = set()
        if not self.store_file.exists():
            return ids
        with self.store_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    ids.add(json.loads(line).get("id") or "")
                except json.JSONDecodeError:
                    continue
        return ids

    def read_all(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.store_file.exists():
            return items
        with self.store_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items

    def read_window(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        exclude_ids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        exclude_ids = exclude_ids or set()
        out: list[dict[str, Any]] = []
        for it in self.read_all():
            if it.get("id") in exclude_ids:
                continue
            ts = parse_iso(it.get("ts") or "")
            if ts is None:
                continue
            if window_start <= ts <= window_end:
                out.append(it)
        out.sort(key=lambda x: x.get("ts") or "")
        return out

    # ---------------- window / checkpoints ----------------

    def resolve_window(
        self, *, window_hours: float, overlap_minutes: float = 5.0
    ) -> tuple[datetime, datetime]:
        """[max(last_digest - overlap, now - window_hours), now] — cursor with a floor."""
        now = utc_now()
        state = self.load_state()
        last = parse_iso(state.get("last_digest_ts") or "")
        floor = now - timedelta(hours=window_hours)
        start = floor
        if last is not None:
            start = min(last - timedelta(minutes=overlap_minutes), floor)
        return start, now

    def commit_digest(self, *, window_end: datetime, item_ids: list[str]) -> None:
        state = self.load_state()
        delivered: list[str] = list(state.get("delivered_ids") or [])
        seen = set(delivered)
        for i in item_ids:
            if i not in seen:
                delivered.append(i)
                seen.add(i)
        state["delivered_ids"] = delivered[-self.delivered_cap :]
        state["last_digest_ts"] = iso(window_end)
        state["last_digest_at"] = iso(utc_now())
        state["last_digest_count"] = len(item_ids)
        self.save_state(state)
