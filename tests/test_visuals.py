"""E6 — Unit tests for the Generative Visual Layer.

Tests palette mapping, OSC message construction (fake sender), and
the VisualEngine tick interface.  The Omni call is not exercised
(model access unconfirmed) — the stub path is tested instead.

Run:  pytest tests/test_visuals.py -v
"""

from __future__ import annotations

import socket
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Palette tests
# ---------------------------------------------------------------------------

class TestPaletteMapping:
    """Palette mapping — deterministic, no I/O."""

    def test_deep_house_minor_key_returns_sunset(self):
        from agent.visuals.palette import get_palette
        assert get_palette("deep-house", "Am", 7) == "Sunset"

    def test_deep_house_major_key_returns_desert(self):
        from agent.visuals.palette import get_palette
        assert get_palette("deep house", "C", 6) == "Desert"

    def test_dnb_high_energy_returns_laser(self):
        from agent.visuals.palette import get_palette
        assert get_palette("drum-n-bass", "Dm", 9) == "Laser"

    def test_dnb_low_energy_returns_midnight(self):
        from agent.visuals.palette import get_palette
        assert get_palette("dnb", "Em", 3) == "Midnight"

    def test_psy_high_energy_returns_fractal(self):
        from agent.visuals.palette import get_palette
        assert get_palette("psytrance", "Am", 9) == "Fractal"

    def test_psy_low_energy_returns_plasma(self):
        from agent.visuals.palette import get_palette
        assert get_palette("goa", "Gm", 4) == "Plasma"

    def test_melodic_techno_minor_high_returns_horizon(self):
        from agent.visuals.palette import get_palette
        assert get_palette("melodic-techno", "Am", 8) == "Horizon"

    def test_melodic_techno_major_high_returns_sunset(self):
        from agent.visuals.palette import get_palette
        assert get_palette("melodic techno", "F", 7) == "Sunset"

    def test_melodic_techno_low_energy_returns_midnight(self):
        from agent.visuals.palette import get_palette
        assert get_palette("melodic-techno", "Am", 2) == "Midnight"

    def test_hardstyle_high_energy_returns_uv(self):
        from agent.visuals.palette import get_palette
        assert get_palette("hardstyle", "Am", 9) == "UV"

    def test_afro_high_energy_returns_desert(self):
        from agent.visuals.palette import get_palette
        assert get_palette("bollyafro", "", 7) == "Desert"

    def test_organic_low_energy_returns_forest(self):
        from agent.visuals.palette import get_palette
        assert get_palette("organic", "C", 2) == "Forest"

    def test_trance_high_energy_returns_uv(self):
        from agent.visuals.palette import get_palette
        assert get_palette("trance", "Am", 8) == "UV"

    def test_unknown_genre_returns_midnight_fallback(self):
        from agent.visuals.palette import get_palette
        assert get_palette("xyzfoo", "Am", 5) == "Midnight"

    def test_empty_inputs_returns_midnight_fallback(self):
        from agent.visuals.palette import get_palette
        assert get_palette("", "", 5) == "Midnight"

    def test_energy_clamped_out_of_bounds(self):
        """Energy outside 1-10 should not crash."""
        from agent.visuals.palette import get_palette
        # Should not raise; result is some defined palette
        result = get_palette("dnb", "Am", 99)
        assert isinstance(result, str)
        result2 = get_palette("dnb", "Am", -5)
        assert isinstance(result2, str)

    def test_palette_colors_returns_hex_dict(self):
        from agent.visuals.palette import palette_colors
        colors = palette_colors("Horizon")
        assert "primary" in colors
        assert colors["primary"].startswith("#")

    def test_palette_colors_fallback_for_unknown(self):
        from agent.visuals.palette import palette_colors
        colors = palette_colors("NonExistentPalette")
        # Should fall back to Midnight
        assert "primary" in colors

    def test_key_mode_minor_detection(self):
        from agent.visuals.palette import _key_mode
        assert _key_mode("Am") == "minor"
        assert _key_mode("Dm") == "minor"
        assert _key_mode("F#m") == "minor"

    def test_key_mode_major_detection(self):
        from agent.visuals.palette import _key_mode
        assert _key_mode("C") == "major"
        assert _key_mode("F#") == "major"
        assert _key_mode("") == "major"

    def test_progressive_minor_returns_horizon(self):
        from agent.visuals.palette import get_palette
        assert get_palette("progressive", "Am", 7) == "Horizon"

    def test_progressive_major_returns_sunset(self):
        from agent.visuals.palette import get_palette
        assert get_palette("progressive", "G", 7) == "Sunset"


# ---------------------------------------------------------------------------
# OSC message construction tests (no network)
# ---------------------------------------------------------------------------

class TestOSCMessageConstruction:
    """Test raw OSC packet builder without sending."""

    def test_encode_str_null_terminated(self):
        from agent.visuals.osc_emitter import _encode_str
        result = _encode_str("test")
        assert b"\x00" in result
        assert len(result) % 4 == 0

    def test_encode_float_big_endian(self):
        from agent.visuals.osc_emitter import _encode_float
        result = _encode_float(128.0)
        assert len(result) == 4
        # Unpack and verify round-trip
        val = struct.unpack(">f", result)[0]
        assert abs(val - 128.0) < 0.01

    def test_encode_int_big_endian(self):
        from agent.visuals.osc_emitter import _encode_int
        result = _encode_int(7)
        assert len(result) == 4
        val = struct.unpack(">i", result)[0]
        assert val == 7

    def test_build_osc_message_type_tags(self):
        from agent.visuals.osc_emitter import _build_osc_message
        pkt = _build_osc_message("/treta/beat", 128.5)
        # Packet must start with address
        assert pkt.startswith(b"/treta/beat")
        # Type tag string must contain 'f' (float)
        # Find the comma that starts the type tag block
        assert b",f" in pkt

    def test_build_osc_message_multi_args(self):
        from agent.visuals.osc_emitter import _build_osc_message
        pkt = _build_osc_message("/treta/bundle", "test-string")
        assert b"/treta/bundle" in pkt
        assert b",s" in pkt

    def test_build_osc_message_int_and_str(self):
        from agent.visuals.osc_emitter import _build_osc_message
        pkt = _build_osc_message("/test", 7, "drop")
        # Type tag should have 'is'
        assert b",is" in pkt

    def test_pad4(self):
        from agent.visuals.osc_emitter import _pad4
        assert _pad4(0) == 0
        assert _pad4(1) == 4
        assert _pad4(4) == 4
        assert _pad4(5) == 8
        assert _pad4(8) == 8


class TestOSCEmitterFakeSend:
    """Test OSCEmitter with a fake UDP socket (no real network)."""

    def _make_emitter_with_fake_sock(self):
        """Return an OSCEmitter that uses a fake socket (no python-osc)."""
        from agent.visuals.osc_emitter import OSCEmitter
        emitter = OSCEmitter.__new__(OSCEmitter)
        emitter.host = "127.0.0.1"
        emitter.port = 7000
        emitter._has_python_osc = False
        # Inject a fake socket that records sent packets
        fake_sock = MagicMock()
        emitter._sock = fake_sock
        emitter._osc_client = None
        return emitter, fake_sock

    def test_emit_calls_sendto(self):
        emitter, fake_sock = self._make_emitter_with_fake_sock()
        emitter.emit(
            bpm=128.0,
            energy=7,
            section="drop",
            palette="Horizon",
            key="Am",
            genre="melodic-techno",
            beat_active=True,
        )
        # sendto should have been called multiple times (one per OSC address)
        assert fake_sock.sendto.call_count >= 7  # 7 individual + 1 bundle

    def test_emit_sends_to_configured_address(self):
        emitter, fake_sock = self._make_emitter_with_fake_sock()
        emitter.emit(bpm=130.0, energy=8, section="buildup", palette="Laser")
        # All calls should target the configured host:port
        for call in fake_sock.sendto.call_args_list:
            args = call[0]
            assert args[1] == ("127.0.0.1", 7000)

    def test_emit_bundle_contains_json(self):
        import json
        from agent.visuals.osc_emitter import _build_osc_message, _encode_str

        emitter, fake_sock = self._make_emitter_with_fake_sock()
        emitter.emit(
            bpm=128.0, energy=5, section="breakdown", palette="Midnight",
            key="Dm", genre="deep-house",
        )
        # Find the /treta/bundle sendto call
        bundle_call = None
        for call in fake_sock.sendto.call_args_list:
            pkt = call[0][0]
            if b"/treta/bundle" in pkt:
                bundle_call = pkt
                break
        assert bundle_call is not None, "No /treta/bundle message sent"
        # The bundle payload should contain valid JSON with our fields
        # JSON is encoded as an OSC string after the type-tag block
        # Find the JSON payload by scanning for '{'
        raw = bundle_call
        brace_idx = raw.find(b"{")
        assert brace_idx != -1
        # Find end — look for last '}'
        end_idx = raw.rfind(b"}")
        json_bytes = raw[brace_idx:end_idx + 1]
        data = json.loads(json_bytes.decode("utf-8"))
        assert data["bpm"] == 128.0
        assert data["energy"] == 5
        assert data["section"] == "breakdown"
        assert data["palette"] == "Midnight"

    def test_emit_does_not_raise_on_sendto_error(self):
        """Socket errors must not propagate — visual layer is best-effort."""
        emitter, fake_sock = self._make_emitter_with_fake_sock()
        fake_sock.sendto.side_effect = OSError("network down")
        # Should not raise
        emitter.emit(bpm=128.0, energy=5, section="drop", palette="Horizon")

    def test_close_releases_socket(self):
        emitter, fake_sock = self._make_emitter_with_fake_sock()
        emitter.close()
        fake_sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# VisualEngine integration tests (no real network, no LLM)
# ---------------------------------------------------------------------------

class TestVisualEngine:
    """VisualEngine tick — palette resolution + OSC emission wired together."""

    def _make_engine(self, omni_enabled=False):
        from agent.visuals.engine import VisualEngine
        from unittest.mock import MagicMock

        engine = VisualEngine(
            enabled=True,
            osc_host="127.0.0.1",
            osc_port=7001,
            omni_enabled=omni_enabled,
        )
        # Replace the real OSC emitter with a mock
        engine._osc = MagicMock()
        return engine

    def test_tick_returns_dict_with_palette(self):
        engine = self._make_engine()
        result = engine.tick(
            bpm=128.0, energy=7, section="drop",
            key="Am", genre="melodic-techno",
        )
        assert result["enabled"] is True
        assert "palette" in result
        assert result["palette"] == "Horizon"  # melodic-techno minor high

    def test_tick_disabled_returns_noop(self):
        from agent.visuals.engine import VisualEngine
        engine = VisualEngine(enabled=False)
        result = engine.tick(bpm=128.0, energy=7, section="drop", key="Am", genre="techno")
        assert result == {"enabled": False}

    def test_tick_calls_osc_emit(self):
        engine = self._make_engine()
        engine.tick(bpm=130.0, energy=9, section="buildup", key="Fm", genre="hardstyle")
        engine._osc.emit.assert_called_once()

    def test_tick_osc_emit_args(self):
        engine = self._make_engine()
        engine.tick(bpm=130.0, energy=9, section="drop", key="Am", genre="psy-trance")
        call_kwargs = engine._osc.emit.call_args[1]
        assert call_kwargs["bpm"] == 130.0
        assert call_kwargs["energy"] == 9
        assert call_kwargs["section"] == "drop"
        # psy high energy → Fractal
        assert call_kwargs["palette"] == "Fractal"

    def test_tick_updates_last_palette(self):
        engine = self._make_engine()
        engine.tick(bpm=130.0, energy=8, section="drop", key="Am", genre="dnb")
        assert engine.last_palette == "Laser"

    def test_tick_osc_error_does_not_propagate(self):
        """An OSC emit error must not crash the tick."""
        engine = self._make_engine()
        engine._osc.emit.side_effect = RuntimeError("socket broken")
        # Should not raise
        result = engine.tick(bpm=128.0, energy=5, section="groove", genre="techno")
        assert result["osc_ok"] is False

    def test_tick_omni_not_triggered_when_disabled(self):
        engine = self._make_engine(omni_enabled=False)
        result = engine.tick(bpm=128.0, energy=5, section="drop", genre="techno")
        assert result["omni_triggered"] is False

    def test_tick_omni_triggered_when_enabled(self):
        """With omni_enabled=True and interval=0, first tick should trigger."""
        engine = self._make_engine(omni_enabled=True)
        engine.omni_interval_seconds = 0.0
        # Patch generate_visual to return a stub immediately
        async def _stub(*a, **kw):
            return {"ok": False, "stub": True, "concept": "", "video_url": "",
                    "palette": "Midnight", "error": "stub"}
        with patch("agent.visuals.engine.generate_visual", _stub):
            result = engine.tick(bpm=128.0, energy=7, section="drop", genre="techno")
            # Let the background thread complete
            if engine._omni_thread:
                engine._omni_thread.join(timeout=3.0)
        assert result["omni_triggered"] is True

    def test_tick_omni_rate_limited(self):
        """Second tick within interval window must NOT trigger Omni again."""
        import time
        engine = self._make_engine(omni_enabled=True)
        engine.omni_interval_seconds = 60.0  # long window

        async def _stub(*a, **kw):
            return {"ok": False, "stub": True, "concept": "", "video_url": "",
                    "palette": "Midnight", "error": "stub"}
        with patch("agent.visuals.engine.generate_visual", _stub):
            r1 = engine.tick(bpm=128.0, energy=5, section="drop", genre="techno")
            r2 = engine.tick(bpm=128.0, energy=5, section="drop", genre="techno")

        assert r1["omni_triggered"] is True
        assert r2["omni_triggered"] is False  # rate-limited

    def test_from_config_disabled_when_no_visuals_section(self):
        """Config without a visuals block → engine disabled."""
        from agent.visuals.engine import VisualEngine
        fake_config = MagicMock(spec=[])  # no .visuals attribute
        engine = VisualEngine.from_config(fake_config)
        assert engine.enabled is False


# ---------------------------------------------------------------------------
# Omni stub path tests
# ---------------------------------------------------------------------------

class TestOmniStub:
    """Ensure the Omni stub path always returns a safe dict without crashing."""

    def test_generate_visual_returns_stub_when_genai_unavailable(self):
        import asyncio
        with patch("agent.visuals.omni._GENAI_AVAILABLE", False):
            from agent.visuals.omni import generate_visual, VisualPromptContext
            ctx = VisualPromptContext(genre="techno", energy=7, section="drop")
            result = asyncio.run(generate_visual(ctx))
        assert "ok" in result
        assert result["stub"] is True
        assert "error" in result
        assert result["error"]  # non-empty message

    def test_generate_visual_result_has_required_keys(self):
        import asyncio
        with patch("agent.visuals.omni._GENAI_AVAILABLE", False):
            from agent.visuals.omni import generate_visual, VisualPromptContext
            ctx = VisualPromptContext()
            result = asyncio.run(generate_visual(ctx))
        for key in ("ok", "stub", "concept", "video_url", "palette", "error"):
            assert key in result, f"Missing key: {key}"

    def test_visual_prompt_context_defaults(self):
        from agent.visuals.omni import VisualPromptContext
        ctx = VisualPromptContext()
        assert ctx.bpm == 128.0
        assert ctx.energy == 7
        assert ctx.section == "drop"

    def test_build_prompt_contains_genre_and_section(self):
        from agent.visuals.omni import _build_prompt, VisualPromptContext
        ctx = VisualPromptContext(genre="deep-house", section="breakdown", bpm=124.0)
        prompt = _build_prompt(ctx)
        assert "deep-house" in prompt
        assert "breakdown" in prompt
        assert "124.0" in prompt

    def test_build_prompt_includes_track_title_when_present(self):
        from agent.visuals.omni import _build_prompt, VisualPromptContext
        ctx = VisualPromptContext(track_title="Cosmos", artist="ARTBAT")
        prompt = _build_prompt(ctx)
        assert "Cosmos" in prompt
        assert "ARTBAT" in prompt

    def test_build_prompt_no_crash_on_empty_context(self):
        from agent.visuals.omni import _build_prompt, VisualPromptContext
        prompt = _build_prompt(VisualPromptContext())
        assert len(prompt) > 50

    def test_parse_response_extracts_concept_from_json(self):
        from agent.visuals.omni import _parse_response
        text = (
            'Some vivid description of fractals. '
            '{"concept": "fractal lightning", "motion": "burst", '
            '"dominant_color": "#FF00FF", "bg_color": "#000000"}'
        )
        result = _parse_response(text, "Fractal")
        assert result["ok"] is True
        assert result["concept"] == "fractal lightning"
        assert result["motion"] == "burst"
        assert result["stub"] is False

    def test_parse_response_falls_back_on_bad_json(self):
        from agent.visuals.omni import _parse_response
        text = "Some vivid description without JSON."
        result = _parse_response(text, "Midnight")
        assert result["ok"] is True
        # concept falls back to truncated text
        assert result["concept"]
        assert result["palette"] == "Midnight"
