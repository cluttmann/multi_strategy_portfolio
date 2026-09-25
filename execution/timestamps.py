"""Timezone-aware broker timestamps, compatible with the Python 3.10 runtime."""
import datetime as dt
import re


def parse_timestamp(value):
    # Alpaca emits RFC3339 nanoseconds. Python 3.10 accepts only 3 or 6
    # fractional digits; truncate sub-microseconds and pad shorter fractions.
    # Preserve the timezone: freshness checks must compare actual instants.
    normalized=re.sub(r'\.(\d+)(?=Z$|[+-]\d{2}:\d{2}$)',
                      lambda m:'.'+m.group(1)[:6].ljust(6,'0'),value)
    stamp=dt.datetime.fromisoformat(normalized.replace('Z','+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Broker timestamp requires a timezone')
    return stamp
