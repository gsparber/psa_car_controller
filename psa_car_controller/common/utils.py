import contextlib
from functools import wraps
from threading import Semaphore, Timer
from typing import List

import requests
TIMEOUT_IN_S = 10


def rate_limit(limit, every):
    def limit_decorator(func):
        semaphore = Semaphore(limit)

        @wraps(func)
        def wrapper(*args, **kwargs):
            if semaphore.acquire(blocking=False):  # pylint: disable=consider-using-with
                try:
                    return func(*args, **kwargs)
                finally:  # don't catch but ensure semaphore release
                    timer = Timer(every, semaphore.release)
                    timer.daemon = True
                    timer.start()
            else:
                raise RateLimitException(func.__name__)

        return wrapper

    return limit_decorator


def parse_hour(s):
    s = s[2:]
    separators = ("H", "M", "S")
    res: List[int] = []
    for sep in separators:
        if sep in s:
            n, s = s.split(sep)
        else:
            n = 0
        res.append(int(n))
        if s.isnumeric():
            res.append(int(s))
            break
    if len(res) == 2:
        res.append(0)
    return res


class RateLimitException(Exception):
    def __init__(self, func_name):
        super().__init__(f"Rate limit exceeded for {func_name}")


def get_positions(locations):
    latitude = 0
    longitude = 1
    locations_str = ""
    for line in locations:
        locations_str += str(line[latitude]) + "," + str(line[longitude]) + "|"
    locations_str = locations_str[:-1]
    res = requests.get("https://api.opentopodata.org/v1/srtm30m",
                       params={"locations": locations_str},
                       timeout=TIMEOUT_IN_S)
    return res.json()["results"]


@contextlib.contextmanager
def nonblocking(lock):
    locked = lock.acquire(False)
    try:
        yield locked
    finally:
        if locked:
            lock.release()


PRECOND_PROGRAM_KEYS = ("program1", "program2", "program3", "program4")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_preconditioning_programs(programs):
    # The car expects exactly four programs. Each has a 7-day flag list (Monday
    # first), a departure hour/minute and an on flag. hour 34 is the car's
    # "unset" sentinel, so it is allowed alongside 0-23.
    if not isinstance(programs, dict):
        raise ValueError("programs must be an object")
    validated = {}
    for key in PRECOND_PROGRAM_KEYS:
        program = programs.get(key)
        if not isinstance(program, dict):
            raise ValueError(f"{key} must be an object")
        day = program.get("day")
        if not isinstance(day, list) or len(day) != 7 or any(d not in (0, 1) for d in day):
            raise ValueError(f"{key}.day must be a list of 7 values of 0 or 1")
        hour = program.get("hour")
        if not _is_int(hour) or not (0 <= hour <= 23 or hour == 34):
            raise ValueError(f"{key}.hour must be 0-23 (or 34 for unset)")
        minute = program.get("minute")
        if not _is_int(minute) or not 0 <= minute <= 59:
            raise ValueError(f"{key}.minute must be 0-59")
        on = program.get("on")
        if on not in (0, 1):
            raise ValueError(f"{key}.on must be 0 or 1")
        validated[key] = {"day": day, "hour": hour, "minute": minute, "on": on}
    return validated
