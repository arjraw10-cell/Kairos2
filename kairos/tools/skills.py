import re
from pathlib import Path
from typing import List

from .base import ToolResult


class SkillManager:
    """Manages skill folders, each containing a SKILL.md file.

    Skills are stored under ``<workspace>/skills/<skill_name>/SKILL.md``.
    Only skill names are loaded into the system prompt; the full content
    is fetched on demand via ``load_skill``.
    """

    SKILL_FILENAME = "SKILL.md"

    # Folders / names that are not allowed as skill names
    _UNSAFE_PATTERN = re.compile(r"[\\/:*?\"<>|\x00]")

    def __init__(self, skills_dir: str):
        self.skills_dir = Path(skills_dir).resolve()
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def list_skills(self) -> ToolResult:
        """Return a comma-separated list of available skill names."""
        names = self._discover_skills()
        if not names:
            return ToolResult(True, "No skills available.")
        return ToolResult(True, "Available skills:\n" + ", ".join(names))

    def load_skill(self, skill_name: str) -> ToolResult:
        """Load and return the full SKILL.md content for a skill."""
        name_err = self._validate_name(skill_name)
        if name_err:
            return ToolResult(False, "", name_err)

        skill_path = self.skills_dir / skill_name / self.SKILL_FILENAME
        if not skill_path.is_file():
            available = ", ".join(self._discover_skills()) or "(none)"
            return ToolResult(
                False,
                "",
                f"Skill '{skill_name}' not found. Available skills: {available}",
            )

        try:
            content = skill_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(False, "", f"Failed to read skill: {e}")

        if not content.strip():
            return ToolResult(
                True, f"[Skill '{skill_name}' exists but SKILL.md is empty]"
            )

        return ToolResult(True, content)

    def write_skill(
        self, skill_name: str, content: str, overwrite: bool = False
    ) -> ToolResult:
        """Create or update a skill's SKILL.md file."""
        name_err = self._validate_name(skill_name)
        if name_err:
            return ToolResult(False, "", name_err)

        if not content or not content.strip():
            return ToolResult(False, "", "Skill content must not be empty.")

        skill_dir = self.skills_dir / skill_name
        skill_path = skill_dir / self.SKILL_FILENAME

        if skill_path.exists() and not overwrite:
            return ToolResult(
                False,
                "",
                f"Skill '{skill_name}' already exists. "
                f"Set overwrite=true to replace it.",
            )

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(content, encoding="utf-8")
        except Exception as e:
            return ToolResult(False, "", f"Failed to write skill: {e}")

        action = "Updated" if overwrite else "Created"
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(True, f"{action} skill '{skill_name}' ({lines} lines)")

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    def _discover_skills(self) -> List[str]:
        """Return sorted list of skill names that have a SKILL.md file."""
        if not self.skills_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.skills_dir.iterdir()
            if entry.is_dir() and (entry / self.SKILL_FILENAME).is_file()
        )

    def _validate_name(self, name: str) -> str:
        """Return an error string if the name is invalid, else empty string."""
        if not name or not name.strip():
            return "Skill name must not be empty."
        if self._UNSAFE_PATTERN.search(name):
            return (
                f"Invalid skill name '{name}': "
                f"contains illegal characters. Use only letters, numbers, "
                f"hyphens, and underscores."
            )
        if ".." in name:
            return f"Invalid skill name '{name}': must not contain '..'."
        return ""
