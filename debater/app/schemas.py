from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


Confidence = Literal["high", "medium", "low"]
ClaimType = Literal[
    "FACT",
    "CLAIM",
    "ASSUMPTION",
    "INFERENCE",
    "OPINION",
    "USER_PREFERENCE",
    "UNCERTAINTY",
]
ClaimStatus = Literal[
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "DISPUTED",
    "REFUTED",
    "UNVERIFIED",
]


class Weight(BaseModel):
    """Legacy-compatible representation of a criterion weight."""

    name: str = ""
    percent: int = 0
    note: str = ""


class Criterion(BaseModel):
    name: str
    weight: int
    description: str = ""
    source: str = ""


class Option(BaseModel):
    name: str
    description: str = ""


class DecisionBrief(BaseModel):
    decision: str = ""
    question: str = ""
    user: str = ""
    context: str = ""
    options: list[Option] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    criteria_origin: Literal["user", "evidence", ""] = ""
    weights_origin: Literal["user", "equal_default", ""] = ""
    status_quo: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    complete: bool = False

    # Kept in serialized history/API output while the existing UI migrates.
    subject: str = ""
    job: str = ""
    weights: list[Weight] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def load_legacy_card(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data.setdefault("decision", data.get("subject", ""))
        data.setdefault("question", data.get("subject", ""))
        data.setdefault("context", data.get("job", ""))
        data.setdefault("constraints", data.get("deal_breakers") or [])
        if not data.get("criteria") and data.get("weights"):
            data["criteria"] = [
                {
                    "name": item.get("name", ""),
                    "weight": item.get("percent", 0),
                    "description": item.get("note", ""),
                }
                for item in data["weights"]
            ]
        return data

    @model_validator(mode="after")
    def sync_legacy_fields(self) -> "DecisionBrief":
        self.decision = self.decision or self.question or self.subject
        self.question = self.question or self.decision
        self.context = self.context or self.job
        self.subject = self.decision
        self.job = self.context
        self.constraints = self.constraints or self.deal_breakers
        self.deal_breakers = list(self.constraints)
        self.weights = [
            Weight(name=item.name, percent=item.weight, note=item.description)
            for item in self.criteria
        ]
        return self


UseCaseCard = DecisionBrief


class EvidenceItem(BaseModel):
    id: str
    option: str
    requirement_or_criterion: str = ""
    evidence: str = ""
    source: str = ""
    source_quality: str = "unknown"


class Claim(BaseModel):
    id: str
    option: str = ""
    statement: str = ""
    claim_type: ClaimType = "CLAIM"
    evidence: str = ""
    source: str = ""
    source_quality: str = "unknown"
    assumptions: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    counterargument: str = ""
    status: ClaimStatus = "UNVERIFIED"
    load_bearing: bool = False


class OptionResearch(BaseModel):
    option: str
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class DiscoveredCriterion(BaseModel):
    name: str = ""
    source: str = ""
    description: str = ""


class CriteriaDiscovery(BaseModel):
    criteria: list[DiscoveredCriterion] = Field(default_factory=list)


class ResearchOutput(BaseModel):
    options: list[OptionResearch] = Field(default_factory=list)
    grounded: bool = False
    limitations: list[str] = Field(default_factory=list)


class Challenge(BaseModel):
    target_id: str
    attack: str = ""
    evidence: str = ""
    source: str = ""
    response: str = ""
    counter_response: str = ""
    final_status: ClaimStatus = "UNVERIFIED"
    kind: Literal["evidence", "reframe", "kill"] = "evidence"


class ClaimResolution(BaseModel):
    claim_id: str
    final_status: ClaimStatus = "UNVERIFIED"
    rationale: str = ""
    evidence: str = ""
    source: str = ""


class RequirementAssessment(BaseModel):
    requirement: str
    satisfied: bool | None = None
    evidence_claim_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    rationale: str = ""


class DimensionScore(BaseModel):
    name: str
    score: int = 0


class CriterionScore(BaseModel):
    criterion: str
    raw_score: int = Field(default=0, ge=0, le=100)
    confidence: Confidence = "low"
    evidence_claim_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    weighted_contribution: float = 0.0


class OptionScore(BaseModel):
    name: str
    overall: float | None = None
    criterion_breakdown: list[CriterionScore] = Field(default_factory=list)
    requirement_satisfaction: list[RequirementAssessment] = Field(default_factory=list)
    confidence: Confidence = "low"
    rationale: str = ""
    unresolved_assumptions: list[str] = Field(default_factory=list)
    dimensions: list[DimensionScore] = Field(default_factory=list)
    disposition: Literal["lead", "survive", "dead"] = "survive"
    why: str = ""
    speculation: bool = False


class SensitivityScenario(BaseModel):
    name: str
    weights: dict[str, int] = Field(default_factory=dict)
    totals: dict[str, float | None] = Field(default_factory=dict)
    leading_option: str = ""
    leader_changed: bool = False


class RedTeamFinding(BaseModel):
    option: str = ""
    criterion: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    overturning_evidence: str = ""
    source: str = ""
    source_quality: str = "unknown"
    confidence: Confidence = "low"
    status: ClaimStatus = "UNVERIFIED"
    resolution: str = ""


class ScoreReport(BaseModel):
    message: str = ""
    options: list[OptionScore] = Field(default_factory=list)
    conclusion: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
    recommendation: str = ""
    requirement_matrix: dict[str, dict[str, bool | None]] = Field(default_factory=dict)
    confidence: Confidence = "low"
    unresolved_assumptions: list[str] = Field(default_factory=list)
    sensitivity: list[SensitivityScenario] = Field(default_factory=list)
    red_team_findings: list[RedTeamFinding] = Field(default_factory=list)
    winner: str = ""
    unresolved: list[str] = Field(default_factory=list)


class ChamberEvent(BaseModel):
    speaker: str
    kind: Literal[
        "opening",
        "challenge",
        "resolution",
        "score",
        "red_team",
        "progress",
        "brief",
    ]
    text: str
    claims: list[Claim] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list)
    resolutions: list[ClaimResolution] = Field(default_factory=list)


class ImagePayload(BaseModel):
    mime_type: str
    data: str


class ChatRequest(BaseModel):
    session_id: str
    message: str = ""
    images: list[ImagePayload] = Field(default_factory=list)
    confirm_run: bool = False


class GuideTurn(BaseModel):
    message: str
    decision: str = ""
    question: str = ""
    user: str = ""
    context: str = ""
    status_quo: str = ""
    options: list[Option] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    complete: bool = False
    ready_to_run: bool = False
    user_wants_chamber: bool = False
    asked_a_question: bool = False


class OpeningTurn(BaseModel):
    speaker: str
    summary: str
    claims: list[Claim] = Field(default_factory=list)


class ChallengeTurn(BaseModel):
    speaker: str
    summary: str
    challenges: list[Challenge] = Field(default_factory=list)


class ResolutionTurn(BaseModel):
    summary: str = ""
    resolutions: list[ClaimResolution] = Field(default_factory=list)


class RawScoreTurn(BaseModel):
    options: list[OptionScore] = Field(default_factory=list)
    conclusion: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
    recommendation: str = ""
    unresolved_assumptions: list[str] = Field(default_factory=list)


class RedTeamTurn(BaseModel):
    summary: str = ""
    findings: list[RedTeamFinding] = Field(default_factory=list)
