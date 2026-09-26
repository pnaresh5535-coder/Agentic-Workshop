"""The triage decision: the one contract the agent returns and the eval checks.

A decision is a JSON object with exactly four fields: category, priority,
route and a one-line rationale. The route is fixed by the category
(TRIAGE_POLICY.md), so a decision whose route disagrees is rejected.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

Category = Literal["billing", "bug", "access", "performance", "how-to"]
Priority = Literal["P1", "P2", "P3", "P4"]
Route = Literal["billing-team", "bug-team", "access-team", "performance-team", "how-to-team"]

ROUTE_BY_CATEGORY: dict[str, str] = {
    "billing": "billing-team",
    "bug": "bug-team",
    "access": "access-team",
    "performance": "performance-team",
    "how-to": "how-to-team",
}

MAX_RATIONALE_CHARS = 199


class TriageValidationError(ValueError):
    """A triage decision failed validation. The message names each bad field."""


class TriageDecision(BaseModel):
    """One triage decision for one support ticket."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    category: Category = Field(description="The ticket's category, from TRIAGE_POLICY.md.")
    priority: Priority = Field(description="P1 (outage) to P4 (question or cosmetic), from TRIAGE_POLICY.md.")
    route: Route = Field(
        description="The team for the category: "
        + ", ".join(f"{category} -> {route}" for category, route in ROUTE_BY_CATEGORY.items())
        + "."
    )
    rationale: str = Field(
        max_length=MAX_RATIONALE_CHARS,
        description=f"One sentence naming the rule applied: one line, under {MAX_RATIONALE_CHARS + 1} characters.",
    )

    @field_validator("route")
    @classmethod
    def _route_matches_category(cls, route: str, info: ValidationInfo) -> str:
        category = info.data.get("category")
        if category is not None and route != ROUTE_BY_CATEGORY[category]:
            raise ValueError(f"expected '{ROUTE_BY_CATEGORY[category]}' for category '{category}', got '{route}'")
        return route

    @field_validator("rationale")
    @classmethod
    def _rationale_is_one_short_line(cls, rationale: str) -> str:
        if not rationale.strip():
            raise ValueError("must not be empty")
        # splitlines() also catches  , \x0b, \x0c, \x85 and a trailing break.
        if rationale.splitlines() != [rationale]:
            raise ValueError("must be one line, with no line breaks")
        return rationale


def validate_decision(data: dict | str) -> TriageDecision:
    """Validate a decision given as a dict or as JSON text.

    Raises TriageValidationError, never a raw pydantic or json error.
    """
    try:
        if isinstance(data, str):
            return TriageDecision.model_validate_json(data)
        return TriageDecision.model_validate(data)
    except ValidationError as exc:
        raise TriageValidationError(_describe(exc)) from exc


def _describe(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "decision"
        if error["type"] == "missing":
            message = "is required"
        elif error["type"] == "extra_forbidden":
            message = "is not allowed (the fields are category, priority, route, rationale)"
        elif error["type"] == "string_too_long":
            message = f"must be under {MAX_RATIONALE_CHARS + 1} characters, got {len(error['input'])}"
        else:
            message = error["msg"].removeprefix("Value error, ")
        problems.append(f"{field}: {message}")
    return "Invalid triage decision: " + "; ".join(problems)
