"""Read the descriptive Soul fields of OpenPersona, without its runtime or permissions."""

import json

from pydantic import ConfigDict, Field, field_validator

from personal_agent.character.models import CharacterError
from personal_agent.core.models import Model


class PersonaFields(Model):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*")
    @classmethod
    def reject_nul(cls, value: object) -> object:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("NUL is not supported in persona descriptions.")
        return value


class Identity(PersonaFields):
    personaName: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    bio: str = Field(min_length=1)
    role: str = ""


class Aesthetic(PersonaFields):
    creature: str = ""
    emoji: str = ""
    age: str = ""
    vibe: str = ""


class Character(PersonaFields):
    personality: str = Field(min_length=1)
    speakingStyle: str = Field(min_length=1)
    background: str = ""
    boundaries: str = ""
    behaviorGuide: str = ""


class Soul(PersonaFields):
    identity: Identity
    character: Character
    aesthetic: Aesthetic = Field(default_factory=Aesthetic)


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError


def parse_persona(text: str) -> dict[str, str]:
    """Only these descriptive fields can reach the prompt; unknown declarations are inert."""
    try:
        document = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
        soul = Soul.model_validate(document["soul"])
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CharacterError("invalid_persona_json") from None
    return {
        "姓名": soul.identity.personaName,
        "简介": soul.identity.bio,
        "角色": soul.identity.role,
        "类型": soul.aesthetic.creature,
        "年龄": soul.aesthetic.age,
        "氛围": soul.aesthetic.vibe,
        "表情": soul.aesthetic.emoji,
        "性格": soul.character.personality,
        "说话方式": soul.character.speakingStyle,
        "背景": soul.character.background,
        "相处边界": soul.character.boundaries,
        "行为指南": soul.character.behaviorGuide,
    }
