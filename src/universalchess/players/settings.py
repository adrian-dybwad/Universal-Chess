"""Player and game settings management.

Encapsulates settings loading, saving, and access in a clean interface.
"""

from dataclasses import MISSING, dataclass, field, fields
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
from universalchess.utils.settings_persistence import (
    clear_section,
    has_setting,
    load_section,
    save_setting,
)

# Effective coach fields the board menu binds to. Setting one of these writes to
# the active provider's namespaced slot (see GameSettings.set); reading one
# resolves from that slot (see GameSettings.to_dict). Kept as a module constant
# so both paths agree on which keys are "effective, per-provider" aliases.
_COACH_EFFECTIVE_BASES = (API_KEY_BASE, MODEL_BASE, BASE_URL_BASE)

# Board position-analysis time presets. The board's AnalysisService searches each
# position for a fixed WALL-CLOCK time (UCI ``go movetime``), so the preset maps a
# user-facing name to seconds rather than a depth: a fixed depth can stall for many
# seconds in sharp positions on the slow Pi, whereas a time cap keeps per-move cost
# bounded. Longer time reaches greater depth -> a more accurate eval that lands
# closer to the web's fixed-depth number, at the cost of more CPU/battery. Stored as
# the preset NAME (not the seconds) so the seconds can be retuned later without
# invalidating saved configs. "quick" is the historical 0.3s default, so leaving the
# setting untouched changes nothing.
ANALYSIS_TIME_PRESETS: Dict[str, float] = {
    "quick": 0.3,
    "standard": 0.8,
    "deep": 2.0,
}
ANALYSIS_TIME_DEFAULT = "quick"


def analysis_time_seconds(preset: str) -> float:
    """Return the per-position analysis time in seconds for a preset name.

    An unknown/empty preset falls back to the default rather than raising: the
    value comes from persisted config that a downgrade/typo could leave stale, and
    the correct response is the safe historical default, not a crash on the game
    thread. Never fabricates an arbitrary number -- the fallback is an explicit,
    documented preset.

    Args:
        preset: Preset name (one of ``ANALYSIS_TIME_PRESETS``).

    Returns:
        Seconds per position for that preset, or the default preset's seconds.
    """
    return ANALYSIS_TIME_PRESETS.get(preset, ANALYSIS_TIME_PRESETS[ANALYSIS_TIME_DEFAULT])


# Section names for the two player slots in centaur.ini. Held here, beside the
# dataclass that reads them, because the board, the web app and the Lichess
# account store all address the same two sections: three copies of the literal
# meant a rename could reach some readers and not others.
PLAYER1_SECTION = "PlayerOne"
PLAYER2_SECTION = "PlayerTwo"


def default_player_name(player_num: int) -> str:
    """Slot-specific default display name for an unnamed human player.

    The default is per-slot ("Player 1"/"Player 2"), so it cannot live in the
    shared catalog as a single ``valueDefault`` literal (one node serves both
    slots). It is derived here from the player's slot number and supplied to the
    game (the PGN name) and to the board's Name row via the per-slot detail
    context's ``{fn:player_name}`` compute. The web supplies the same text as the
    Name field's placeholder from its per-slot context.
    """
    return f"Player {player_num}"


def _field_defaults(cls) -> Dict[str, Any]:
    """Default value for every persisted field of a settings dataclass.

    Single source of truth for a section's read set. ``load_section`` reads ONLY
    the keys present in the defaults it is given, so a field absent from that set
    is silently never read back -- it stays at its default no matter what is
    stored (this is exactly how ``led_brightness``/``notation`` stopped round-
    tripping once they were left out of the hand-maintained defaults dict).
    Deriving the read set from the dataclass makes that class of drift impossible:
    every declared field is read. Excludes ``section`` (required, no default) and
    private fields like ``_log``.
    """
    return {
        f.name: f.default
        for f in fields(cls)
        if not f.name.startswith("_") and f.name != "section" and f.default is not MISSING
    }


@dataclass
class PlayerSettings:
    """Settings for a single player.

    Handles loading from and saving to centaur.ini.

    Attributes:
        section: Section name in config file
        color: Color this player plays ('white' or 'black', only for player 1)
        type: Player type ('human', 'engine', 'hand_brain'). Leftover
            ``lichess`` is rewritten to ``human`` on load and on set; Lichess
            play is started from the lobby, which substitutes the pairing
            without writing slots.
        name: Player name (for human type, empty = use default)
        engine: Engine name (for engine/human/hand_brain type)
        elo: Engine ELO level (for engine/human/hand_brain type)
        hand_brain_mode: Hand+Brain mode ('normal' or 'reverse')
        think_time: Seconds the engine may think per move (engine type). Integer
            seconds because the settings loader infers type from the default and
            has no float branch; a float would round-trip as a string.

    A slot does not name an online account. The Lichess account belongs to the
    lobby (``GameSettings.lichess_account``) because it must exist whether or
    not a game was started from the lobby -- a lobby seek runs with a pairing
    derived for that game, which no saved slot describes. Upgraded configs keep
    a legacy ``account`` key in the player section; :meth:`AllSettings.load`
    reads it once to seed the lobby setting and nothing else consults it.
    """

    section: str
    color: str = "white"
    type: str = "human"
    name: str = ""
    engine: str = "stockfish"
    elo: str = "Default"
    hand_brain_mode: str = "normal"
    think_time: int = 5
    _log: Optional[Any] = field(default=None, repr=False)

    @property
    def slot(self) -> int:
        """Which player slot this is: 1 for the bottom of the board, else 2.

        The slot number is what the default name and the board's Name row are
        derived from, and it is only recoverable from the section this was loaded
        from. Anything that is not player one's section is player two, so an
        unrecognised section reads as slot 2 rather than raising on the game
        thread.
        """
        return 1 if self.section == PLAYER1_SECTION else 2

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

        ``think_time`` is coerced to ``int`` because the board menu writes the
        shared catalog's option values, which are strings (``"5"``), while the
        field is declared ``int`` and read back as ``int`` by ``load``. Coercing
        here keeps the in-memory value consistent with its declared type and with
        the persisted/reloaded value, so ``player_config_signature`` does not
        differ purely by type (``"5"`` vs ``5``) after a board edit.

        ``type`` of ``lichess`` is stored as ``human``: Lichess is not a Type
        picker choice, and persisting it would block Positions and make PLAY seek.

        Args:
            key: Setting key to set
            value: New value
        """
        if key == "think_time":
            value = int(value)
        if key == "type" and value == "lichess":
            # Lichess is not a Type picker choice. A leftover or stale POST
            # would block Positions and make PLAY seek. The lobby substitutes
            # Human vs Lichess at start without writing this field.
            value = "human"
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
            "think_time": self.think_time,
        }

    @classmethod
    def load(
        cls,
        section: str,
        defaults: Optional[Dict[str, Any]] = None,
        log=None,
    ) -> "PlayerSettings":
        """Load player settings from config file.

        The read set (and per-key fallback) is derived from the dataclass fields
        so every declared field is read from the section. Any ``defaults`` the
        caller passes override those per-key fallbacks (e.g. Player 2 defaults to
        black/engine), but can no longer determine *which* fields are read -- that
        is fixed by the dataclass, preventing a field from silently not loading.

        Args:
            section: Section name in config file
            defaults: Optional per-key default overrides (e.g. color/type).
            log: Optional logger for debug output

        Returns:
            PlayerSettings instance with loaded values
        """
        read_defaults = _field_defaults(cls)
        if defaults:
            read_defaults.update(defaults)
        data = load_section(section, read_defaults)
        return cls(
            section=section,
            color=data["color"],
            type=data["type"],
            name=data["name"],
            engine=data["engine"],
            elo=data["elo"],
            hand_brain_mode=data["hand_brain_mode"],
            think_time=data["think_time"],
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
        time_control: Base time per player in minutes for a simple sudden-death
            clock. Used as the fallback base time when no time_control_preset is
            set. Defaults to 5 so a fresh install is a real timed clock; untimed
            is selected via the "untimed" preset rather than a 0 here. A stored 0
            (from a pre-preset config) is still honored and resolves to an untimed
            control, preserving pre-existing configurations.
        time_control_preset: Selected named time control (see
            state/time_control.py PRESETS), the sentinel "custom" to build from
            the tc_custom_* fields, or "" to fall back to the legacy time_control
            minutes.
        tc_custom_base_seconds: Custom control base time per side, in seconds.
        tc_custom_increment_seconds: Custom Fischer increment added each move.
        tc_custom_delay_seconds: Custom per-move delay in seconds.
        tc_custom_delay_mode: Custom delay mode ("none", "simple", or
            "bronstein").
        tc_custom_asymmetric: When True, the custom control uses distinct per-
            side times (the tc_custom_black_* fields for Black); otherwise both
            sides use the white base/increment.
        tc_custom_black_base_seconds: Black's base time (seconds) when asymmetric.
        tc_custom_black_increment_seconds: Black's increment when asymmetric.
        engine_move_clock_delay_seconds: Grace seconds for the engine-move clock
            hand-off in timed local-engine games. When the engine shows its move
            it "presses its clock": its clock stops immediately, neither side
            counts for this many seconds, then the human's clock starts -- even
            though the move is still being physically transcribed. Default 1.
        analysis_mode: Enable analysis engine
        analysis_engine: Engine to use for position analysis
        analysis_time_preset: How long the board analyses each position, as a
            preset name ("quick"/"standard"/"deep", default "quick"). Maps to
            seconds via ``analysis_time_seconds``; longer is more accurate but
            uses more CPU/battery. Stored as the name so the seconds can be
            retuned without invalidating saved configs.
        deep_analysis: Allow the web review page to fetch a stronger engine
            (Stockfish 16 NNUE, roughly 39 MB) from jsDelivr for the position
            being viewed. Default False. Must be server-side rather than a
            browser preference: the Content-Security-Policy that permits the
            fetch is emitted by the web process, so the two have to agree.
        ponder: When True, engine players think on the opponent's time (UCI
            pondering). A             pondering engine runs in a dedicated process so its
            background search is never interrupted by analysis or the opponent;
            costs extra CPU/power. Default False.
        chess960: When True, each new game starts from a random Chess960 (Fischer
            Random) position. Engines receive UCI_Chess960 automatically (the
            board carries the chess960 flag) and 960 castling rules apply. The
            board only senses occupancy, which is identical to standard chess for
            every 960 start, so the target position is shown on the display and
            the physical setup is trusted. Default False.
        alert_queen_threat: Show the YOUR QUEEN warning (e-paper text, LED flash,
            red highlight, and web banner) when the side to move's own queen is
            attacked. Default True -- the board's long-standing behavior -- so the
            toggle only ever opts out. The CHECK warning has no equivalent setting:
            an unanswered check makes every other move illegal, so hiding it would
            let the player build an impossible position on the physical board.
        show_board: Show chess board widget
        show_clock: Show the untimed turn-indicator widget. Timed games always
            show the clock so remaining time is visible.
        show_analysis: Show analysis widget
        show_graph: Show history graph in analysis widget
        led_brightness: LED brightness level (1-10, default 5)
        pegasus_override_brightness: When True (default), the Pegasus emulator
            drives its LEDs from ``led_brightness`` instead of the intensity the
            DGT Chess app transmits. The app sends a fixed constant (it exposes no
            brightness control), so honoring it pins Pegasus brightness; the flag
            lets the app value be honored again if a future app varies it.
        chess_sprites: Identifier of the selected chesssprites_ sheet. Defaults to
            "default", the sentinel default sheet (chesssprites_default.png, the
            Cburnett set); the previous Mods artwork now ships as "original_mods".
            Because the id is stable, a persisted "default" silently resolves to
            the current default art, so the default style swaps with no migration.
            Must be a real field so the Display > Board > Sprites selector can read
            the current selection via to_dict() and persist changes via set();
            otherwise the menu always reads the default and cycling never advances.
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
        coach_multipv: Number of engine candidate lines (1-5) the AI coach is given
            for a reviewed move. 1 (default) sends no alternatives (current
            behavior); higher values run a multi-line analysis so the coach can
            reference better/alternative moves.
        text_size: Display text size ("small", "medium", or "large", default
            "medium") scaling the e-paper coach panel and move-list fonts. Medium
            leaves existing layouts unchanged.
    """

    section: str
    time_control: int = 5
    time_control_preset: str = ""
    tc_custom_base_seconds: int = 300
    tc_custom_increment_seconds: int = 0
    tc_custom_delay_seconds: int = 0
    tc_custom_delay_mode: str = "none"
    tc_custom_asymmetric: bool = False
    tc_custom_black_base_seconds: int = 300
    tc_custom_black_increment_seconds: int = 0
    engine_move_clock_delay_seconds: int = 1
    analysis_mode: bool = True
    analysis_engine: str = "stockfish"
    analysis_time_preset: str = ANALYSIS_TIME_DEFAULT
    deep_analysis: bool = False
    ponder: bool = False
    chess960: bool = False
    lichess_rated: bool = False
    lichess_use_dev: bool = False
    lichess_account: str = ""
    lichess_clock: str = "rapid_10_0"
    lichess_color: str = "random"
    alert_queen_threat: bool = True
    show_board: bool = True
    show_clock: bool = True
    show_analysis: bool = True
    show_graph: bool = True
    led_brightness: int = 5  # LED brightness 1-10
    pegasus_override_brightness: bool = True
    chess_sprites: str = "default"
    notation: str = "figurine"
    text_size: str = "medium"
    coach_provider: str = "none"
    coach_api_key_openai: str = ""
    coach_api_key_anthropic: str = ""
    coach_api_key_custom: str = ""
    coach_model_openai: str = ""
    coach_model_anthropic: str = ""
    coach_model_custom: str = ""
    coach_base_url_custom: str = ""
    coach_id: str = "auto"
    coach_multipv: int = 1
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
            "time_control_preset": self.time_control_preset,
            "tc_custom_base_seconds": self.tc_custom_base_seconds,
            "tc_custom_increment_seconds": self.tc_custom_increment_seconds,
            "tc_custom_delay_seconds": self.tc_custom_delay_seconds,
            "tc_custom_delay_mode": self.tc_custom_delay_mode,
            "tc_custom_asymmetric": self.tc_custom_asymmetric,
            "tc_custom_black_base_seconds": self.tc_custom_black_base_seconds,
            "tc_custom_black_increment_seconds": self.tc_custom_black_increment_seconds,
            "engine_move_clock_delay_seconds": self.engine_move_clock_delay_seconds,
            "analysis_mode": self.analysis_mode,
            "analysis_engine": self.analysis_engine,
            "analysis_time_preset": self.analysis_time_preset,
            "deep_analysis": self.deep_analysis,
            "ponder": self.ponder,
            "chess960": self.chess960,
            "lichess_rated": self.lichess_rated,
            "lichess_use_dev": self.lichess_use_dev,
            "lichess_account": self.lichess_account,
            "lichess_clock": self.lichess_clock,
            "lichess_color": self.lichess_color,
            "alert_queen_threat": self.alert_queen_threat,
            "show_board": self.show_board,
            "show_clock": self.show_clock,
            "show_analysis": self.show_analysis,
            "show_graph": self.show_graph,
            "led_brightness": self.led_brightness,
            "pegasus_override_brightness": self.pegasus_override_brightness,
            "chess_sprites": self.chess_sprites,
            "notation": self.notation,
            "text_size": self.text_size,
            "coach_provider": self.coach_provider,
            "coach_id": self.coach_id,
            "coach_multipv": self.coach_multipv,
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
        defaults: Optional[Dict[str, Any]] = None,
        log=None,
    ) -> "GameSettings":
        """Load game settings from config file.

        The read set (and per-key fallback) is derived from the dataclass fields
        so every persisted field is read from the section -- ``load_section`` only
        reads keys present in its defaults, so a field left out of the read set was
        silently never loaded (led_brightness/notation/pegasus_override_brightness
        each showed the hardcoded default no matter what was stored). Any
        ``defaults`` the caller passes override the per-key fallbacks but cannot
        remove a field from the read set.

        Args:
            section: Section name in config file
            defaults: Optional per-key default overrides.
            log: Optional logger for debug output

        Returns:
            GameSettings instance with loaded values
        """
        load_defaults = _field_defaults(cls)
        if defaults:
            load_defaults.update(defaults)
        # The legacy flat coach keys (one-time migration only) and any user-added
        # agent's namespaced slots are not declared dataclass fields, so add them
        # to the read set explicitly; the built-in namespaced keys already come
        # from the dataclass above.
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
            time_control=data["time_control"],
            time_control_preset=data["time_control_preset"],
            tc_custom_base_seconds=data["tc_custom_base_seconds"],
            tc_custom_increment_seconds=data["tc_custom_increment_seconds"],
            tc_custom_delay_seconds=data["tc_custom_delay_seconds"],
            tc_custom_delay_mode=data["tc_custom_delay_mode"],
            tc_custom_asymmetric=data["tc_custom_asymmetric"],
            tc_custom_black_base_seconds=data["tc_custom_black_base_seconds"],
            tc_custom_black_increment_seconds=data["tc_custom_black_increment_seconds"],
            engine_move_clock_delay_seconds=data["engine_move_clock_delay_seconds"],
            analysis_mode=data["analysis_mode"],
            analysis_engine=data["analysis_engine"],
            analysis_time_preset=data["analysis_time_preset"],
            deep_analysis=data["deep_analysis"],
            ponder=data["ponder"],
            chess960=data["chess960"],
            lichess_rated=data["lichess_rated"],
            lichess_use_dev=data["lichess_use_dev"],
            lichess_account=data["lichess_account"],
            lichess_clock=data["lichess_clock"],
            lichess_color=data["lichess_color"],
            alert_queen_threat=data["alert_queen_threat"],
            show_board=data["show_board"],
            show_clock=data["show_clock"],
            show_analysis=data["show_analysis"],
            show_graph=data["show_graph"],
            led_brightness=data["led_brightness"],
            pegasus_override_brightness=data["pegasus_override_brightness"],
            chess_sprites=data["chess_sprites"],
            notation=data["notation"],
            text_size=data["text_size"],
            coach_provider=coach.get("coach_provider", "none"),
            coach_api_key_openai=coach.get("coach_api_key_openai", ""),
            coach_api_key_anthropic=coach.get("coach_api_key_anthropic", ""),
            coach_api_key_custom=coach.get("coach_api_key_custom", ""),
            coach_model_openai=coach.get("coach_model_openai", ""),
            coach_model_anthropic=coach.get("coach_model_anthropic", ""),
            coach_model_custom=coach.get("coach_model_custom", ""),
            coach_base_url_custom=coach.get("coach_base_url_custom", ""),
            coach_id=data["coach_id"],
            coach_multipv=data["coach_multipv"],
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


LEGACY_PLAYER_ACCOUNT_KEY = "account"


def _adopt_legacy_lichess_account(player1, player2, game, game_section: str) -> None:
    """Seed the lobby account from a pre-lobby config's Lichess player slot.

    The Lichess account used to be stored on whichever player slot was set to
    Lichess. Boards upgrading from that layout must keep playing as the account
    they were bound to rather than silently falling back to the first
    credential, so the slot's value is read once here.

    Only an absent ``lichess_account`` is seeded. The legacy key is left in the
    player section rather than deleted, so treating an empty stored value as
    "unset" would resurrect the old account on every load and undo a user who
    has since chosen Default. Player 1 is preferred, matching the slot the lobby
    authenticated as when both were Lichess, and a value left behind on a slot
    that is no longer Lichess is ignored as stale.

    In-memory until the account is written: a newly adopted value is persisted
    immediately so a following rewrite of leftover ``type=lichess`` to human
    cannot lose the account on the next boot (adoption only reads Lichess slots).
    """
    if has_setting(game_section, "lichess_account"):
        return
    for player in (player1, player2):
        if player.type != "lichess":
            continue
        legacy = load_section(
            player.section, {LEGACY_PLAYER_ACCOUNT_KEY: ""}
        ).get(LEGACY_PLAYER_ACCOUNT_KEY, "")
        if legacy:
            game.lichess_account = legacy
            game.save("lichess_account")
            return


def _migrate_lichess_slot_types(player1, player2) -> None:
    """Rewrite leftover ``type=lichess`` slots to human and persist.

    Lichess is not a Players Type. A leftover slot blocks Positions and makes
    PLAY post a seek. The lobby substitutes Human vs Lichess at start without
    writing the slots, so a saved ``lichess`` type is only a leftover from
    before the picker lost that option.
    """
    for player in (player1, player2):
        if player.type == "lichess":
            player.type = "human"
            player.save("type")


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
        player1_defaults: Optional[Dict[str, Any]] = None,
        player2_defaults: Optional[Dict[str, Any]] = None,
        game_defaults: Optional[Dict[str, Any]] = None,
        log=None,
    ) -> "AllSettings":
        """Load all settings from config file.

        The per-section read sets derive from the dataclass fields; the *_defaults
        arguments are optional per-key overrides (e.g. Player 2 defaults to
        black/engine), not the source of which fields are read.

        Args:
            player1_section: Section name for player 1
            player2_section: Section name for player 2
            game_section: Section name for game settings
            player1_defaults: Optional per-key default overrides for player 1
            player2_defaults: Optional per-key default overrides for player 2
            game_defaults: Optional per-key default overrides for game settings
            log: Optional logger for debug output

        Returns:
            AllSettings instance with all loaded values
        """
        player1 = PlayerSettings.load(player1_section, player1_defaults, log)
        player2 = PlayerSettings.load(player2_section, player2_defaults, log)
        game = GameSettings.load(game_section, game_defaults, log)
        _adopt_legacy_lichess_account(player1, player2, game, game_section)
        _migrate_lichess_slot_types(player1, player2)

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
            p1.type, p1.color, p1.engine, p1.elo, p1.hand_brain_mode, p1.think_time,
            p2.type, p2.color, p2.engine, p2.elo, p2.hand_brain_mode, p2.think_time,
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
        player1_defaults: Optional[Dict[str, Any]] = None,
        player2_defaults: Optional[Dict[str, Any]] = None,
        game_defaults: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Reset all settings to defaults.

        Clears the config file sections and reloads with defaults.

        Args:
            player1_section: Section name for player 1
            player2_section: Section name for player 2
            game_section: Section name for game settings
            player1_defaults: Optional per-key default overrides for player 1
            player2_defaults: Optional per-key default overrides for player 2
            game_defaults: Optional per-key default overrides for game settings
        """
        clear_section(player1_section)
        clear_section(player2_section)
        clear_section(game_section)

        # Reload with defaults
        log = self.player1._log
        self.player1 = PlayerSettings.load(player1_section, player1_defaults, log)
        self.player2 = PlayerSettings.load(player2_section, player2_defaults, log)
        self.game = GameSettings.load(game_section, game_defaults, log)

