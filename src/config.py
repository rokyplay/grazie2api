"""Configuration loader: reads config.yaml and exposes a Settings dataclass."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("grazie2api.config")

_CONFIG_SEARCH_PATHS = [
    Path(__file__).parent.parent / "config.yaml",
]


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8800
    max_request_body_bytes: int = 10 * 1024 * 1024


@dataclass
class AuthConfig:
    client_id: str = "ide"
    scope: str = "openid offline_access r_ide_auth"
    code_challenge_method: str = "S256"
    client_info: str = "eyJwcm9kdWN0IjoiUFkiLCJidWlsZCI6IjI2MS4yMjE1OC4zNDAifQ"


@dataclass
class UrlsConfig:
    hub_base: str = "https://oauth.account.jetbrains.com"
    jb_base: str = "https://account.jetbrains.com"
    ai_base: str = "https://api.jetbrains.ai"

    @property
    def hub_token_url(self) -> str:
        return f"{self.hub_base}/api/rest/oauth2/token"

    @property
    def register_url(self) -> str:
        return f"{self.ai_base}/auth/jetbrains-jwt/register"

    @property
    def jwt_url(self) -> str:
        return f"{self.ai_base}/auth/jetbrains-jwt/provide-access/license/v2"

    @property
    def chat_url(self) -> str:
        return f"{self.ai_base}/user/v5/llm/chat/stream/v7"

    @property
    def profiles_url(self) -> str:
        return f"{self.ai_base}/user/v5/llm/profiles/v3"

    @property
    def quota_url(self) -> str:
        return f"{self.ai_base}/user/v5/quota/get"


@dataclass
class GrazieConfig:
    agent_name: str = "aia:idea"
    agent_version: str = "251.26094.80.11:251.25410.109"
    chat_prompt: str = "ij.chat.request.new-chat-on-start"

    @property
    def agent_json(self) -> str:
        return json.dumps({"name": self.agent_name, "version": self.agent_version})


@dataclass
class TokensConfig:
    jwt_margin_seconds: int = 300
    access_token_margin_seconds: int = 120


@dataclass
class CredentialsConfig:
    cooldown_seconds: int = 600
    callback_port_start: int = 19280
    callback_port_end: int = 19290


@dataclass
class QuotaConfig:
    refresh_interval_seconds: int = 300


@dataclass
class ModelsConfig:
    profiles_cache_ttl_seconds: int = 3600
    profile_exclude_keywords: list[str] = field(default_factory=lambda: ["embedding", "instruct"])
    fallback_profiles: list[str] = field(default_factory=lambda: [
        "anthropic-claude-4-6-sonnet",
        "anthropic-claude-4-5-sonnet",
        "openai-gpt-4o",
        "google-chat-gemini-pro-2.5",
    ])
    alias_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class PortalConfig:
    enabled: bool = True
    max_credentials_per_key: int = 5


@dataclass
class Settings:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    urls: UrlsConfig = field(default_factory=UrlsConfig)
    grazie: GrazieConfig = field(default_factory=GrazieConfig)
    tokens: TokensConfig = field(default_factory=TokensConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    strategy: str = "round_robin"
    api_key: str = ""  # If set, all endpoints require this key via Bearer token or x-api-key header
    accounts: list[dict] = field(default_factory=list)  # [{email, password}]
    system_api_keys: list[dict] = field(default_factory=list)
    # Format: [{"id": "system-rok", "identity": "rok", "key": "sk-xxx", "tier": "system", "enabled": true}]

    # Derived paths
    config_home: Path = field(default_factory=lambda: Path.home() / ".grazie2api")
    script_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent.resolve())

    @property
    def multi_credentials_file(self) -> Path:
        return self.config_home / "credentials.json"

    @property
    def quota_cache_file(self) -> Path:
        return self.config_home / "quota-cache.json"

    @property
    def main_db_file(self) -> Path:
        return self.config_home / "main.db"

    @property
    def stats_db_file(self) -> Path:
        return self.config_home / "stats.db"

    @property
    def portal_db_file(self) -> Path:
        return self.config_home / "portal.db"

    @property
    def legacy_credentials_file(self) -> Path:
        return self.script_dir / "jb-credentials.json"

    def ensure_config_home(self) -> None:
        self.config_home.mkdir(parents=True, exist_ok=True)
        try:
            self.config_home.chmod(0o700)
        except OSError:
            pass  # Windows ACL may not support POSIX chmod


def _apply_dict(dc: Any, data: dict) -> None:
    """Apply dict values to a dataclass instance."""
    for k, v in data.items():
        if hasattr(dc, k):
            current = getattr(dc, k)
            if isinstance(current, (ServerConfig, AuthConfig, UrlsConfig, GrazieConfig,
                                    TokensConfig, CredentialsConfig, QuotaConfig, ModelsConfig, PortalConfig)):
                if isinstance(v, dict):
                    _apply_dict(current, v)
            else:
                setattr(dc, k, v)


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from a YAML config file.

    Search order:
      1. Explicit config_path argument
      2. _CONFIG_SEARCH_PATHS
      3. Defaults
    """
    settings = Settings()

    path: Path | None = config_path
    if path is None:
        for candidate in _CONFIG_SEARCH_PATHS:
            if candidate.exists():
                path = candidate
                break

    if path is not None and path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _apply_dict(settings, raw)
                log.info("Loaded config from %s", path)
        except Exception as e:
            log.warning("Failed to load config from %s: %s (using defaults)", path, e)
    else:
        log.info("No config.yaml found, using defaults")

    settings.ensure_config_home()
    return settings
