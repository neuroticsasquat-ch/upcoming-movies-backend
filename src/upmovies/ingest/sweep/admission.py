"""The admission gate: whether a candidate the enumerate phase reached may be written.

Two levels, deliberately kept apart (spec §7.3, §7.4):

- **`enabled`** is the master switch, in the manner of `NEWS_GOOGLE_ENABLED`. Off means the
  sweep still runs and still reports what it found — the one-move rollback if precision
  collapses, with dormancy left to drain the working set on its own.
- **`directors` / `writers` / `cast`** ramp admission one seed grade at a time. The
  retrieval-health guard (ADR-0010) gets to react to a 1,446-person expansion before a
  7,519-person one, and if precision does collapse we learn *which* grade caused it rather
  than staring at one undifferentiated jump.

Every flag defaults off, so "the sweep admits nothing" is a configuration rather than a
code state: opening a tranche is an env change, not a deploy.
"""

from collections.abc import Container
from dataclasses import dataclass

from upmovies.config import Settings


@dataclass(frozen=True)
class AdmissionTranches:
    """Which seed grades may admit a film, master switch included."""

    enabled: bool = False
    directors: bool = False
    writers: bool = False
    cast: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "AdmissionTranches":
        """The gate as the deploy has it configured. The mapping lives here rather than at
        the entrypoint so there is exactly one place the env flags become a decision."""
        return cls(
            enabled=settings.sweep_enabled,
            directors=settings.sweep_admit_directors,
            writers=settings.sweep_admit_writers,
            cast=settings.sweep_admit_cast,
        )

    def admits(self, roles: Container[str]) -> bool:
        """Whether a candidate reached at these seed-grade `roles` may be written.

        The film is the unit of admission, not the credit: one open tranche among the
        grades that reached it is enough.
        """
        if not self.enabled:
            return False
        return (
            (self.directors and "director" in roles)
            or (self.writers and "writer" in roles)
            or (self.cast and "cast" in roles)
        )
