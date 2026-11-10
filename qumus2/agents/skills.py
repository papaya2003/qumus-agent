"""Reference skills appended to agent instructions by the OpenAI runtime.

Skills contain judgement criteria; tools and policy enforce hard rules.
"""

from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills" / "skills"


def skill_text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
