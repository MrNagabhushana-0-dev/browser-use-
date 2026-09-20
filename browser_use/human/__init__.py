"""Drive the browser the way a person does: real pointer paths, real wheel, real keys."""

from browser_use.human.input import HumanInput
from browser_use.human.motion import bezier_path, keystroke_delays, landing_point

__all__ = ['HumanInput', 'bezier_path', 'keystroke_delays', 'landing_point']
