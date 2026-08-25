"""Compose an engine profile's display label from its own option values.

A profile is a section of options in ``config/engines/<engine>.uci``. Its section
name used to be its label as well as its identity, which is why the label could
lie: a section named ``1000 ELO`` whose ``UCI_Elo`` had been edited to 1400 was
still called ``1000 ELO``, and the only remedy was to rename the section (and
every setting referring to it). Names are identities now, so the label is
projected from the values instead -- there is one copy of the Elo, and nothing to
drift.

The projection is an ordered list of option keys. How each key renders is derived
from the probed schema rather than declared per engine, which is what lets an
engine nobody curated get a truthful label:

* a file-backed option renders as its basename, preferring an embedded rating
  (``maia-1500.pb.gz`` -> ``1500 ELO``, ``Defender.txt`` -> ``Defender``)
* an ``int`` renders as its number, with the unit the option registry declares
  (``UCI_Elo`` -> ``1400 ELO``), because a bare number does not say what it
  measures
* a ``bool`` renders its name when on and nothing when off
* an ``info`` option is display-only and can never be a label key
* a gated option (:attr:`ProfileField.requires`) renders nothing while its gate
  is off, because the engine ignores the value: an uncapped profile must not
  advertise the Elo it is not playing at

Composing rather than naming is also what makes two axes representable at once:
a Rodent personality capped at an Elo is ``Defender: 1700 ELO``.

This module holds no engine or filesystem knowledge and does no I/O, so it is
usable from the web API, the on-device menu, and the PGN writer alike. Callers
supply the schema fields and the profile's values; a projection with no terms
returns the empty string, leaving what to show in its place to the caller (the
picker says ``Default (Unlimited)``, the game card says ``Unlimited``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "LabelProjection",
    "ProfileLabel",
    "TERM_SEPARATOR",
    "file_term",
    "rating_in_filename",
    "term_for",
    "render_terms",
    "render_label",
    "selectable_keys",
    "resolve_keys",
]

# Terms are joined with a colon so a composed label reads as a qualification of
# the first axis ("Defender: 1700 ELO") rather than as an unordered set.
TERM_SEPARATOR = ": "

# A rating embedded in a file name (Maia's nets, Rodent's rated personalities).
# Three or four digits: engine files are not named for two-digit ratings, and
# accepting longer runs would read a date or a version as a rating.
_RATING_IN_FILENAME = re.compile(r"(\d{3,4})")

# Values ConfigParser writes for a false bool. Only an explicit false switches a
# gate off: an engine that does not advertise the gate at all ignores nothing, so
# an absent value must leave the gated option rendering.
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class ProfileLabel:
    """The ordered option keys whose values compose an engine's profile labels.

    Declared on ``EngineDefinition`` for engines whose strength axis cannot be
    derived from the probe alone (an engine advertising several file-backed
    options gives no way to tell which one selects strength). Everything else
    uses the axes the schema exposes, so this stays the exception.

    Attributes:
        keys: Option names in the order their terms appear in the label. Spelling
            need not match the probe exactly -- :func:`resolve_keys` reconciles
            it -- and keys the installed engine does not advertise are dropped.
    """

    keys: Tuple[str, ...]


def rating_in_filename(value: str) -> Optional[int]:
    """Return the rating a file name embeds, or None when it embeds none.

    Distinguishes the two kinds of file-backed axis an engine can expose, which
    are read very differently: Maia's nets are named for the rating they mimic,
    so that axis *is* a strength ladder, while Rodent's personalities are named
    for how they play and say nothing about strength. Seeding needs the
    difference (a rated axis has a meaningful middle rung; an unrated one does
    not), and so does the label.
    """
    if not value:
        return None
    base = os.path.basename(str(value).strip())
    match = _RATING_IN_FILENAME.search(base)
    return int(match.group(1)) if match else None


def file_term(value: str) -> str:
    """Render a file-backed option's value as a label term.

    A rating in the file name is what the file means -- Maia's nets are named for
    the rating they mimic -- so it is preferred over the bare stem. Everything
    else renders as the basename without its suffixes, which is how the files are
    talked about (``Defender.txt`` is "Defender").

    Never returns the path: the stored value is absolute, and the label reaches
    the strength picker, the game card and the PGN.
    """
    if not value:
        return ""
    base = os.path.basename(str(value).strip())
    if not base:
        return ""
    rating = rating_in_filename(base)
    if rating is not None:
        return f"{rating} ELO"
    return base.split(".")[0] or base


def _is_file_backed(field) -> bool:
    """Return whether ``field`` selects a file rather than an opaque value.

    The schema builder marks an enumerated file option as a ``select`` that also
    accepts custom values (the enumerated list is a convenience, not a
    constraint), which is the same marker it groups such options by. A combo
    option the engine declared has fixed values and is not a file.
    """
    return field.type == "select" and field.allow_custom


def term_for(field, value: Optional[str]) -> Optional[str]:
    """Render one label term for ``field`` holding ``value``, or None for no term.

    Returns None -- contributing nothing to the label -- when the profile does not
    set the option, when the field is display-only, and when a bool is off.
    Suppression by a gated option's gate is applied by :func:`render_terms`,
    which is the only place that can see the other values.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if field.type == "info":
        return None
    if field.type == "bool":
        return None if text.casefold() in _FALSE_VALUES else field.label
    if _is_file_backed(field):
        return file_term(text) or None
    if field.type == "int" and field.unit:
        return f"{text} {field.unit}"
    return text


def _by_casefolded_key(items: Iterable[Tuple[str, object]]) -> Dict[str, object]:
    """Index ``items`` by casefolded key, keeping the first spelling seen."""
    indexed: Dict[str, object] = {}
    for key, item in items:
        folded = key.casefold()
        if folded not in indexed:
            indexed[folded] = item
    return indexed


def render_terms(
    keys: Sequence[str],
    fields: Sequence,
    values: Mapping[str, str],
) -> List[str]:
    """Render the label terms for ``keys``, in the order given.

    Keys the schema does not advertise are skipped rather than rendered raw: a
    config outlives an engine version (and crosses installs via Centaur SD import
    and backup restore), so a stored value can have no field, and its meaning is
    then unknown. Values are matched case-insensitively because the engine
    matches option names that way and a hand-edited section can differ in case.
    """
    by_key = _by_casefolded_key((field.key, field) for field in fields)
    by_value = _by_casefolded_key(values.items())

    terms: List[str] = []
    for key in keys:
        field = by_key.get(key.casefold())
        if field is None:
            continue
        gate = getattr(field, "requires", "")
        if gate:
            gate_value = by_value.get(gate.casefold())
            if gate_value is not None and str(gate_value).strip().casefold() in _FALSE_VALUES:
                continue
        term = term_for(field, by_value.get(field.key.casefold()))
        if term:
            terms.append(term)
    return terms


def render_label(
    keys: Sequence[str],
    fields: Sequence,
    values: Mapping[str, str],
    *,
    fallback: Sequence[str] = (),
) -> str:
    """Render the profile label for ``keys``, or "" when no term applies.

    ``fallback`` keys are tried when ``keys`` renders nothing, which is how a
    per-install selection that cannot describe this profile degrades to the
    catalog/derived keys instead of to a blank label.

    The empty result is meaningful and must not be papered over: it says the
    profile sets nothing on the labelled axes, which is what an uncapped profile
    looks like. Only the caller knows what belongs there instead, so inventing a
    label here would put a strength in front of the user that no profile states.
    """
    terms = render_terms(keys, fields, values)
    if not terms and fallback:
        terms = render_terms(fallback, fields, values)
    return TERM_SEPARATOR.join(terms)


@dataclass(frozen=True)
class LabelProjection:
    """One engine's resolved label projection, ready to label any of its profiles.

    Built once per engine (the schema comes from a probe) and then applied to each
    profile's values, so a picker of twenty rows resolves the keys once.

    Attributes:
        keys: The label keys in effect, already resolved against ``fields``.
        fields: The engine's probed schema fields.
        fallback: Keys to project through when ``keys`` renders no term for a
            profile. This is the guard on a per-install selection: a selection of
            engine-wide options only (``Hash`` lives in the shared ``[DEFAULT]``
            section, never in a profile) would otherwise label every profile with
            the empty string, and there would be nothing in the picker to pick.
    """

    keys: Tuple[str, ...]
    fields: Tuple = ()
    fallback: Tuple[str, ...] = ()

    def label(self, values: Mapping[str, str]) -> str:
        """Return the label for a profile holding ``values``, or "" for no terms."""
        return render_label(self.keys, self.fields, values, fallback=self.fallback)


def selectable_keys(fields: Sequence) -> Tuple[str, ...]:
    """Return the keys of ``fields`` that may be used as label keys.

    Display-only (``info``) options are excluded: they are never written to a
    profile, so they cannot vary between profiles and would give every profile
    the same label.
    """
    return tuple(field.key for field in fields if field.type != "info")


def resolve_keys(declared: Sequence[str], fields: Sequence) -> Tuple[str, ...]:
    """Resolve ``declared`` label keys against the probed ``fields``.

    Keys are returned in declared order, spelled as the engine advertises them
    (so the value lookup is exact), with unknown keys, display-only options and
    duplicates dropped. Validating against the probe rather than trusting the
    declaration is what keeps a stale catalog entry, an engine update that
    removed an option, or a hand-written per-install override from composing a
    label out of keys the engine has no values for.
    """
    allowed = _by_casefolded_key(
        (key, key) for key in selectable_keys(fields)
    )
    resolved: List[str] = []
    for key in declared:
        if not isinstance(key, str):
            continue
        actual = allowed.get(key.casefold())
        if actual is None or actual in resolved:
            continue
        resolved.append(str(actual))
    return tuple(resolved)
