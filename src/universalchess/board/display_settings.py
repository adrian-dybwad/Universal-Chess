"""Read the ``[display]`` settings, identically in both processes.

The board process and the web process both read these: the board to drive the
panel, the web to show and edit the display-tuning card. They must agree on what
a stored value means, and they did not agree by construction -- each had its own
copy of the parser, and one of the two carried a docstring promising it matched
the other.

Kept free of any driver import so the web process can read the settings without
pulling in the GPIO-dependent driver modules.
"""

TRUE_VALUES = ('1', 'true', 'on', 'yes')


def read_flag(name: str, default: bool = False) -> bool:
    """Return whether a ``[display]`` boolean opt-in is set.

    Covers the three_color switch, the update-batching option and the
    partial-only switch. A key no longer read, such as ``high_contrast`` from
    the removed voltage override, stays in the file and has no effect.

    ``default`` is the value when the key is absent, so a never-configured board
    gets the intended shipped behaviour (e.g. batching on).
    """
    from universalchess.board.settings import Settings
    value = Settings.read('display', name, 'True' if default else 'False')
    return str(value).strip().lower() in TRUE_VALUES


def read_waveform_profile_key() -> str:
    """Return the stored waveform-profile key, unresolved.

    One key is shared by both controllers; each driver resolves it against its
    own profile family (``waveform_profiles.get_profile(key, controller)``),
    falling back to that controller's verified default when the stored key
    belongs to the other controller -- as it does after a panel swap -- so a
    working panel is never left without a waveform.
    """
    from universalchess.board.settings import Settings
    return str(Settings.read('display', 'waveform_profile', '')).strip()


def read_panel_key() -> str:
    """Return the stored panel-model key, or ``auto`` when unset or unknown.

    ``auto`` is the Centaur V1/V2 probe. An explicit Good Display key selects
    that module's driver and does not fall through to another controller.
    Unknown values degrade to ``auto`` so a stale key cannot select a driver
    that was removed.
    """
    from universalchess.board.settings import Settings
    from universalchess.epaper.framework.waveshare import panel_models
    stored = str(Settings.read('display', 'panel', panel_models.PANEL_AUTO)).strip()
    if panel_models.is_known_panel(stored):
        return stored
    return panel_models.PANEL_AUTO
