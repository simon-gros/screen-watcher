from __future__ import annotations

from dataclasses import dataclass

ANCHORS = {
    "top-left", "top-right", "bottom-left", "bottom-right",
    "top-center", "bottom-center", "center",
}


@dataclass(frozen=True)
class Region:
    anchor: str
    dx: int
    dy: int
    w: int
    h: int
    grid: tuple[int, int, int, int, int, int] | None = None

    @staticmethod
    def parse(spec) -> "Region":
        if isinstance(spec, list):
            if len(spec) != 4:
                raise ValueError("legacy region arrays must be [x,y,w,h]")
            x, y, w, h = spec
            return Region("top-left", x, y, w, h)
        if not isinstance(spec, dict):
            raise TypeError("region must be an object or [x,y,w,h]")
        unknown = {
            key for key in spec
            if not key.startswith("_") and key not in {"anchor", "dx", "dy", "w", "h", "grid"}
        }
        if unknown:
            raise ValueError(f"unknown region fields: {', '.join(sorted(unknown))}")
        anchor = spec.get("anchor", "top-left")
        if anchor not in ANCHORS:
            raise ValueError(f"bad anchor {anchor!r}; expected one of {sorted(ANCHORS)}")
        grid_spec = spec.get("grid")
        grid = None
        if grid_spec is not None:
            allowed = {"x0", "y0", "cell_w", "cell_h", "cols", "rows"}
            extra = {k for k in grid_spec if not k.startswith("_") and k not in allowed}
            if extra:
                raise ValueError(f"unknown grid fields: {', '.join(sorted(extra))}")
            grid = (
                int(grid_spec["x0"]),
                int(grid_spec["y0"]),
                int(grid_spec["cell_w"]),
                int(grid_spec["cell_h"]),
                int(grid_spec["cols"]),
                int(grid_spec["rows"]),
            )
        return Region(
            anchor,
            int(spec["dx"]),
            int(spec["dy"]),
            int(spec["w"]),
            int(spec["h"]),
            grid,
        )

    def resolve(self, win: tuple[int, int]) -> tuple[int, int, int, int]:
        width, height = win
        if self.anchor.startswith("top"):
            y = self.dy
        elif self.anchor.startswith("bottom"):
            y = height + self.dy - self.h
        else:
            y = (height - self.h) // 2 + self.dy

        if self.anchor.endswith("left"):
            x = self.dx
        elif self.anchor.endswith("right"):
            x = width + self.dx - self.w
        else:
            x = (width - self.w) // 2 + self.dx

        w = max(1, min(self.w, width))
        h = max(1, min(self.h, height))
        x = max(0, min(x, width - w))
        y = max(0, min(y, height - h))
        return x, y, w, h
