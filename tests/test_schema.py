import json

import pytest
from pydantic import ValidationError

from triage.schema import ROUTE_BY_CATEGORY, TriageDecision, TriageValidationError, validate_decision

VALID = {
    "category": "billing",
    "priority": "P2",
    "route": "billing-team",
    "rationale": "Double charge puts money at stake (P2).",
}


def with_(**changes):
    return {**VALID, **changes}


POLICY_ROUTES = {
    "billing": "billing-team",
    "bug": "bug-team",
    "access": "access-team",
    "performance": "performance-team",
    "how-to": "how-to-team",
}


def rejects(data, field):
    with pytest.raises(TriageValidationError) as exc:
        validate_decision(data)
    assert f"{field}:" in str(exc.value)
    return str(exc.value)


def test_valid_dict_is_accepted():
    decision = validate_decision(VALID)
    assert isinstance(decision, TriageDecision)
    assert decision.model_dump() == VALID


def test_valid_json_text_is_accepted():
    assert validate_decision(json.dumps(VALID)) == validate_decision(VALID)


def test_route_mapping_matches_triage_policy():
    assert ROUTE_BY_CATEGORY == POLICY_ROUTES


@pytest.mark.parametrize(("category", "route"), sorted(POLICY_ROUTES.items()))
def test_every_category_with_its_route_is_accepted(category, route):
    validate_decision(with_(category=category, route=route))


def test_decision_cannot_be_changed_after_validation():
    decision = validate_decision(VALID)
    with pytest.raises(ValidationError):
        decision.route = "bug-team"
    assert decision.route == "billing-team"


def test_json_schema_tells_the_model_the_rules():
    schema = TriageDecision.model_json_schema()
    assert schema["properties"]["rationale"]["maxLength"] == 199
    assert "billing -> billing-team" in schema["properties"]["route"]["description"]
    assert schema["additionalProperties"] is False


def test_unknown_category_lists_allowed_values():
    message = rejects(with_(category="sales"), "category")
    for allowed in ROUTE_BY_CATEGORY:
        assert allowed in message


@pytest.mark.parametrize("priority", ["P5", "p2", 2])
def test_bad_priority_is_rejected(priority):
    message = rejects(with_(priority=priority), "priority")
    assert "P1" in message and "P4" in message


def test_route_that_disagrees_with_category_is_rejected():
    message = rejects(with_(route="bug-team"), "route")
    assert "billing-team" in message


def test_missing_field_is_rejected():
    data = dict(VALID)
    del data["route"]
    message = rejects(data, "route")
    assert "required" in message


def test_extra_field_is_rejected():
    message = rejects(with_(confidence=0.9), "confidence")
    assert "not allowed" in message


@pytest.mark.parametrize("rationale", ["", "   "])
def test_empty_rationale_is_rejected(rationale):
    rejects(with_(rationale=rationale), "rationale")


@pytest.mark.parametrize("line_break", ["\n", "\r", "\u2028", "\x0b", "\x0c", "\x85"])
def test_multi_line_rationale_is_rejected(line_break):
    message = rejects(with_(rationale=f"Money at stake.{line_break}Second line."), "rationale")
    assert "line breaks" in message


def test_rationale_ending_in_a_line_break_is_rejected():
    rejects(with_(rationale="Money at stake.\n"), "rationale")


def test_rationale_of_199_characters_is_accepted():
    validate_decision(with_(rationale="x" * 199))


def test_rationale_of_200_characters_is_rejected():
    message = rejects(with_(rationale="x" * 200), "rationale")
    assert "under 200 characters" in message


@pytest.mark.parametrize("data", ["{not json", "[1, 2]", [VALID], None])
def test_non_object_raises_triage_validation_error(data):
    with pytest.raises(TriageValidationError):
        validate_decision(data)
