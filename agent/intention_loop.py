"""Intention loop — weekly meta-meta layer.

Runs every Sunday at 23:00 local. Reviews journal entries from the
week, listener_profile changes, set archive summaries. Synthesizes:
  - Taste shifts noticed in Manish this week
  - My own taste shifts this week
  - What I want to try next week

Writes to ~/.beings/dj-treta/INTENTIONS.md (append weekly section).
No LanceDB embed — intentions are rare and local.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")

INTENTIONS_FILE = Path.home() / ".beings" / "dj-treta" / "INTENTIONS.md"
WEEKLY_HOUR = 23   # 23:00 local
WEEKLY_DOW = 6     # Sunday (Mon=0..Sun=6)


class IntentionLoop:
    def __init__(self, being):
        self.being = being
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="intention-loop"
        )
        self._thread.start()
        log.info("[intention] started")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            sleep_s = self._seconds_until_next_sunday_23()
            log.info(
                f"[intention] next fire in {sleep_s/3600:.1f} h "
                f"({datetime.now() + timedelta(seconds=sleep_s):%Y-%m-%d %H:%M})"
            )
            if self._stop.wait(sleep_s):
                return
            try:
                self._run_once()
            except Exception as exc:
                log.warning(f"[intention] cycle error: {exc}")
            # Sleep at least a minute so we don't double-fire in the same minute.
            if self._stop.wait(90):
                return

    def _seconds_until_next_sunday_23(self) -> float:
        now = datetime.now()
        days_ahead = (WEEKLY_DOW - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=WEEKLY_HOUR, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target = target + timedelta(days=7)
        delta = (target - now).total_seconds()
        return max(60.0, delta)

    def _run_once(self):
        if getattr(self.being.session, "dj_paused", False):
            log.debug("[intention] dj_paused — skipping cycle")
            return

        slice_data = self._gather_week()
        if not slice_data["any_activity"]:
            log.info("[intention] no activity this week — skipping")
            return

        sections = self._synthesize(slice_data)
        if not sections:
            return

        try:
            self._write_markdown(sections, slice_data["week_start"])
            log.info(
                f"[intention] wrote weekly intention "
                f"(week of {slice_data['week_start']})"
            )
        except Exception as exc:
            log.warning(f"[intention] markdown write failed: {exc}")

    def _gather_week(self) -> dict:
        """Pull last 7 days of journal entries + tracks + listener profile."""
        now = time.time()
        cutoff = now - 7 * 86400

        today = datetime.now().date()
        week_start = (today - timedelta(days=7)).isoformat()
        week_end = today.isoformat()

        # Journal entries this week.
        journal_entries = []
        try:
            from .memory import recall_journal
            journal_entries = recall_journal(
                date_range=(week_start, week_end), k=30
            ) or []
        except Exception as exc:
            log.debug(f"[intention] recall_journal failed: {exc}")

        # Tracks played this week.
        recent_tracks = []
        sess = self.being.session
        try:
            for t in (sess.tracks_played or []):
                ts = t.get("loaded_at") or t.get("time") or 0
                try:
                    ts = float(ts)
                except Exception:
                    ts = 0.0
                if ts >= cutoff:
                    recent_tracks.append({
                        "title": t.get("title", "?"),
                        "artist": t.get("artist", ""),
                        "mood": t.get("mood", ""),
                    })
        except Exception:
            pass

        # Listener profile (best-effort — schema varies; just dump what's there).
        listener_profile = {}
        try:
            from . import db as _db
            if hasattr(_db, "get_listener_profile_all"):
                listener_profile = _db.get_listener_profile_all() or {}
        except Exception:
            pass

        # Recent reflections (capped on session, but useful context).
        reflections = []
        try:
            for r in list(sess.reflections or []):
                ts = float(r.get("ts") or 0)
                if ts >= cutoff:
                    reflections.append(r)
        except Exception:
            pass

        return {
            "any_activity": bool(
                journal_entries or recent_tracks or reflections
            ),
            "week_start": week_start,
            "week_end": week_end,
            "journal_entries": journal_entries,
            "recent_tracks": recent_tracks,
            "listener_profile": listener_profile,
            "reflections": reflections,
        }

    def _synthesize(self, slice_data: dict):
        """Call Gemini → dict {manish_taste, my_taste, next_week}."""
        # Compress journal bodies for prompt budget.
        journal_blob = []
        for j in slice_data["journal_entries"][:10]:
            body = j.get("body", "") if isinstance(j, dict) else str(j)
            journal_blob.append(body[:600])

        prompt = (
            "You are DJ Treta writing your weekly intention note. Reflect on the "
            "past 7 days and think forward.\n\n"
            "Output STRICT JSON with these keys (no markdown fences):\n"
            "{\n"
            '  "manish_taste": "string",   // 2-4 sentences on what Manish leaned toward this week\n'
            '  "my_taste":     "string",   // 2-4 sentences on your own taste evolving\n'
            '  "next_week":    "string"    // 3-5 sentences — what you want to try, why, how\n'
            "}\n\n"
            f"Window: {slice_data['week_start']} → {slice_data['week_end']}\n"
            f"Tracks played this week ({len(slice_data['recent_tracks'])}): "
            f"{[t['title'] for t in slice_data['recent_tracks'][:30]]}\n\n"
            "Journal excerpts:\n"
            f"{json.dumps(journal_blob, ensure_ascii=False)[:4000]}\n\n"
            "Reflections this week (compressed):\n"
            f"{json.dumps([r.get('next_intent', '') for r in slice_data['reflections']], ensure_ascii=False)[:1500]}\n\n"
            "Listener profile snapshot:\n"
            f"{json.dumps(slice_data['listener_profile'], default=str)[:1500]}\n\n"
            "Respond with JSON only."
        )

        try:
            raw = self._call_llm(prompt)
            if not raw:
                return None
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            return json.loads(raw)
        except Exception as exc:
            log.warning(f"[intention] LLM/parse failed: {exc}")
            return None

    def _call_llm(self, prompt: str) -> str:
        try:
            api_key = os.environ.get("DJTRETA_LLM_API_KEY", "")
            base = os.environ.get(
                "DJTRETA_LLM_API_BASE", "http://localhost:4000"
            )
            r = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "gemini-3.1-pro",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 3000,
                    "temperature": 0.5,
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning(f"[intention] LLM call failed: {exc}")
            return ""

    def _write_markdown(self, sections: dict, week_start: str):
        INTENTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        is_new = (
            not INTENTIONS_FILE.exists()
            or INTENTIONS_FILE.stat().st_size == 0
        )
        out = []
        if is_new:
            out.append("# DJ Treta — Weekly Intentions\n\n")
        out.append(f"\n## Week of {week_start}\n\n")
        out.append("### Manish's taste shifts\n\n")
        out.append((sections.get("manish_taste") or "_(none observed)_").strip() + "\n\n")
        out.append("### My taste shifts\n\n")
        out.append((sections.get("my_taste") or "_(none observed)_").strip() + "\n\n")
        out.append("### What I want to try next week\n\n")
        out.append((sections.get("next_week") or "_(no plan yet)_").strip() + "\n")
        with INTENTIONS_FILE.open("a", encoding="utf-8") as f:
            f.write("".join(out))
