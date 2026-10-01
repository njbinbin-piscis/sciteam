"""Organization manifest schema and agent-profile templates (docs/50-organization.md §2, §4).
The manifest is a rule-set file: registering it makes the institution layer parse and version
it like the rules and the communication policy."""

from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sciteam.comm.model import ID_RE
from sciteam.comm.policy import compile_selector
from sciteam.institution.model import FILE_KINDS

MANIFEST_PREFIX = "org/"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Seat(_Strict):
    name: str
    count: int = Field(1, ge=1)
    team: str = ""
    reports_to: str | None = None
    groups: list[str] = Field(default_factory=list)
    harness: list[str] = Field(default_factory=list)   # allowed harness configs; [] = default
    templates: list[str] = Field(default_factory=list)  # suggested profile templates
    charter: str = ""

    @model_validator(mode="after")
    def _names(self):
        if not ID_RE.match(self.name):
            raise ValueError(f"seat name '{self.name}' must match {ID_RE.pattern}")
        for g in self.groups:
            if not ID_RE.match(g):
                raise ValueError(f"group name '{g}' must match {ID_RE.pattern}")
        return self


class GoalSpec(_Strict):
    label: str = "goal"
    fact: str = "accepted"


class Manifest(_Strict):
    mission: str = ""
    default_harness: str | None = None
    staffing: str | None = None     # selector: who is told about vacancies
    seats: list[Seat] = Field(default_factory=list)
    goals: GoalSpec | None = None

    @model_validator(mode="after")
    def _refs(self):
        if self.staffing:
            compile_selector(self.staffing, {})
        names = [s.name for s in self.seats]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate seats {sorted(dup)}")
        for s in self.seats:
            if s.reports_to is not None and s.reports_to not in names:
                raise ValueError(f"seat {s.name}: reports_to unknown seat '{s.reports_to}'")
            if not s.harness and not self.default_harness:
                raise ValueError(f"seat {s.name}: no harness allowed (give seats[].harness "
                                 f"or default_harness)")
        return self

    def seat(self, name: str) -> Seat | None:
        return next((s for s in self.seats if s.name == name), None)

    def allowed_harness(self, seat: Seat) -> list[str]:
        return seat.harness or [self.default_harness]

    def groups_of(self, seat: str) -> str:
        s = self.seat(seat)
        return ",".join(s.groups) if s else ""


FILE_KINDS[MANIFEST_PREFIX] = Manifest


def sha(text: str) -> str:
    return sha256(text.encode()).hexdigest()
