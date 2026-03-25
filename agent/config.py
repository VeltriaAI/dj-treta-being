"""Load and validate config.yaml."""

import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class MixxxConfig:
    url: str = "http://localhost:7778"
    timeout: int = 5


@dataclass
class LLMConfig:
    model: str = "openai/gemini-3-flash"
    api_base: str = "http://localhost:4000"
    api_key: str = ""
    temperature: float = 0.7
    timeout: int = 30


@dataclass
class LibraryConfig:
    music_dir: str = "~/Music/DJTreta"

    @property
    def music_path(self) -> Path:
        return Path(self.music_dir).expanduser()


@dataclass
class TransitionConfig:
    lookahead_seconds: int = 120
    default_duration: int = 60
    fps: int = 20


@dataclass
class DaemonConfig:
    poll_hz: int = 2
    max_errors: int = 10


@dataclass
class SetConfig:
    default_mood: str = "techno-deep"
    energy_max_jump: int = 2
    peak_max_consecutive: int = 3


@dataclass
class Config:
    mixxx: MixxxConfig = field(default_factory=MixxxConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    transitions: TransitionConfig = field(default_factory=TransitionConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    set: SetConfig = field(default_factory=SetConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file."""
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    path = Path(path)

    if not path.exists():
        return Config()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config()
    if "mixxx" in raw:
        cfg.mixxx = MixxxConfig(**raw["mixxx"])
    if "llm" in raw:
        cfg.llm = LLMConfig(**raw["llm"])
    if "library" in raw:
        cfg.library = LibraryConfig(**raw["library"])
    if "transitions" in raw:
        cfg.transitions = TransitionConfig(**raw["transitions"])
    if "daemon" in raw:
        cfg.daemon = DaemonConfig(**raw["daemon"])
    if "set" in raw:
        cfg.set = SetConfig(**raw["set"])

    return cfg
