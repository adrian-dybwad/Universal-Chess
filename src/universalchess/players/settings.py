"""Player and game settings management.

Encapsulates settings loading, saving, and access in a clean interface.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from universalchess.utils.settings_persistence import load_section, save_setting, clear_section


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
        chess_sprites: Identifier of the selected chesssprites_ sheet ("default"
            maps to chesssprites_default.bmp). Must be a real field so the
            Display > Board > Sprites selector can read the current selection via
            to_dict() and persist changes via set(); otherwise the menu always
            reads the default and cycling never advances.
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
    chess_sprites: str = "default"
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
            "time_control": self.time_control,
            "analysis_mode": self.analysis_mode,
            "analysis_engine": self.analysis_engine,
            "show_board": self.show_board,
            "show_clock": self.show_clock,
            "show_analysis": self.show_analysis,
            "show_graph": self.show_graph,
            "led_brightness": self.led_brightness,
            "chess_sprites": self.chess_sprites,
        }

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
        data = load_section(section, defaults)
        return cls(
            section=section,
            time_control=data.get("time_control", defaults.get("time_control", 0)),
            analysis_mode=data.get("analysis_mode", defaults.get("analysis_mode", True)),
            analysis_engine=data.get("analysis_engine", defaults.get("analysis_engine", "stockfish")),
            show_board=data.get("show_board", defaults.get("show_board", True)),
            show_clock=data.get("show_clock", defaults.get("show_clock", True)),
            show_analysis=data.get("show_analysis", defaults.get("show_analysis", True)),
            show_graph=data.get("show_graph", defaults.get("show_graph", True)),
            led_brightness=data.get("led_brightness", defaults.get("led_brightness", 5)),
            chess_sprites=data.get("chess_sprites", defaults.get("chess_sprites", "default")),
            _log=log,
        )

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

