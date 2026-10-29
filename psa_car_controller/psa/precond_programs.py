"""Preconditioning schedules: the car keeps four programs, each with week days, a departure time and an on flag.

MQTT format (what the car takes): {"program1": {"day": [Mon..Sun as 0/1], "hour": 6, "minute": 0, "on": 1}, ...}
hour 34 is the car's "unset" value.
"""
from copy import deepcopy

from psa_car_controller.common.utils import parse_hour
from psa_car_controller.psa.constants import DEFAULT_PRECONDITIONING_PROGRAM

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
UNSET_HOUR = 34
KEYS = ("program1", "program2", "program3", "program4")


def from_status(programs) -> dict:
    """Programs from the REST vehicle status (slot, enabled, start "PT6H30M", occurence.day ["Mon", ...])."""
    result = deepcopy(DEFAULT_PRECONDITIONING_PROGRAM)
    for program in programs or []:
        slot = _get(program, "slot")
        if slot not in (1, 2, 3, 4):
            continue
        days = _get(_get(program, "occurence") or {}, "day") or []
        try:
            parts = parse_hour(_get(program, "start"))
            hour, minute = parts[0], parts[1]
        except (AttributeError, TypeError, ValueError, IndexError):
            hour, minute = UNSET_HOUR, 0
        result[f"program{slot}"] = {"day": [1 if day in days else 0 for day in DAYS], "hour": hour,
                                    "minute": minute, "on": 1 if _get(program, "enabled") else 0}
    return result


def _get(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def is_set(program) -> bool:
    return program.get("hour", UNSET_HOUR) != UNSET_HOUR and any(program.get("day", []))


def is_default(programs) -> bool:
    return not any(is_set(programs.get(key, {})) for key in KEYS)


def days_text(day_flags) -> str:
    """[1,1,1,1,0,0,0] -> "Mon–Thu"; [1,0,1,0,1,0,0] -> "Mon, Wed, Fri"; every day -> "Every day"."""
    days = [i for i, flag in enumerate(day_flags) if flag]
    if not days:
        return "No day"
    if len(days) == 7:
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if days == [5, 6]:
        return "Weekend"
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAYS[days[0]]}–{DAYS[days[-1]]}"
    return ", ".join(DAYS[i] for i in days)


def program_text(program) -> str:
    if not is_set(program):
        return "Not set"
    return f"{days_text(program['day'])} · {program['hour']:02d}:{program['minute']:02d}"


def build(on_flags, times, day_lists) -> dict:
    """Programs from the form values, one entry per program. A program without time or day is stored as unset."""
    programs = {}
    for key, on, time, days in zip(KEYS, on_flags, times, day_lists):
        flags = [1 if i in (days or []) else 0 for i in range(7)]
        try:
            hour, minute = (int(part) for part in (time or "").split(":")[:2])
        except ValueError:
            hour, minute = UNSET_HOUR, 0
        if hour == UNSET_HOUR or not any(flags):
            programs[key] = dict(DEFAULT_PRECONDITIONING_PROGRAM[key])
        else:
            programs[key] = {"day": flags, "hour": hour, "minute": minute, "on": 1 if on else 0}
    return programs
