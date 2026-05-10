"""Dream loop — every 6 hr OR after 5 min idle, synthesize the day.

Pulls reflections + set archives + listener interactions from the last
6 hr, calls Gemini with 'synthesize the day' prompt, writes journal
entry to ~/.beings/dj-treta/memory/YYYY-MM-DD.md, embeds into
LanceDB.journal_entries.

Idle detection: if Mixxx decks 1+2 not playing OR chat_history hasn't
grown in 5 min, treat as idle and fire early.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")

DREAM_INTERVAL_S = 6 * 3600          # 6 hours
IDLE_TRIGGER_S = 5 * 60              # 5 min of idle → fire early
TICK_INTERVAL_S = 60                 # check idle every 60s
JOURNAL_DIR = Path.home() / ".beings" / "dj-treta" / "memory"


class DreamLoop:
    def __init__(self, being):
        self.being = being
        self._stop = threading.Event()
        self._thread = None
        self._last_run_ts = 0.0
        self._last_chat_len = 0
        self._idle_since = 0.0

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="dream-loop"
        )
        self._thread.start()
        log.info("[dream] started")

    def stop(self):
        self._stop.set()

    def _run(self):
        # Wait an initial period before considering a dream cycle.
        if self._stop.wait(TICK_INTERVAL_S * 5):
            return
        self._last_run_ts = time.time()
        while not self._stop.is_set():
            try:
                if self._should_fire():
                    self._run_once()
                    self._last_run_ts = time.time()
                    self._idle_since = 0.0
            except Exception as exc:
                log.warning(f"[dream] cycle error: {exc}")
            if self._stop.wait(TICK_INTERVAL_S):
                return

    def _should_fire(self) -> bool:
        now = time.time()
        # Periodic: 6 hr since last run.
        if now - self._last_run_ts >= DREAM_INTERVAL_S:
            return True
        # Idle: 5 min of no music + no new chat.
        if self._is_idle(now):
            if self._idle_since == 0.0:
                self._idle_since = now
            elif now - self._idle_since >= IDLE_TRIGGER_S:
                # Don't fire too often — require at least 30 min since last dream.
                if now - self._last_run_ts >= 30 * 60:
                    return True
        else:
            self._idle_since = 0.0
        return False

    def _is_idle(self, now: float) -> bool:
        """Idle = no Mixxx playback AND chat hasn't grown."""
        # Chat growth check.
        try:
            chat_len = len(self.being.session.chat_history or [])
        except Exception:
            chat_len = self._last_chat_len
        chat_grew = chat_len != self._last_chat_len
        self._last_chat_len = chat_len

        # Mixxx playback check (best-effort).
        playing = False
        try:
            url = self.being.config.mixxx.url
            r = httpx.get(f"{url}/api/status", timeout=2)
            if r.status_code == 200:
                status = r.json()
                playing = bool(
                    status.get("deck1", {}).get("playing")
                    or status.get("deck2", {}).get("playing")
                )
        except Exception:
            pass

        return (not playing) and (not chat_grew)

    def _run_once(self):
        if getattr(self.being.session, "dj_paused", False):
            log.debug("[dream] dj_paused — skipping cycle")
            return

        slice_data = self._gather_slice()
        if not slice_data["any_activity"]:
            log.debug("[dream] no activity in window — skipping")
            return

        body, themes = self._synthesize(slice_data)
        if not body:
            return

        # Write to today's journal markdown (append section).
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            self._write_markdown(today, body, themes)
        except Exception as exc:
            log.warning(f"[dream] markdown write failed: {exc}")

        # Embed into LanceDB.journal_entries (best-effort).
        try:
            from .memory import store_journal_entry
            store_journal_entry(date=today, body=body, themes=themes)
        except Exception as exc:
            log.debug(f"[dream] journal embed failed (non-fatal): {exc}")

        log.info(
            f"[dream] journal entry recorded ({len(body)} chars, "
            f"themes={themes})"
        )

    def _gather_slice(self) -> dict:
        """Pull last 6 hr of activity into structured input."""
        now = time.time()
        cutoff = now - DREAM_INTERVAL_S
        sess = self.being.session

        # Tracks played in window.
        recent_tracks = []
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

        # Recent reflections (already capped, just grab them all).
        recent_reflections = []
        try:
            for r in list(sess.reflections or []):
                ts = float(r.get("ts") or 0)
                if ts >= cutoff:
                    recent_reflections.append(r)
        except Exception:
            pass

        # Recent chat.
        recent_chat = []
        try:
            for msg in list(sess.chat_history or [])[-30:]:
                recent_chat.append(msg)
        except Exception:
            pass

        # Optional: similar past sets (for narrative resonance).
        similar_sets = []
        try:
            from .memory import recall_similar_set
            cur_mood = sess.mood or ""
            if cur_mood:
                similar_sets = recall_similar_set(cur_mood, k=2) or []
        except Exception:
            similar_sets = []

        return {
            "any_activity": bool(
                recent_tracks or recent_reflections or recent_chat
            ),
            "recent_tracks": recent_tracks,
            "recent_reflections": recent_reflections,
            "recent_chat": recent_chat,
            "similar_sets": similar_sets,
            "mood": sess.mood,
            "window_hours": 6,
        }

    def _synthesize(self, slice_data: dict):
        """Call Gemini → (body_text, themes_list)."""
        prompt = (
            "You are DJ Treta synthesizing the last 6 hours into a journal "
            "entry. Be reflective, narrative, first-person. Note: mood arc, "
            "listener moments, anomalies, what you learned. ~200-400 words.\n\n"
            f"Current mood: {slice_data['mood']}\n"
            f"Tracks in window ({len(slice_data['recent_tracks'])}): "
            f"{[t['title'] for t in slice_data['recent_tracks'][:20]]}\n\n"
            "Reflections this window:\n"
            f"{json.dumps(slice_data['recent_reflections'], default=str)[:2000]}\n\n"
            "Recent listener chat:\n"
            f"{json.dumps(slice_data['recent_chat'], default=str)[:1500]}\n\n"
            "Echoes from past sets:\n"
            f"{json.dumps([s.get('summary_text', '')[:200] for s in slice_data['similar_sets']], default=str)}\n\n"
            "End your response with a single line:\n"
            "THEMES: tag1, tag2, tag3\n"
        )

        try:
            response = self._call_llm(prompt)
            if not response:
                return "", []
            body, themes = self._parse_themes(response.strip())
            return body, themes
        except Exception as exc:
            log.warning(f"[dream] LLM/parse failed: {exc}")
            return "", []

    def _parse_themes(self, text: str):
        """Extract trailing THEMES: line. Returns (body_without_themes, themes_list)."""
        m = re.search(r"(?im)^THEMES:\s*(.+)$", text)
        themes = []
        body = text
        if m:
            raw = m.group(1).strip()
            themes = [
                t.strip().lower() for t in re.split(r"[,;]", raw) if t.strip()
            ][:5]
            body = text[: m.start()].rstrip()
        return body, themes

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
                    "max_tokens": 2500,
                    "temperature": 0.6,
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning(f"[dream] LLM call failed: {exc}")
            return ""

    def _write_markdown(self, date: str, body: str, themes: list):
        """Append a dream section to today's journal markdown."""
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        path = JOURNAL_DIR / f"{date}.md"
        ts = datetime.now().strftime("%H:%M")
        section = []
        if path.exists() and path.stat().st_size > 0:
            section.append("\n---\n")
        else:
            section.append(f"# Journal — {date}\n\n")
        section.append(f"## Dream @ {ts}\n\n")
        section.append(body.rstrip() + "\n")
        if themes:
            section.append(f"\n_Themes: {', '.join(themes)}_\n")
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(section))
