"""Unit tests for agent/prompts.py — prompt builder functions.

These test that prompts are correctly formatted, not LLM behavior.
No LLM calls — pure Python assertions.
"""

import json
import pytest

from agent.prompts import (
    build_dj_user_message,
    build_planner_user_message,
    build_being_user_message,
    build_consciousness_user_message,
    build_heartbeat_context,
    format_candidate_text,
    format_feedback_line,
    format_timeline,
    get_current_section,
)


class TestBuildDjUserMessage:

    def test_basic_format(self):
        """DJ message should contain ACTIVE, NEXT, and the decision-prompt section."""
        msg = build_dj_user_message(
            active_track="Test Track", position=100, duration=300, remaining=200,
            active_bpm=125, active_file_bpm=125, active_key="Am",
            active_section="groove (energy:6, 30s-180s)",
            active_timeline="0s-30s intro(energy:3) → 30s-180s groove(energy:6)",
            idle_track="Next Track", idle_deck=2,
            idle_bpm=126, idle_file_bpm=126, idle_key="Cm",
            idle_timeline="(no analysis)",
        )
        assert "ACTIVE:" in msg
        assert "Test Track" in msg
        assert "NEXT:" in msg
        assert "Next Track" in msg
        # New v8/v9 prompt drops the literal "ACTION REQUIRED:" header in
        # favour of a "Decide now. Your options:" decision block.
        assert "Decide now" in msg
        assert "schedule_transition" in msg

    def test_directive_included(self):
        """DJ directive should appear at top of message."""
        msg = build_dj_user_message(
            active_track="A", position=100, duration=300, remaining=200,
            active_bpm=125, active_file_bpm=125, active_key="Am",
            active_section="breakdown", active_timeline="...",
            idle_track="B", idle_deck=2,
            idle_bpm=126, idle_file_bpm=126, idle_key="Cm",
            idle_timeline="...",
            dj_directive="use bass_swap",
        )
        assert "DIRECTIVE FROM TRETA:" in msg
        assert "use bass_swap" in msg

    def test_no_directive_when_empty(self):
        """No directive section when directive is empty."""
        msg = build_dj_user_message(
            active_track="A", position=100, duration=300, remaining=200,
            active_bpm=125, active_file_bpm=125, active_key="Am",
            active_section="breakdown", active_timeline="...",
            idle_track="B", idle_deck=2,
            idle_bpm=126, idle_file_bpm=126, idle_key="Cm",
            idle_timeline="...",
        )
        assert "DIRECTIVE FROM TRETA:" not in msg

    def test_pending_transition(self):
        """Should include pending warning when transition is pending."""
        msg = build_dj_user_message(
            active_track="A", position=100, duration=300, remaining=200,
            active_bpm=125, active_file_bpm=125, active_key="Am",
            active_section="breakdown", active_timeline="...",
            idle_track="B", idle_deck=2,
            idle_bpm=126, idle_file_bpm=126, idle_key="Cm",
            idle_timeline="...",
            transition_pending=True,
        )
        assert "TRANSITION ALREADY PENDING" in msg

    def test_bpm_formatting(self):
        """BPM should be formatted as integer (:.0f)."""
        msg = build_dj_user_message(
            active_track="A", position=100, duration=300, remaining=200,
            active_bpm=125.7, active_file_bpm=125.7, active_key="Am",
            active_section="breakdown", active_timeline="...",
            idle_track="B", idle_deck=2,
            idle_bpm=130.3, idle_file_bpm=130.3, idle_key="Cm",
            idle_timeline="...",
        )
        # 125.7 rounds to 126, 130.3 rounds to 130
        assert "BPM:126" in msg
        assert "BPM:130" in msg

    def test_track_name_truncation(self):
        """Track names longer than 40 chars should be truncated."""
        long_name = "A" * 60
        msg = build_dj_user_message(
            active_track=long_name, position=100, duration=300, remaining=200,
            active_bpm=125, active_file_bpm=125, active_key="Am",
            active_section="breakdown", active_timeline="...",
            idle_track="B", idle_deck=2,
            idle_bpm=126, idle_file_bpm=126, idle_key="Cm",
            idle_timeline="...",
        )
        # Should contain first 40 chars but not the full 60
        assert "A" * 40 in msg
        assert "A" * 41 not in msg

    def test_position_and_duration(self):
        """Position and duration should appear formatted as integers."""
        msg = build_dj_user_message(
            active_track="Track", position=123.4, duration=456.7, remaining=333.3,
            active_bpm=125, active_file_bpm=125, active_key="Am",
            active_section="drop", active_timeline="...",
            idle_track="Next", idle_deck=1,
            idle_bpm=126, idle_file_bpm=126, idle_key="Cm",
            idle_timeline="...",
        )
        assert "123s/457s" in msg
        assert "333s left" in msg


class TestBuildPlannerUserMessage:

    def test_basic_format(self):
        """Planner message should have current, played, library, mood."""
        msg = build_planner_user_message(
            current_info="Track A | BPM:125 Key:Am Energy:7",
            played_list=["Track A", "Track B"],
            candidate_text="  - Track C | path: /c.mp3 | BPM:126",
            mood="melodic-techno",
        )
        assert "Currently playing:" in msg
        assert "Track A" in msg
        assert "Already played" in msg
        assert "melodic-techno" in msg
        assert "OVERRIDES" in msg

    def test_directive_included(self):
        msg = build_planner_user_message(
            current_info="X", played_list=[], candidate_text="",
            mood="techno",
            planner_directive="find bhojpuri tracks",
        )
        assert "DIRECTIVE FROM TRETA:" in msg
        assert "bhojpuri" in msg

    def test_user_intent_included(self):
        """Listener request should appear when user_intent is set."""
        msg = build_planner_user_message(
            current_info="X", played_list=[], candidate_text="",
            mood="techno",
            user_intent="play some trance",
        )
        assert "LISTENER REQUEST:" in msg
        assert "play some trance" in msg

    def test_feedback_included(self):
        msg = build_planner_user_message(
            current_info="X", played_list=[], candidate_text="",
            mood="techno",
            feedback_line="\nLISTENER LIKES: Track A, Track B\n",
        )
        assert "LISTENER LIKES" in msg

    def test_empty_candidates(self):
        msg = build_planner_user_message(
            current_info="X", played_list=[], candidate_text="",
            mood="techno",
        )
        assert "(none)" in msg

    def test_default_mood_fallback(self):
        """When mood is empty, should default to melodic-techno."""
        msg = build_planner_user_message(
            current_info="X", played_list=[], candidate_text="",
            mood="",
        )
        assert "melodic-techno" in msg

    def test_source_instructions_appended(self):
        """Source instructions should be appended to message."""
        msg = build_planner_user_message(
            current_info="X", played_list=[], candidate_text="",
            mood="techno",
            source_instructions="Use YouTube for downloads.\n",
        )
        assert "Use YouTube for downloads." in msg


class TestBuildBeingUserMessage:

    def test_basic_format(self):
        msg = build_being_user_message(
            context="mood: techno", history="", message="hello",
        )
        assert 'The listener says: "hello"' in msg
        assert "Respond naturally" in msg

    def test_readonly_tag(self):
        msg = build_being_user_message(
            context="", history="", message="hi", readonly=True,
        )
        assert "READONLY" in msg
        assert "Do NOT call" in msg

    def test_no_readonly_by_default(self):
        msg = build_being_user_message(
            context="", history="", message="hi",
        )
        assert "READONLY" not in msg

    def test_context_and_history_in_output(self):
        msg = build_being_user_message(
            context="Mood: deep-house, 2 tracks played",
            history="Listener: play something chill\nTreta: Sure!",
            message="thanks",
        )
        assert "Mood: deep-house" in msg
        assert "play something chill" in msg
        assert 'The listener says: "thanks"' in msg


class TestFormatTimeline:

    def test_with_sections(self):
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
                {"start": 30, "end": 120, "section": "drop", "energy": 9},
                {"start": 120, "end": 150, "section": "outro", "energy": 2},
            ])
        }
        result = format_timeline(meta)
        assert "intro" in result
        assert "drop" in result
        assert "outro" in result
        assert "\u2192" in result  # arrow character

    def test_section_format(self):
        """Each section should show start-end, name, and energy."""
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
            ])
        }
        result = format_timeline(meta)
        assert "0s-30s intro(energy:3)" in result

    def test_no_timeline(self):
        meta = {"bpm": 125, "key_musical": "Am", "energy_peak": 7}
        result = format_timeline(meta)
        assert "125" in result
        assert "Am" in result

    def test_none_meta(self):
        assert "no analysis" in format_timeline(None)

    def test_empty_meta(self):
        assert "no analysis" in format_timeline({})

    def test_list_timeline_not_json_string(self):
        """Timeline can be a list (already parsed) not just a JSON string."""
        meta = {
            "timeline": [
                {"start": 0, "end": 60, "section": "buildup", "energy": 5},
            ]
        }
        result = format_timeline(meta)
        assert "buildup" in result
        assert "energy:5" in result


class TestGetCurrentSection:

    def test_finds_section(self):
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
                {"start": 30, "end": 120, "section": "drop", "energy": 9},
            ])
        }
        result = get_current_section(meta, 50.0)
        assert "drop" in result

    def test_section_format_includes_energy_and_range(self):
        """Section result should include energy and time range."""
        meta = {
            "timeline": json.dumps([
                {"start": 30, "end": 120, "section": "drop", "energy": 9},
            ])
        }
        result = get_current_section(meta, 60.0)
        assert "drop" in result
        assert "energy:9" in result
        assert "30s-120s" in result

    def test_none_meta(self):
        assert get_current_section(None, 50.0) == "unknown"

    def test_no_timeline_key(self):
        assert get_current_section({"bpm": 125}, 50.0) == "unknown"

    def test_past_end(self):
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
            ])
        }
        result = get_current_section(meta, 50.0)
        assert result == "past end"

    def test_at_section_boundary(self):
        """Position exactly at section start should be in that section."""
        meta = {
            "timeline": json.dumps([
                {"start": 0, "end": 30, "section": "intro", "energy": 3},
                {"start": 30, "end": 120, "section": "drop", "energy": 9},
            ])
        }
        # At 30.0: start <= 30 < end(120) matches drop
        result = get_current_section(meta, 30.0)
        assert "drop" in result


class TestFormatCandidateText:

    def test_basic(self):
        candidates = [
            {"title": "Track A", "path": "/a.mp3", "bpm": 125, "key_musical": "Am", "energy_peak": 7},
        ]
        result = format_candidate_text(candidates)
        assert "Track A" in result
        assert "BPM:125" in result
        assert "Am" in result
        assert "Energy:7" in result

    def test_includes_mix_points(self):
        """Should include Mix-in and Mix-out values."""
        candidates = [
            {
                "title": "Track A", "path": "/a.mp3", "bpm": 125,
                "key_musical": "Am", "energy_peak": 7,
                "mix_in_seconds": 16, "mix_out_seconds": 32,
            },
        ]
        result = format_candidate_text(candidates)
        assert "Mix-in:16s" in result
        assert "Mix-out:32s" in result

    def test_with_timeline(self):
        candidates = [
            {
                "title": "Track B", "path": "/b.mp3", "bpm": 128, "key_musical": "Gm",
                "energy_peak": 8, "timeline": json.dumps([
                    {"section": "intro", "energy": 3}, {"section": "drop", "energy": 9}
                ]),
            },
        ]
        result = format_candidate_text(candidates)
        assert "Structure:" in result
        assert "intro(3)" in result
        assert "drop(9)" in result

    def test_empty(self):
        assert format_candidate_text([]) == ""

    def test_multiple_candidates(self):
        candidates = [
            {"title": "Track A", "path": "/a.mp3", "bpm": 125, "key_musical": "Am", "energy_peak": 7},
            {"title": "Track B", "path": "/b.mp3", "bpm": 128, "key_musical": "Gm", "energy_peak": 8},
        ]
        result = format_candidate_text(candidates)
        assert "Track A" in result
        assert "Track B" in result
        lines = result.strip().split("\n")
        assert len(lines) == 2


class TestFormatFeedbackLine:

    def test_with_likes(self):
        liked = [
            {"track_title": "Track A", "genre": "techno", "bpm": 125},
            {"track_title": "Track B", "genre": "techno", "bpm": 128},
        ]
        result = format_feedback_line(liked, [])
        assert "LISTENER LIKES" in result
        assert "Track A" in result
        assert "techno" in result

    def test_with_bpm_range(self):
        """Should show BPM range from liked tracks."""
        liked = [
            {"track_title": "A", "bpm": 120},
            {"track_title": "B", "bpm": 130},
        ]
        result = format_feedback_line(liked, [])
        assert "120-130" in result

    def test_with_dislikes(self):
        result = format_feedback_line([], ["Bad Track"])
        assert "DISLIKES" in result
        assert "Bad Track" in result

    def test_both_likes_and_dislikes(self):
        liked = [{"track_title": "Good", "genre": "house", "bpm": 124}]
        result = format_feedback_line(liked, ["Bad"])
        assert "LISTENER LIKES" in result
        assert "Good" in result
        assert "DISLIKES" in result
        assert "Bad" in result

    def test_empty(self):
        assert format_feedback_line([], []) == ""


class TestBuildHeartbeatContext:

    def test_with_active_set(self):
        import time
        ctx = build_heartbeat_context(
            current_set={"title": "Evening Vibes", "started_at": time.time() - 2700},
            tracks_played=[{"title": "Track A"}, {"title": "Track B"}],
            mood="melodic-techno",
            emergency_count=0,
            time_str="22:15",
        )
        assert "22:15" in ctx
        assert "Evening Vibes" in ctx
        assert "2 tracks" in ctx
        assert "melodic-techno" in ctx
        assert "Track B" in ctx  # last track

    def test_with_emergencies(self):
        import time
        ctx = build_heartbeat_context(
            current_set={"title": "Test", "started_at": time.time()},
            emergency_count=3,
            time_str="20:00",
        )
        assert "Emergencies: 3" in ctx

    def test_minimal(self):
        ctx = build_heartbeat_context(time_str="14:00")
        assert "14:00" in ctx

    def test_pipe_separated(self):
        """Parts should be separated by ' | '."""
        import time
        ctx = build_heartbeat_context(
            current_set={"title": "Set", "started_at": time.time()},
            tracks_played=[{"title": "Track"}],
            mood="techno",
            time_str="18:00",
        )
        assert " | " in ctx
        parts = ctx.split(" | ")
        assert len(parts) >= 3  # time, set info, mood, last track

    def test_no_emergencies_when_zero(self):
        ctx = build_heartbeat_context(
            emergency_count=0,
            time_str="12:00",
        )
        assert "Emergencies" not in ctx


class TestBuildConsciousnessMessage:

    def test_format(self):
        msg = build_consciousness_user_message("Time: 22:15 | Set running")
        assert "HEARTBEAT TICK" in msg
        assert "22:15" in msg
        assert "HEARTBEAT_OK" in msg

    def test_context_embedded(self):
        msg = build_consciousness_user_message("Time: 14:00 | Mood: chill")
        assert "14:00" in msg
        assert "Mood: chill" in msg
