"""Load and validate config.yaml."""

import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class MixxxConfig:
    url: str = "http://localhost:7778"
    timeout: int = 5
    auto_start: bool = True
    binary: str = ""
    resource_path: str = ""
    settings_path: str = ""


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
    heartbeat_interval_seconds: float = 30.0
    pulse_interval_seconds: float = 5.0  # legacy, use heartbeat_interval_seconds
    poll_hz: int = 2
    max_errors: int = 10


@dataclass
class SetConfig:
    default_mood: str = "techno-deep"
    energy_max_jump: int = 2
    peak_max_consecutive: int = 3


@dataclass
class CapabilitiesConfig:
    allow_shell: bool = False


@dataclass
class Config:
    mixxx: MixxxConfig = field(default_factory=MixxxConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    transitions: TransitionConfig = field(default_factory=TransitionConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    set: SetConfig = field(default_factory=SetConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)


def _pick_fields(d: dict, cls: type) -> dict:
    fields = getattr(cls, "__dataclass_fields__", {})
    return {k: v for k, v in d.items() if k in fields}


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file."""
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    path = Path(path)

    if not path.exists():
        cfg = Config()
        env_key = os.environ.get("DJTRETA_LLM_API_KEY") or os.environ.get("LLM_API_KEY")
        if env_key:
            cfg.llm.api_key = env_key
        return cfg

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config()
    if "mixxx" in raw:
        cfg.mixxx = MixxxConfig(**_pick_fields(raw["mixxx"], MixxxConfig))
    if "llm" in raw:
        cfg.llm = LLMConfig(**_pick_fields(raw["llm"], LLMConfig))
    if "library" in raw:
        cfg.library = LibraryConfig(**_pick_fields(raw["library"], LibraryConfig))
    if "transitions" in raw:
        cfg.transitions = TransitionConfig(**_pick_fields(raw["transitions"], TransitionConfig))
    if "daemon" in raw:
        cfg.daemon = DaemonConfig(**_pick_fields(raw["daemon"], DaemonConfig))
    if "set" in raw:
        cfg.set = SetConfig(**_pick_fields(raw["set"], SetConfig))
    if "capabilities" in raw:
        cfg.capabilities = CapabilitiesConfig(**_pick_fields(raw["capabilities"], CapabilitiesConfig))

    env_key = os.environ.get("DJTRETA_LLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if env_key:
        cfg.llm.api_key = env_key

    return cfg
