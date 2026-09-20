"""Playing browser games, as a test of whether the input and perception layers really work."""

from browser_use.play.arena import GameArena
from browser_use.play.strategies import Action, BanditPlayer, default_actions
from browser_use.play.views import GameReport, InputEvent, Scoreboard

__all__ = ['Action', 'BanditPlayer', 'GameArena', 'GameReport', 'InputEvent', 'Scoreboard', 'default_actions']
