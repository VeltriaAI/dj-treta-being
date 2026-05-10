"""Reflection loop — every 15 min Treta reviews what just happened.

Reads last 15 min slice of session activity, calls Gemini with a
'reflect on the last 15 min' prompt, parses structured output
({went_well, to_improve, next_intent, mood_drift, listener_engagement_delta}),
appends to session.reflections (capped FIFO at 20), embeds the synthesized
text into LanceDB.treta_thoughts.

Runs in its own thread, started from main.py:run().
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx

log = logging.getLogger("dj-treta")

REFLECTION_INTERVAL_S = 15 * 60       # 15 min
MAX_REFLECTIONS_RETAINED = 20


class ReflectionLoop:
    def __init__(self, being):
        """being: the DJTretaBeing instance. We use being.session for inputs."""
        self.being = being
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="reflection-loop"
        )
        self._thread.start()
        log.info("[reflection] started")

    def stop(self):
        self._stop.set()

    def _run(self):
        # Wait one full interval before first reflection (no point at t=0).
        if self._stop.wait(REFLECTION_INTERVAL_S):
            return
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as exc:
                log.warning(f"[reflection] cycle error: {exc}")
            if self._stop.wait(REFLECTION_INTERVAL_S):
                return

    def _run_once(self):
        # Skip if subagent paused — Treta is taking the wheel.
        if getattr(self.being.session, "dj_paused", False):
            log.debug("[reflection] dj_paused — skipping cycle")
            return

        slice_data = self._gather_slice()
        if not slice_data["any_activity"]:
            log.debug("[reflection] no activity in last 15 min — skipping")
            return

        synthesis = self._synthesize(slice_data)
        if not synthesis:
            return

        self._record(synthesis)

        # Embed into LanceDB treta_thoughts (best-effort).
        try:
            from .memory import store_thought
            text = (
                f"Reflection at {time.strftime('%Y-%m-%d %H:%M')}: "
                f"went well: {', '.join(synthesis.get('went_well', []) or [])}. "
                f"to improve: {', '.join(synthesis.get('to_improve', []) or [])}. "
                f"next intent: {synthesis.get('next_intent', '') or ''}"
            )
            store_thought(
                ts=time.time(),
                agent_id="treta:reflection",
                decision_text=text,
                context={
                    "mood": self.being.session.mood,
                    "synthesis": synthesis,
                },
            )
        except Exception as exc:
            log.debug(f"[reflection] embed failed (non-fatal): {exc}")

    def _gather_slice(self) -> dict:
        """Pull last 15 min of activity into structured input for Gemini."""
        now = time.time()
        cutoff = now - REFLECTION_INTERVAL_S
        sess = self.being.session

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
                    "bpm": t.get("bpm", 0),
                    "energy": t.get("energy"),
                })

        directives_active = [
            d for d in (sess.directives or [])
            if isinstance(d, dict) and d.get("status") == "active"
        ]

        recent_chat = []
        try:
            chat = sess.chat_history or []
            for msg in list(chat)[-10:]:
                recent_chat.append(msg)
        except Exception:
            recent_chat = []

        return {
            "any_activity": bool(recent_tracks or recent_chat),
            "recent_tracks": recent_tracks,
            "track_count": len(recent_tracks),
            "directives_active": directives_active,
            "recent_chat": recent_chat,
            "mood": sess.mood,
            "mood_profile_slug": (sess.mood_profile or {}).get(
                "canonical_slug", ""
            ),
            "emergency_count": getattr(sess, "emergency_count", 0),
            "window_minutes": 15,
        }

    def _synthesize(self, slice_data: dict):
        """Call Gemini with reflection prompt. Returns parsed dict or None."""
        directive_texts = []
        for d in slice_data["directives_active"]:
            if d.get("kind") == "shape":
                txt = (d.get("payload") or {}).get("text", "")
                if txt:
                    directive_texts.append(txt[:60])

        prompt = (
            "You are DJ Treta reflecting on the last 15 minutes of your set. "
            "Be brief, honest, and actionable. Output STRICT JSON with these keys:\n"
            "{\n"
            '  "went_well": [string],          // 1-3 short bullets\n'
            '  "to_improve": [string],         // 1-3 short bullets\n'
            '  "next_intent": "string",        // one short sentence\n'
            '  "mood_drift": "string",         // describe any drift in mood\n'
            '  "listener_engagement_delta": int  // -2..+2 estimate\n'
            "}\n\n"
            f"Window: {slice_data['window_minutes']} minutes\n"
            f"Mood: {slice_data['mood']} (slug: {slice_data['mood_profile_slug']})\n"
            f"Tracks played in window: {slice_data['track_count']}\n"
            f"Track titles: {[t['title'] for t in slice_data['recent_tracks']]}\n"
            f"Active directives: {directive_texts}\n"
            f"Emergency count: {slice_data['emergency_count']}\n"
            f"Recent chat (last 10): {slice_data['recent_chat']}\n\n"
            "Respond with JSON only."
        )

        try:
            response = self._call_llm(prompt)
            if not response:
                return None
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            response = response.strip()
            return json.loads(response)
        except Exception as exc:
            log.warning(f"[reflection] LLM/parse failed: {exc}")
            return None

    def _call_llm(self, prompt: str) -> str:
        """Call the local LiteLLM proxy with the prompt. Best-effort."""
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
                    "max_tokens": 1500,
                    "temperature": 0.4,
                },
                timeout=20,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning(f"[reflection] LLM call failed: {exc}")
            return ""

    def _record(self, synthesis: dict):
        """Append to session.reflections, FIFO at MAX_REFLECTIONS_RETAINED."""
        sess = self.being.session
        entry = {"ts": time.time(), **synthesis}
        try:
            sess.reflections.append(entry)
            if len(sess.reflections) > MAX_REFLECTIONS_RETAINED:
                keep = list(sess.reflections[-MAX_REFLECTIONS_RETAINED:])
                sess.reflections.clear()
                sess.reflections.extend(keep)
        except Exception as exc:
            log.warning(f"[reflection] record failed: {exc}")
            return
        log.info(
            f"[reflection] recorded — next intent: "
            f"{(synthesis.get('next_intent') or '')[:80]}"
        )
