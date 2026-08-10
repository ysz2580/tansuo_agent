"""内置技能包。"""
from .config import SetupSkill
from .tune import TuneSkill

SKILLS = {"tune": TuneSkill, "setup": SetupSkill}

__all__ = ["SKILLS", "TuneSkill", "SetupSkill"]
