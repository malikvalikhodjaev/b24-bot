"""Timing metadata only: never print API URLs, payloads or error descriptions."""
from datetime import datetime, timezone
import re


def diagnostic(component, seconds, status):
    component = component if re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', component) else 'unknown'
    status = status if re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', status) else 'ERROR'
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    print(f'{stamp} {component} seconds={seconds:.3f} status={status}', flush=True)
