"""Force a recorder channel's white light on, and put it back (#959).

On the cameras this is for, the physical white emitter is governed by a
per-channel LightingScheme: nine scenes, each with a LightingMode. In AIMode the
camera decides its own illumination and ignores a manual Lighting_V2 write --
the write is accepted and reads back, and the light stays off, which is the
whole of #959. Forcing the light on means two things in order: set the
WhiteLight row of Lighting_V2 to Manual at the wanted brightness, then switch
the scenes to WhiteMode. Measured with the owner watching the light on a
DHI-NVR5464-16P-EI driving a VSIPP-6DIRMD-I3 (four warm LEDs) on channel 11:
Lighting_V2 alone did nothing; both tables lit it; restoring both put it back.
The recipe is @jays3l33t's, from #959, confirmed on his DHI-NVR5232 first.

Everything here is pure: it takes the two tables as the device returns them for
one channel and hands back what to write or what was read. The transport, the
snapshot that is kept to undo this, and the entity live elsewhere. Kept apart so
the shape handling can be tested against the tables a real recorder returned,
which are in tests/dahua/fixtures.

What it will not do, on purpose:
- invent fields. A camera that has NearLight and FarLight but no MiddleLight
  keeps that shape; only brightness banks that exist are written.
- touch anything but LightingMode and the WhiteLight row. The scene schedule
  and the infrared and mixed-light rows are carried over verbatim.
- guess which scene is active. Every scene is forced, because the active
  lighting scene is not reliably observable (#647) and the integration's
  profile index has been the wrong one before (#959: "General" was scene 2).
  The caller undoes this by restoring the exact tables it saved, not by
  writing a mode it assumes was there.
"""

import copy

WHITE_LIGHT = "WhiteLight"
WHITE_MODE = "WhiteMode"
MANUAL = "Manual"
SCHEME_SCHEDULE = "SchemeSchedule"

# The brightness banks a WhiteLight row may carry. A row has some subset.
BRIGHTNESS_BANKS = ("NearLight", "MiddleLight", "FarLight")


def lighting_shape(scheme, lighting):
    """How many scenes the two tables describe, or None if this code cannot
    drive them.

    Both must be lists of the same length; every scene must carry a
    LightingMode string; every profile must hold exactly one WhiteLight row
    with a Mode string. Exactly one, because two would leave this guessing
    which emitter it is forcing, and zero means there is nothing to force.
    """
    if not isinstance(scheme, list) or not scheme:
        return None
    if not isinstance(lighting, list) or len(lighting) != len(scheme):
        return None
    for scene in scheme:
        if not isinstance(scene, dict):
            return None
        mode = scene.get("LightingMode")
        if not isinstance(mode, str) or not mode:
            return None
    for profile in lighting:
        if not isinstance(profile, list):
            return None
        whites = [
            row
            for row in profile
            if isinstance(row, dict) and row.get("LightType") == WHITE_LIGHT
        ]
        if len(whites) != 1 or not isinstance(whites[0].get("Mode"), str):
            return None
    return len(scheme)


def white_light(profile):
    """The WhiteLight row of one profile. lighting_shape has promised one."""
    return next(
        row
        for row in profile
        if isinstance(row, dict) and row.get("LightType") == WHITE_LIGHT
    )


def scheme_modes(scheme):
    """Each scene's LightingMode, in order."""
    return [scene.get("LightingMode") for scene in scheme]


def white_light_modes(lighting):
    """Each profile's WhiteLight Mode, in order."""
    return [white_light(profile).get("Mode") for profile in lighting]


def build_on(scheme, lighting, brightness):
    """Copies of both tables that force the white light on.

    Every scene becomes WhiteMode and every WhiteLight row becomes Manual at
    `brightness` (the device's 0-100 scale), written into whichever brightness
    banks the row already has. The inputs are not mutated: the caller keeps
    them as the snapshot it will restore.
    """
    scheme_on = copy.deepcopy(scheme)
    lighting_on = copy.deepcopy(lighting)
    for scene in scheme_on:
        scene["LightingMode"] = WHITE_MODE
        # Disable the scene's own schedule while it is forced. A schedule left
        # enabled can switch the scene back off WhiteMode on a timer, undoing
        # the override with nothing in HA to show why. The original Enable is
        # carried in the snapshot and comes back on restore. Only touched when
        # the schedule is already there; no field is invented (@jays3l33t, #959).
        schedule = scene.get(SCHEME_SCHEDULE)
        if isinstance(schedule, dict) and "Enable" in schedule:
            schedule["Enable"] = False
    level = int(brightness)
    for profile in lighting_on:
        row = white_light(profile)
        row["Mode"] = MANUAL
        for bank in BRIGHTNESS_BANKS:
            segments = row.get(bank)
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if isinstance(segment, dict) and "Light" in segment:
                    segment["Light"] = level
    return scheme_on, lighting_on


def is_forced_on(scheme, lighting):
    """Whether a read-back shows the on state this module writes.

    Compares only the fields that were written, so a camera that normalises
    some other field on the way through still counts as converged.
    """
    return all(mode == WHITE_MODE for mode in scheme_modes(scheme)) and all(
        mode == MANUAL for mode in white_light_modes(lighting)
    )


def modes_match(scheme_a, lighting_a, scheme_b, lighting_b):
    """Whether two pairs of tables agree on every scene and WhiteLight mode.

    The restore check: the tables read back after putting the snapshot back
    must show the snapshot's modes, whatever they were -- Off, ZoomPrio, Auto.
    Judged on modes rather than whole-table equality for the same reason as
    is_forced_on.
    """
    return scheme_modes(scheme_a) == scheme_modes(scheme_b) and white_light_modes(
        lighting_a
    ) == white_light_modes(lighting_b)


def build_restore(cur_scheme, cur_lighting, orig_scheme, orig_lighting):
    """Tables that put the owned parts back without clobbering anything else.

    build_on forces every scene, so the snapshot is the whole original table and
    a blunt restore would overwrite a scene the user changed while the light was
    on. This merges: start from what the camera reports now, and put a scene's
    LightingMode (and its schedule) back only where it is still WhiteMode, and a
    WhiteLight row back only where it is still Manual -- the exact marks build_on
    leaves. A scene or row that reads as anything else was changed since, so it
    is left as it is. No active-scene index is needed, which is deliberate: the
    index is what #959 kept getting wrong, so ownership is read off the modes.

    cur_* is a fresh read the caller has already checked with lighting_shape and
    confirmed the same length as the snapshot. Inputs are not mutated.
    """
    restore_scheme = copy.deepcopy(cur_scheme)
    restore_lighting = copy.deepcopy(cur_lighting)
    for index, scene in enumerate(restore_scheme):
        if scene.get("LightingMode") == WHITE_MODE:
            restore_scheme[index] = copy.deepcopy(orig_scheme[index])
    for index, profile in enumerate(restore_lighting):
        row = white_light(profile)
        if row.get("Mode") == MANUAL:
            row.clear()
            row.update(copy.deepcopy(white_light(orig_lighting[index])))
    return restore_scheme, restore_lighting
