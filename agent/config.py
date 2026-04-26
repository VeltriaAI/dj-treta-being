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
class PlannerConfig:
    replan_every_n_tracks: int = 2
    download_new_tracks: int = 3
    generate_new_tracks: int = 1
    min_play_time_seconds: int = 180


@dataclass
class SetsConfig:
    auto_mode: bool = True
    default_duration_minutes: int = 120
    recording_dir: str = "~/Music/DJTreta/recordings"
    local_recording: bool = False


@dataclass
class RelayConfig:
    enabled: bool = False
    server_url: str = ""  # wss://your-relay-host/ws/relay — required when enabled
    token: str = ""  # set via env DJTRETA_RELAY_TOKEN
    push_hz: int = 3


@dataclass
class SourcesConfig:
    youtube: bool = True
    treta_originals: bool = True


@dataclass
class BroadcastConfig:
    auto_start: bool = True


@dataclass
class ProducerConfig:
    enabled: bool = True
    model: str = "lyria-3-pro-preview"
    vertex_project: str = ""  # set in config.yaml or via DJTRETA_VERTEX_PROJECT
    vertex_location: str = "us-central1"  # safe default region
    default_duration_seconds: int = 180
    genre_dir: str = "ai-generated"
    # v8 Phase 6: daily guardrails so an over-eager producer_need signal
    # can't burn through Lyria 3 credits overnight.
    max_per_day: int = 4
    cost_cap_per_day_usd: float = 2.00


@dataclass
class KnowledgeConfig:
    enabled: bool = False
    data_dir: str = "~/Music/DJTreta/knowledge"  # consistent with library.music_dir


@dataclass
class EvolutionConfig:
    enabled: bool = False
    reflect_every_n_tracks: int = 5
    auto_evolve: bool = False
    max_evolve_per_day: int = 2
    max_budget_per_evolve_usd: float = 0.50
    claude_binary: str = "~/.local/bin/claude"
    require_tests: bool = True


@dataclass
class Config:
    mixxx: MixxxConfig = field(default_factory=MixxxConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    transitions: TransitionConfig = field(default_factory=TransitionConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    set: SetConfig = field(default_factory=SetConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    sets: SetsConfig = field(default_factory=SetsConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    broadcast: BroadcastConfig = field(default_factory=BroadcastConfig)
    producer: ProducerConfig = field(default_factory=ProducerConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)


def _pick_fields(d: dict, cls: type) -> dict:
    fields = getattr(cls, "__dataclass_fields__", {})
    return {k: v for k, v in d.items() if k in fields}


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file. Also loads .env if present."""
    # Load .env file if it exists (before anything else)
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    if path is None:
        # Look for config.local.yaml first (gitignored, user-specific), fall
        # back to config.yaml (tracked, repo default). Lets contributors
        # override without diffing the canonical config.
        repo_root = Path(__file__).parent.parent
        local = repo_root / "config.local.yaml"
        path = local if local.exists() else (repo_root / "config.yaml")
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
    if "planner" in raw:
        cfg.planner = PlannerConfig(**_pick_fields(raw["planner"], PlannerConfig))
    if "sets" in raw:
        cfg.sets = SetsConfig(**_pick_fields(raw["sets"], SetsConfig))
    if "relay" in raw:
        cfg.relay = RelayConfig(**_pick_fields(raw["relay"], RelayConfig))
    if "broadcast" in raw:
        cfg.broadcast = BroadcastConfig(**_pick_fields(raw["broadcast"], BroadcastConfig))
    if "producer" in raw:
        cfg.producer = ProducerConfig(**_pick_fields(raw["producer"], ProducerConfig))
    if "sources" in raw:
        cfg.sources = SourcesConfig(**_pick_fields(raw["sources"], SourcesConfig))
    if "knowledge" in raw:
        cfg.knowledge = KnowledgeConfig(**_pick_fields(raw["knowledge"], KnowledgeConfig))
    if "evolution" in raw:
        cfg.evolution = EvolutionConfig(**_pick_fields(raw["evolution"], EvolutionConfig))

    # Env-var overrides — keep secrets and machine-specific values out of code.
    env_key = os.environ.get("DJTRETA_LLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if env_key:
        cfg.llm.api_key = env_key

    relay_token = os.environ.get("DJTRETA_RELAY_TOKEN")
    if relay_token:
        cfg.relay.token = relay_token

    # Mixxx + LLM endpoints — let env override config.yaml so the same
    # repo can run against localhost / Docker / a remote service.
    mixxx_url = os.environ.get("DJTRETA_MIXXX_URL")
    if mixxx_url:
        cfg.mixxx.url = mixxx_url

    llm_base = os.environ.get("DJTRETA_LLM_API_BASE")
    if llm_base:
        cfg.llm.api_base = llm_base

    # GCP / Vertex AI — required for Producer (Lyria 3) + LiteLLM proxy backend.
    vertex_project = os.environ.get("DJTRETA_VERTEX_PROJECT")
    if vertex_project:
        cfg.producer.vertex_project = vertex_project

    vertex_location = os.environ.get("DJTRETA_VERTEX_LOCATION")
    if vertex_location:
        cfg.producer.vertex_location = vertex_location

    return cfg
