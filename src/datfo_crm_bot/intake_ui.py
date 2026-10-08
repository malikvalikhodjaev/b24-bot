from __future__ import annotations

import math
from .i18n import tr
from .intake_content import TEXTS

def phrase(key):
    return tr(TEXTS[key]['ru'])

ADD_LOCATION = TEXTS['location_yes']['ru']
SKIP_LOCATION = TEXTS['location_skip']['ru']
SEND_LOCATION = TEXTS['location_current']['ru']
ADD_REMINDER = TEXTS['reminder_yes']['ru']
NO_REMINDER = TEXTS['reminder_no']['ru']


def coordinates(value):
    if not isinstance(value, dict):
        raise ValueError(phrase('location_invalid'))
    result = {}
    for key, limit in (('latitude', 90), ('longitude', 180)):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not -limit <= number <= limit:
            raise ValueError(phrase('location_invalid'))
        result[key] = float(number)
    return result


def location_keyboard():
    from .service import CANCEL
    return [[{'text': SEND_LOCATION, 'request_location': True}], [SKIP_LOCATION], [CANCEL]]
