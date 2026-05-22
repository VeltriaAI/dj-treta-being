"""Commands mixin — TUI/MCP command dispatch and conversation."""

import json
import logging
import threading
import time
from pathlib import Path

import httpx

from .agents import create_agents
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.runners import Runner
from .runtime_paths import runtime_path

log = logging.getLogger("dj-treta")

COMMAND_FILE = runtime_path("command.json")


class CommandsMixin:

    def _pick_up_directives(self):
        """v8: directives live in Session, written directly by set_mood /
        set_dj_directive / set_planner_directive tools. No file-based IPC.

        This method is retained as a no-op for backward compat with the main
        loop (agent/main.py:start() calls it each tick). It used to poll
        /tmp/dj-treta-mood-change.json and /tmp/dj-treta-directives.json.

        Side-effects that used to happen here (force replan on mood change)
        are now handled by Session callbacks registered in main.py:start().
        """
        return

    def _check_commands(self):
        if not COMMAND_FILE.exists():
            return
        try:
            raw = json.loads(COMMAND_FILE.read_text())
            COMMAND_FILE.unlink()
        except Exception:
            return

        cmd = raw.get("command", "")
        args = raw.get("args", {})
        cmd_id = raw.get("id", "")
        log.info(f"Command: {cmd}")

        self._last_command = cmd
        self._last_command_id = cmd_id
        self._last_result = "processing..."
        self._write_state()

        try:
            result = self._handle_command(cmd, args, cmd_id)
        except Exception as e:
            result = f"Error: {e}"

        if result != "processing...":
            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Result: {result[:200]}")

    def _handle_command(self, cmd, args, cmd_id):
        if cmd == "talk":
            message = args.get("message", "")
            readonly = args.get("readonly", False)
            if not message:
                return "No message"
            if not hasattr(self, 'being_agent') or not self.being_agent:
                return "Brain not ready"

            threading.Thread(
                target=self._being_talk, args=(message, cmd_id, readonly), daemon=True
            ).start()
            return "processing..."

        elif cmd == "skip":
            threading.Thread(target=self._agent_skip, daemon=True).start()
            return "processing..."

        elif cmd == "stop":
            if self.agent and not self._agent_busy:
                threading.Thread(
                    target=lambda: self._invoke_agent("Fade out the current track gracefully over 30 seconds."),
                    daemon=True
                ).start()
                return "Fading out..."
            return "Agent busy — try again in a moment"

        elif cmd == "change_mood":
            new_mood = args.get("mood", self.mood)
            self.mood = new_mood
            # Update current set's mood + genre
            if self.current_set:
                self.current_set["mood"] = new_mood
                self.current_set["genre"] = new_mood
            # Capture as user intent so planner picks it up immediately
            self.user_intent = f"Switch to {new_mood} — listener changed mood"
            # Force planner to replan
            self._tracks_since_plan = self.config.planner.replan_every_n_tracks
            return f"Mood changed to {new_mood}"

        elif cmd == "feedback":
            feedback_type = args.get("type", "like")  # 'like' or 'dislike'
            from .main import _get_status
            from .db import add_feedback
            status = _get_status(self.config.mixxx.url)
            if not status:
                return "Mixxx offline"
            # Get current playing track
            track_title = ""
            track_path = ""
            for dk in [1, 2]:
                if status.get(f"deck{dk}", {}).get("playing"):
                    try:
                        tinfo = httpx.get(f"{self.config.mixxx.url}/api/deck/{dk}/track_info", timeout=2).json()
                        track_title = tinfo.get("title", "")
                        track_path = tinfo.get("file_path", "")
                    except Exception:
                        pass
                    break
            if not track_title:
                return "No track playing"
            set_id = self.current_set["id"] if self.current_set else ""
            add_feedback(track_title, feedback_type, track_path, set_id)
            emoji = "👍" if feedback_type == "like" else "👎"
            log.info(f"Feedback: {emoji} {track_title}")
            return f"{emoji} {feedback_type.upper()}: {track_title}"

        elif cmd == "change_sources":
            source = args.get("source", "")
            enabled = args.get("enabled", True)
            if source == "youtube":
                self.config.sources.youtube = enabled
            elif source in ("treta_originals", "originals"):
                self.config.sources.treta_originals = enabled
            # Recreate agents with new tool access
            log.info(f"Source changed: {source} → {'on' if enabled else 'off'} — rebuilding agents")
            being_agent, dj_agent, planner_agent = create_agents(self.config)
            self.being_agent = being_agent
            self.agent = dj_agent
            self.planner_agent = planner_agent
            compaction = EventsCompactionConfig(compaction_interval=10, overlap_size=2)
            being_app = App(name="treta_being", root_agent=being_agent, events_compaction_config=compaction)
            dj_app = App(name="dj_treta", root_agent=dj_agent, events_compaction_config=compaction)
            planner_app = App(name="dj_treta_planner", root_agent=planner_agent, events_compaction_config=compaction)
            self._being_runner = Runner(app=being_app, session_service=self._session_service)
            self._dj_runner = Runner(app=dj_app, session_service=self._session_service)
            self._planner_runner = Runner(app=planner_app, session_service=self._session_service)
            async def _reinit():
                self._being_session = await self._session_service.create_session(app_name="treta_being", user_id="listener")
                self._dj_session = await self._session_service.create_session(app_name="dj_treta", user_id="dj")
                self._planner_session = await self._session_service.create_session(app_name="dj_treta_planner", user_id="planner")
            self._run_async(_reinit())
            return f"Source {source} → {'on' if enabled else 'off'} (agents rebuilt)"

        elif cmd == "set_mode":
            mode = (args.get("mode", "") or "").lower().strip()
            if mode not in ("sarathi", "autonomous"):
                return f"Unknown mode '{mode}' — use sarathi | autonomous"
            self.session.sarathi_mode = (mode == "sarathi")
            if mode == "autonomous":
                # Drop any in-flight motion gate so she resumes the wheel.
                self.session.manish_in_motion = False
            log.info(f"Mode → {mode} (sarathi_mode={self.session.sarathi_mode})")
            return (
                f"Mode: {mode}. "
                + ("Manish drives transitions; Treta suggests."
                   if mode == "sarathi"
                   else "Treta executes transitions autonomously.")
            )

        elif cmd in ("confirm_transition", "reject_transition"):
            from .tools.sarathi import confirm_suggestion, reject_suggestion
            sid = args.get("suggestion_id", "")
            if cmd == "confirm_transition":
                res = confirm_suggestion(suggestion_id=sid)
            else:
                res = reject_suggestion(suggestion_id=sid, reason=args.get("reason", ""))
            return res.get("message", str(res))

        else:
            return f"Unknown: {cmd}"

    def _being_talk(self, message, cmd_id, readonly=False):
        """Being handles ALL conversation. She thinks, responds, and optionally directs agents."""
        from .main import _get_status

        try:
            from .prompts import build_being_user_message

            context = self._build_context(_get_status(self.config.mixxx.url))
            history = self._format_history()

            # Boot-replay: on the first Treta invocation after fresh daemon
            # boot, prepend a "RECENT CONVERSATION" block built from today's
            # chat JSONL. This gives her continuity across restarts. The
            # flag flips False after first use so subsequent prompts stay
            # clean (ADK session accumulates new turns normally).
            replay_prefix = ""
            if getattr(self, "_chat_replay_pending", False):
                try:
                    from .chat_persistence import load_recent_turns, format_replay_block
                    recent = load_recent_turns(n=20, max_age_hours=24.0)
                    replay_prefix = format_replay_block(recent, max_chars=4000)
                    if replay_prefix:
                        log.info(
                            f"[chat-replay] prepending {len(recent)} prior turns "
                            f"({len(replay_prefix)} chars) to Treta's first prompt"
                        )
                except Exception as _replay_exc:
                    log.debug(f"chat replay failed (non-fatal): {_replay_exc}")
                # Always flip the flag after attempt — don't retry replay
                # on subsequent invocations.
                self._chat_replay_pending = False

            # Pull active self_suggestion directives so they surface in
            # Treta's prompt as an INNER NUDGE block. Skipped in readonly
            # mode (web listener should never see Treta's inner state).
            active_suggestions = []
            if not readonly:
                try:
                    from .tools.suggestions import list_self_suggestions
                    active_suggestions = list_self_suggestions()
                except Exception as _sug_exc:
                    log.debug(
                        f"list_self_suggestions failed (non-fatal): {_sug_exc}"
                    )

            # Sarathi: surface the mode + any live transition suggestion so
            # she reads "do it" as confirm and "darker / no" as reject.
            sarathi_block = ""
            if not readonly and getattr(self.session, "sarathi_mode", False):
                lines = [
                    "── MODE: SARATHI ──",
                    "Manish drives transitions on the FLX4. You suggest; you do "
                    "NOT execute unless he hands you the wheel.",
                ]
                try:
                    from .tools.sarathi import list_pending_suggestions
                    pend = list_pending_suggestions()
                except Exception:
                    pend = []
                if pend:
                    s = pend[-1]
                    lines.append(
                        f"Live suggestion in front of him: id={s.get('id')} — "
                        f"{s.get('technique')} → deck {s.get('to_deck')} "
                        f"({s.get('track_title') or 'next track'}); "
                        f"{(s.get('reason') or '')[:100]}"
                    )
                    lines.append(
                        '"do it"/"kar do"/"haan" → confirm_suggestion(); '
                        '"no"/"darker"/"doosra" → reject_suggestion(reason=…); '
                        '"i\'ve got this" → leave it, he\'s driving.'
                    )
                else:
                    lines.append("No live suggestion right now.")
                lines.append("── END SARATHI ──\n")
                sarathi_block = "\n".join(lines) + "\n"

            being_msg = build_being_user_message(
                context=context,
                history=history,
                message=message,
                readonly=readonly,
                self_suggestions=active_suggestions,
                sarathi_block=sarathi_block,
            )
            if replay_prefix:
                being_msg = replay_prefix + "\n" + being_msg

            with self._talk_lock:
                result = self._invoke_being(
                    being_msg,
                    timeout=120, max_calls=20 if not readonly else 5,
                )

            # Update conversation memory
            self._chat_history.append((message, result))
            if len(self._chat_history) > 10:
                self._chat_history = self._chat_history[-10:]

            # Compute durable-storage args once (LanceDB + JSONL share them).
            set_id = ""
            if self.current_set and isinstance(self.current_set, dict):
                set_id = str(self.current_set.get("id") or "")
            mood = getattr(self.session, "mood", "") or ""

            # Durable semantic memory — embed this turn so Treta can
            # recall it across daemon restarts via
            # recall_similar_interaction(). Best-effort; failures
            # never break the chat flow. Set evolution plan Tier 1.4.
            try:
                from .memory import store_interaction
                store_interaction(
                    message_text=message,
                    treta_response=result,
                    set_id=set_id,
                    mood=mood,
                )
            except Exception as _mem_exc:
                log.debug(f"store_interaction failed (non-fatal): {_mem_exc}")

            # Ordered + auditable JSONL — daily file under
            # ~/.beings/dj-treta/sessions/. Replayed on next daemon boot
            # as a context block prepended to Treta's first prompt,
            # giving her conversation continuity across restarts.
            try:
                from .chat_persistence import append_chat_turn
                append_chat_turn(
                    message=message,
                    response=result,
                    set_id=set_id,
                    mood=mood,
                )
            except Exception as _jsonl_exc:
                log.debug(f"chat JSONL append failed (non-fatal): {_jsonl_exc}")

            self._last_command_id = cmd_id
            self._last_result = result
            self._write_state()
            log.info(f"Being talk {'(readonly)' if readonly else ''}: {result[:500]}")
            if hasattr(self, '_ws_broadcast'):
                self._ws_broadcast("talk_response", {
                    "id": cmd_id,
                    "result": result,
                })
        except Exception as e:
            self._last_command_id = cmd_id
            self._last_result = f"Error: {e}"
            self._write_state()

    def _agent_skip(self):
        """Skip — emit user_skip signal. DJ agent handles on next P4 tick;
        watchdog P2 fallback fires if the signal sits unresolved >5s.

        Phase A2: removed the direct do_transition + inline _load_next_on_idle
        that bypassed the DJ agent and left the new idle deck empty
        post-crossfade (observed bug, 2026-04-19).
        """
        try:
            self.session.user_skip = {
                "style": "fast",
                "ts": time.time(),
                "directive": None,
            }
            self._last_result = "Skip signaled to DJ"
            log.info("Skip: user_skip signal set, DJ will handle on next tick")
            self._write_state()
        except Exception as e:
            self._last_result = f"Skip error: {e}"
            self._write_state()
