"""机械臂共享类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class JointState6:
    q: tuple[float, float, float, float, float, float]

    @classmethod
    def from_degrees(cls, deg: Sequence[float]) -> "JointState6":
        import math

        if len(deg) != 6:
            raise ValueError("expected 6 joint angles in degrees")
        return cls(tuple(math.radians(float(x)) for x in deg))  # type: ignore[arg-type]

    def as_degrees(self) -> tuple[float, float, float, float, float, float]:
        import math

        return tuple(math.degrees(q) for q in self.q)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class CartesianTarget:
    x: float
    y: float
    z: float
    rx: float
    ry: float
    rz: float

    def as_pose_list(self) -> list[float]:
        return [self.x, self.y, self.z, self.rx, self.ry, self.rz]
