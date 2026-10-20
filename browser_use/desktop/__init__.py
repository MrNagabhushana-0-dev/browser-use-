# Set to 1 only where the person has agreed to let the AI act on their desktop (see `service.DesktopControl`). Kept
# here, not in `service.py`, so the tool list can read it without importing desktop control.
OPT_IN_ENV = 'BROWSER_USE_DESKTOP_CONTROL'
