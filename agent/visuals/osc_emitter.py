"""E6 — OSC emitter for DJ Treta's visual layer.

Sends beat/energy/section data over OSC (UDP) so any OSC-capable
visual tool (TouchDesigner, Max/MSP, resolume, custom p5.js OSC bridge)
can react to Treta's live audio features — parity with deadmau5's
TouchDesigner hook in Autopilot.

Dependency: ``python-osc`` (small; ``pip install python-osc``).
If python-osc is NOT installed, the emitter degrades gracefully to a
thin raw-UDP sender that implements only the address+args subset needed
here (no type-tag bundle, no sync, just basic OSC 1.1 messages).
Caller sees the same API either way.

OSC Message Schema
------------------
All messages are sent to the configured host:port on every tick.

Address                 Type    Description
/treta/beat             f       BPM of the active deck (e.g. 128.5)
/treta/energy           i       Energy level 1-10
/treta/section          s       Section name: intro|breakdown|buildup|drop|outro|main
/treta/palette          s       Palette name from E6 palette map (e.g. "Horizon")
/treta/key              s       Musical key string (e.g. "Am")
/treta/genre            s       Canonical genre slug (e.g. "melodic-techno")
/treta/beat_active      i       1 on a beat boundary, 0 otherwise (estimated)
/treta/bundle           s       JSON-encoded snapshot of all above fields (for
                                hosts that prefer a single message over many)

Configurable via config.yaml ``visuals:`` block::

    visuals:
      enabled: false          # master switch
      osc:
        host: "127.0.0.1"
        port: 7000
      omni:
        enabled: false

The emitter is instantiated once and re-used; it is safe to call
``emit()`` at heartbeat rate (every ~0.5–2 s).
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("dj-treta.visuals.osc")


# ---------------------------------------------------------------------------
# Minimal pure-stdlib OSC 1.1 packet builder
# (used only if python-osc is unavailable)
# ---------------------------------------------------------------------------

def _pad4(n: int) -> int:
    """Round up to next multiple of 4."""
    return (n + 3) & ~3


def _encode_str(s: str) -> bytes:
    """OSC string: null-terminated, padded to multiple of 4."""
    b = s.encode("utf-8") + b"\x00"
    return b.ljust(_pad4(len(b)), b"\x00")


def _encode_float(f: float) -> bytes:
    return struct.pack(">f", f)


def _encode_int(i: int) -> bytes:
    return struct.pack(">i", i)


def _build_osc_message(address: str, *args: Any) -> bytes:
    """Build a raw OSC 1.1 message from address + typed args.

    Supported arg types: int, float, str.
    """
    type_tags = ","
    encoded_args = b""
    for arg in args:
        if isinstance(arg, bool):
            arg = int(arg)
        if isinstance(arg, int):
            type_tags += "i"
            encoded_args += _encode_int(arg)
        elif isinstance(arg, float):
            type_tags += "f"
            encoded_args += _encode_float(arg)
        elif isinstance(arg, str):
            type_tags += "s"
            encoded_args += _encode_str(arg)
        else:
            # Coerce unknown types to string
            type_tags += "s"
            encoded_args += _encode_str(str(arg))

    return _encode_str(address) + _encode_str(type_tags) + encoded_args


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

@dataclass
class OSCEmitter:
    """Sends DJ Treta visual cues over OSC UDP.

    Instantiate once; call ``emit(...)`` on each tick.
    """

    host: str = "127.0.0.1"
    port: int = 7000
    _sock: socket.socket = field(init=False, repr=False, default=None)
    _has_python_osc: bool = field(init=False, repr=False, default=False)
    _osc_client: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._setup_transport()

    def _setup_transport(self) -> None:
        """Try python-osc first; fall back to raw UDP."""
        try:
            from pythonosc import udp_client  # type: ignore
            self._osc_client = udp_client.SimpleUDPClient(self.host, self.port)
            self._has_python_osc = True
            log.debug("OSCEmitter: using python-osc → %s:%d", self.host, self.port)
        except ImportError:
            # python-osc not installed — fall back to raw UDP.
            # pip install python-osc to remove this path.
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._has_python_osc = False
            log.debug(
                "OSCEmitter: python-osc not found, using raw UDP → %s:%d "
                "(install python-osc for full OSC 1.1 compatibility)",
                self.host, self.port,
            )

    def _send_raw(self, address: str, *args: Any) -> None:
        """Send one OSC message via raw UDP."""
        try:
            pkt = _build_osc_message(address, *args)
            self._sock.sendto(pkt, (self.host, self.port))
        except OSError as exc:
            log.warning("OSCEmitter raw send error (%s): %s", address, exc)

    def _send(self, address: str, *args: Any) -> None:
        """Send one OSC message, using python-osc or raw UDP."""
        try:
            if self._has_python_osc:
                self._osc_client.send_message(address, list(args) if len(args) != 1 else args[0])
            else:
                self._send_raw(address, *args)
        except Exception as exc:  # pragma: no cover
            log.warning("OSCEmitter send error (%s): %s", address, exc)

    def emit(
        self,
        bpm: float,
        energy: int,
        section: str,
        palette: str,
        key: str = "",
        genre: str = "",
        beat_active: bool = False,
    ) -> None:
        """Emit the full set of visual cues for one tick.

        Parameters
        ----------
        bpm:         BPM of the active deck.
        energy:      Energy level 1-10.
        section:     Section name from audio_analysis: intro|breakdown|
                     buildup|drop|outro|main.
        palette:     Palette name from the E6 palette map.
        key:         Musical key string, e.g. "Am".
        genre:       Canonical genre slug, e.g. "melodic-techno".
        beat_active: True if this tick is close to a beat boundary.
        """
        # Individual messages — each renderer subscribes to what it needs
        self._send("/treta/beat",        float(bpm))
        self._send("/treta/energy",      int(energy))
        self._send("/treta/section",     str(section))
        self._send("/treta/palette",     str(palette))
        self._send("/treta/key",         str(key))
        self._send("/treta/genre",       str(genre))
        self._send("/treta/beat_active", int(beat_active))

        # Bundle message — single-message hosts can parse this JSON
        bundle = json.dumps({
            "bpm": float(bpm),
            "energy": int(energy),
            "section": str(section),
            "palette": str(palette),
            "key": str(key),
            "genre": str(genre),
            "beat_active": beat_active,
            "ts": time.time(),
        })
        self._send("/treta/bundle", bundle)

    def close(self) -> None:
        """Release the socket (if raw UDP mode)."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
