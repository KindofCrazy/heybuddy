"""Provider-independent immutable image references and a Host-bound reader."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = Field(default="image/jpeg", pattern=r"^image/jpeg$")
    size_bytes: int = Field(gt=0, le=262144)
    width: int = Field(gt=0, le=1536)
    height: int = Field(gt=0, le=1536)
    # Normalized uploads contain one frame, even when their source was animated.
    frame: int = Field(default=0, ge=0, le=0)


class ImageReader(Protocol):
    def read_image(self, reference: ImageRef) -> bytes: ...
