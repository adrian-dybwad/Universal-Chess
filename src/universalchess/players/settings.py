"""Player and game settings management.

Encapsulates settings loading, saving, and access in a clean interface.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from universalchess.managers.game.coach_settings import (
    BASE_URL_BASE,
    API_KEY_BASE,
    MODEL_BASE,
    default_namespaced_settings,
    migrate_legacy,
    per_provider_keys,
    resolve_effective,
    writes_for_effective,
)
from universalchess.utils.settings_persistence import load_section, save_setting, clear_section

# Effective coach fields the board menu binds to. Setting one of these writes to
# the active provider's namespaced slot (see GameSettings.set); reading one
# resolves from that slot (see GameSettings.to_dict). Kept as a module constant
# so both paths agree on which keys are "effective, per-provider" aliases.
_COACH_EFFECTIVE_BASES = (API_KEY_BASE, MODEL_BASE, BASE_URL_BASE)


@dataclass
class PlayerSettings:
    """Settings for a single player.

    Handles loading from and saving to centaur.ini.

    Attributes:
        section: Section name in config file
        color: Color this player plays ('white' or 'black', only for player 1)
        type: Player type ('human', 'engine', 'lichess', 'hand_brain')
        name: Player name (for human type, empty = use default)
        engine: Engine name (for engine/human/hand_brain type)
        elo: Engine ELO level (for engine/human/hand_brain type)
        hand_brain_mode: Hand+Brain mode ('normal' or 'reverse')
    """

    section: str
    color: str = "white"
    type: str = "human"
    name: str = ""
    engine: str = "stockfish"
    elo: str = "Default"
    hand_brain_mode: str = "normal"
    _log: Optional[Any] = field(default=None, repr=False)

    def save(self, key: str) -> None:
        """Save a single setting to config file.

        Args:
            key: Setting key to save (must be an attribute name)
        """
        value = getattr(self, key)
        if save_setting(self.section, key, value):
            if self._log:
                self._log.debug(f"[Settings] Saved {self.section}.{key}={value}")
        else:
            if self._log:
                self._log.warning(f"[Settings] Error saving {self.section}.{key}={value}")

    def set(self, key: str, value: Any) -> None:
        """Set a setting value and save to config file.

        Args:
            key: Setting key to set
            value: New value
        """
        setattr(self, key, value)
        self.save(key)

    def to_dict(self) -> Dict[str, Any]:
        """Convert settings to a dictionary.

        Returns:
            Dict with all setting values
        """
        return {
            "color": self.color,
            "type": self.type,
            "name": self.name,
            "engine": self.engine,
            "elo": self.elo,
            "hand_brain_mode": self.hand_brain_mode,
        }

    @classmethod
    def load(
        cls,
        section: str,
        defaults: Dict[str, str],
        log=None,
    ) -> "PlayerSettings":
        """Load player settings from config file.

        Args:
            section: Section name in config file
            defaults: Default values for settings
            log: Optional logger for debug output

        Returns:
            PlayerSettings instance with loaded values
        """
        data = load_section(section, defaults)
        return cls(
            section=section,
            color=data.get("color", defaults.get("color", "white")),
            type=data.get("type", defaults.get("type", "human")),
            name=data.get("name", defaults.get("name", "")),
            engine=data.get("engine", defaults.get("engine", "stockfish")),
            elo=data.get("elo", defaults.get("elo", "Default")),
            hand_brain_mode=data.get("hand_brain_mode", defaults.get("hand_brain_mode", "normal")),
            _log=log,
        )

    def log_summary(self, label: str) -> None:
        """Log a summary of the settings.

        Args:
            label: Label for the log message (e.g., 'Player1')
        """
        if self._log:
            self._log.info(
                f"[Settings] {label}: type={self.type}, "
                f"color={self.color}, "
                f"name={self.name or '(default)'}, "
                f"engine={self.engine}, elo={self.elo}, "
                f"hb_mode={self.hand_brain_mode}"
            )


@dataclass
class GameSettings:
    """General game settings.

    Handles loading from and saving to centaur.ini.

    Attributes:
        section: Section name in config file
        time_control: Time per player in minutes (0 = disabled/untimed)
        analysis_mode: Enable analysis engine
        analysis_engine: Engine to use for position analysis
        show_board: Show chess board widget
        show_clock: Show clock/turn indicator widget
        show_analysis: Show analysis widget
        show_graph: Show history graph in analysis widget
        led_brightness: LED brightness level (1-10, default 5)
        pegasus_override_brightness: When True (default), the Pegasus emulator
            drives its LEDs from ``led_brightness`` instead of the intensity the
            DGT Chess app transmits. The app sends a fixed constant (it exposes no
            brightness control), so honoring it pins Pegasus brightness; the flag
            lets the app value be honored again if a future app varies it.
        chess_sprites: Identifier of the selected chesssprites_ sheet ("default"
            maps to chesssprites_default.bmp). Must be a real field so the
            Display > Board > Sprites selector can read the current selection via
            to_dict() and persist changes via set(); otherwise the menu always
            reads the default and cycling never advances.
        notation: Chess notation used for move history on the board and web
            ("figurine", "san", "lan", or "uci"). Defaults to figurine.
        coach_provider: Active AI coach service ("none", "openai", "anthropic",
            or "custom"). "none" (default) disables the move-review coach.
        coach_api_key_openai / _anthropic / _custom: API key stored per provider
            so switching providers preserves each provider's key (each a secret in
            centaur.ini, same handling as the Lichess token). The effective key
            for the active provider is exposed as ``coach_api_key`` via to_dict()
            and edited via ``set("coach_api_key", ...)``.
        coach_model_openai / _anthropic / _custom: Model id stored per provider;
            empty uses the provider default. Effective value exposed/edited as
            ``coach_model``.
        coach_base_url_custom: Base URL for the "custom" OpenAI-compatible coach
            endpoint (the built-in providers have fixed endpoints). Effective
            value exposed/edited as ``coach_base_url``.
        coach_id: Selected coach id from the coaches framework, or "auto" (default)
            to pick a coach by the opponent's Elo. Controls the coaching persona,
            independent of the provider/key.
    """

    section: str
    time_control: int = 0
    analysis_mode: bool = True
    analysis_engine: str = "stockfish"
    show_board: bool = True
    show_clock: bool = True
    show_analysis: bool = True
    show_graph: bool = True
    led_brightness: int = 5  # LED brightness 1-10
    pegasus_override_brightness: bool = True
    chess_sprites: str = "default"
    notation: str = "figurine"
    coach_provider: str = "none"
    coach_api_key_openai: str = ""
    coach_api_key_anthropic: str = ""
    coach_api_key_custom: str = ""
    coach_model_openai: str = ""
    coach_model_anthropic: str = ""
    coach_model_custom: str = ""
    coach_base_url_custom: str = ""
    coach_id: str = "auto"
    _log: Optional[Any] = field(default=None, repr=False)

    def _coach_storage(self) -> Dict[str, str]:
        """Raw per-agent coach mapping for resolution (namespaced + provider).

        Uses a default of "" for any namespaced key that has no attribute yet: the
        built-in agents have declared fields, but a user-added agent's slots exist
        only as dynamically set attributes, so ``getattr`` must not raise for one
        that was never written.
        """
        storage = {key: getattr(self, key, "") for key in per_provider_keys()}
        storage["coach_provider"] = self.coach_provider
        return storage

    def effective_coach(self) -> Dict[str, str]:
        """Effective coach provider/key/model/base_url for the active provider."""
        return resolve_effective(self._coach_storage())

    def save(self, key: str) -> None:
        """Save a single setting to config file.

        Args:
            key: Setting key to save (must be an attribute name)
        """
        value = getattr(self, key)
        if save_setting(self.section, key, value):
            if self._log:
                self._log.debug(f"[Settings] Saved {self.section}.{key}={value}")
        else:
            if self._log:
                self._log.warning(f"[Settings] Error saving {self.section}.{key}={value}")

    def set(self, key: str, value: Any) -> None:
        """Set a setting value and save to config file.

        For the effective coach fields (``coach_api_key``/``coach_model``/
        ``coach_base_url``) the value is routed to the *active* provider's
        namespaced slot, so editing a key only touches the current provider and
        every other provider's stored credentials are left intact. All other keys
        (including ``coach_provider`` and the namespaced keys themselves) are set
        directly.

        Args:
            key: Setting key to set
            value: New value
        """
        if key in _COACH_EFFECTIVE_BASES:
            writes = writes_for_effective(self.coach_provider, key, value)
            for namespaced, namespaced_value in writes.items():
                setattr(self, namespaced, namespaced_value)
                self.save(namespaced)
            return
        setattr(self, key, value)
        self.save(key)

    def to_dict(self) -> Dict[str, Any]:
        """Convert settings to a dictionary.

        The coach fields are exposed both as the namespaced per-provider keys (so
        the raw storage round-trips) and as the effective ``coach_api_key``/
        ``coach_model``/``coach_base_url`` for the active provider (what the board
        menu binds and the coach config builder reads).

        Returns:
            Dict with all setting values
        """
        data = {
            "time_control": self.time_control,
            "analysis_mode": self.analysis_mode,
            "analysis_engine": self.analysis_engine,
            "show_board": self.show_board,
            "show_clock": self.show_clock,
            "show_analysis": self.show_analysis,
            "show_graph": self.show_graph,
            "led_brightness": self.led_brightness,
            "pegasus_override_brightness": self.pegasus_override_brightness,
            "chess_sprites": self.chess_sprites,
            "notation": self.notation,
            "coach_provider": self.coach_provider,
            "coach_id": self.coach_id,
        }
        for key in per_provider_keys():
            data[key] = getattr(self, key, "")
        effective = self.effective_coach()
        data["coach_api_key"] = effective["coach_api_key"]
        data["coach_model"] = effective["coach_model"]
        data["coach_base_url"] = effective["coach_base_url"]
        return data

    @classmethod
    def load(
        cls,
        section: str,
        defaults: Dict[str, Any],
        log=None,
    ) -> "GameSettings":
        """Load game settings from config file.

        Args:
            section: Section name in config file
            defaults: Default values for settings
            log: Optional logger for debug output

        Returns:
            GameSettings instance with loaded values
        """
        # Ensure the per-provider coach keys (and the legacy flat keys used only
        # for one-time migration) are read from the section regardless of what the
        # caller listed in ``defaults`` -- load_section only reads keys it has a
        # default for. This keeps the per-provider layout encapsulated here.
        load_defaults = dict(defaults)
        load_defaults.setdefault("coach_provider", "none")
        load_defaults.setdefault("coach_id", "auto")
        for key in default_namespaced_settings():
            load_defaults.setdefault(key, "")
        for legacy in _COACH_EFFECTIVE_BASES:
            load_defaults.setdefault(legacy, "")

        data = load_section(section, load_defaults)
        # Fold any legacy single-slot values into the active provider's namespaced
        # slot so an upgraded config keeps its existing key under the right
        # provider. Pure/in-memory; the migrated values persist on the next save.
        coach = migrate_legacy(data)
        game = cls(
            section=section,
            time_control=data.get("time_control", defaults.get("time_control", 0)),
            analysis_mode=data.get("analysis_mode", defaults.get("analysis_mode", True)),
            analysis_engine=data.get("analysis_engine", defaults.get("analysis_engine", "stockfish")),
            show_board=data.get("show_board", defaults.get("show_board", True)),
            show_clock=data.get("show_clock", defaults.get("show_clock", True)),
            show_analysis=data.get("show_analysis", defaults.get("show_analysis", True)),
            show_graph=data.get("show_graph", defaults.get("show_graph", True)),
            led_brightness=data.get("led_brightness", defaults.get("led_brightness", 5)),
            pegasus_override_brightness=data.get(
                "pegasus_override_brightness",
                defaults.get("pegasus_override_brightness", True),
            ),
            chess_sprites=data.get("chess_sprites", defaults.get("chess_sprites", "default")),
            notation=data.get("notation", defaults.get("notation", "figurine")),
            coach_provider=coach.get("coach_provider", defaults.get("coach_provider", "none")),
            coach_api_key_openai=coach.get("coach_api_key_openai", ""),
            coach_api_key_anthropic=coach.get("coach_api_key_anthropic", ""),
            coach_api_key_custom=coach.get("coach_api_key_custom", ""),
            coach_model_openai=coach.get("coach_model_openai", ""),
            coach_model_anthropic=coach.get("coach_model_anthropic", ""),
            coach_model_custom=coach.get("coach_model_custom", ""),
            coach_base_url_custom=coach.get("coach_base_url_custom", ""),
            coach_id=data.get("coach_id", defaults.get("coach_id", "auto")),
            _log=log,
        )
        # Overlay any namespaced slots that are not declared dataclass fields --
        # user-added agents (discovered from the agents folder) contribute storage
        # keys beyond the built-in openai/anthropic/custom fields. Setting them as
        # instance attributes lets a user agent's credentials round-trip through
        # _coach_storage()/to_dict() without hardcoding every agent id here.
        for key in per_provider_keys():
            if not hasattr(game, key):
                setattr(game, key, coach.get(key, ""))
        return game

    def log_summary(self) -> None:
        """Log a summary of the settings."""
        if self._log:
            self._log.info(
                f"[Settings] Game: time={self.time_control} min, "
                f"analysis={self.analysis_mode}, "
                f"analysis_engine={self.analysis_engine}"
            )
            self._log.info(
                f"[Settings] Display: board={self.show_board}, "
                f"clock={self.show_clock}, analysis={self.show_analysis}, "
                f"graph={self.show_graph}, led_brightness={self.led_brightness}"
            )


@dataclass
class AllSettings:
    """Container for all game settings.

    Provides a single point of access for player and game settings.
    """

    player1: PlayerSettings
    player2: PlayerSettings
    game: GameSettings

    @classmethod
    def load(
        cls,
        player1_section: str,
        player2_section: str,
        game_section: str,
        player1_defaults: Dict[str, str],
        player2_defaults: Dict[str, str],
        game_defaults: Dict[str, Any],
        log=None,
    ) -> "AllSettings":
        """Load all settings from config file.

        Args:
            player1_section: Section name for player 1
            player2_section: Section name for player 2
            game_section: Section name for game settings
            player1_defaults: Default values for player 1
            player2_defaults: Default values for player 2
            game_defaults: Default values for game settings
            log: Optional logger for debug output

        Returns:
            AllSettings instance with all loaded values
        """
        player1 = PlayerSettings.load(player1_section, player1_defaults, log)
        player2 = PlayerSettings.load(player2_section, player2_defaults, log)
        game = GameSettings.load(game_section, game_defaults, log)

        return cls(player1=player1, player2=player2, game=game)

    def player_config_signature(self) -> tuple:
        """Game-defining player fields for both players.

        Captures the player configuration that a game's player objects are built
        from. When this signature differs from a running game's captured value,
        the configured players no longer match the ones in play (e.g. the engine
        was changed), so a new game must rebuild the players rather than reuse the
        stale ones. Excludes ``name`` (cosmetic: a rename must not abandon or
        rebuild a game).
        """
        p1, p2 = self.player1, self.player2
        return (
            p1.type, p1.color, p1.engine, p1.elo, p1.hand_brain_mode,
            p2.type, p2.color, p2.engine, p2.elo, p2.hand_brain_mode,
        )

    def log_summary(self) -> None:
        """Log a summary of all settings."""
        self.player1.log_summary("Player1")
        self.player2.log_summary("Player2")
        self.game.log_summary()

    def reset(
        self,
        player1_section: str,
        player2_section: str,
        game_section: str,
        player1_defaults: Dict[str, str],
        player2_defaults: Dict[str, str],
        game_defaults: Dict[str, Any],
    ) -> None:
        """Reset all settings to defaults.

        Clears the config file sections and reloads with defaults.

        Args:
            player1_section: Section name for player 1
            player2_section: Section name for player 2
            game_section: Section name for game settings
            player1_defaults: Default values for player 1
            player2_defaults: Default values for player 2
            game_defaults: Default values for game settings
        """
        clear_section(player1_section)
        clear_section(player2_section)
        clear_section(game_section)

        # Reload with defaults
        log = self.player1._log
        self.player1 = PlayerSettings.load(player1_section, player1_defaults, log)
        self.player2 = PlayerSettings.load(player2_section, player2_defaults, log)
        self.game = GameSettings.load(game_section, game_defaults, log)

