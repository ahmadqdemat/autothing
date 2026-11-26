from __future__ import annotations

import copy
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import Any, Callable, Iterator

from app.gemini import QUESTION_CAP, generate_grounded_text, generate_json
from app.prompts import (
    EVIDENCE_ANALYST_SYSTEM,
    EVIDENCE_JUDGE_SYSTEM,
    CRITERIA_DISCOVERY_SYSTEM,
    GUIDE_SCHEMA,
    GUIDE_SYSTEM,
    RESEARCH_SYSTEM,
    RED_TEAM_SYSTEM,
    REQUIREMENTS_VALIDATOR_SYSTEM,
    SKEPTIC_SYSTEM,
)
from app.schemas import (
    ChamberEvent,
    ChallengeTurn,
    Claim,
    CriteriaDiscovery,
    Criterion,
    CriterionScore,
    DimensionScore,
    GuideTurn,
    ImagePayload,
    OpeningTurn,
    OptionResearch,
    OptionScore,
    RawScoreTurn,
    RedTeamFinding,
    RedTeamTurn,
    RequirementAssessment,
    ResearchOutput,
    ResolutionTurn,
    ScoreReport,
    SensitivityScenario,
    UseCaseCard,
)

EventSink = Callable[[dict[str, Any]], None]

MAX_IMAGES = 2
MAX_IMAGE_CHARS = 2_800_000
URL_RE = re.compile(r"https?://[^\s]+", re.I)
MODEL_RE = re.compile(r"\b[a-z]{0,8}\d{2,}[a-z0-9-]*\b", re.I)
HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "history.json"


@dataclass
class Session:
    id: str
    title: str = "New decision"
    phase: str = "interview"
    question_count: int = 0
    guide_interaction_id: str | None = None
    use_case: UseCaseCard = field(default_factory=UseCaseCard)
    ready_to_run: bool = False
    chamber_log: list[ChamberEvent] = field(default_factory=list)
    scores: ScoreReport | None = None
    criteria_proposed: bool = False
    messages: list[dict[str, str]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, Session] = {}
        self._running: set[str] = set()
        self._load()

    def create(self) -> Session:
        session = Session(id=uuid.uuid4().hex)
        with self._lock:
            self._sessions[session.id] = session
            self._save_locked()
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._running

    def begin_run(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._running:
                return False
            self._running.add(session_id)
            return True

    def end_run(self, session_id: str) -> None:
        with self._lock:
            self._running.discard(session_id)

    def clear_stale_run(self, session: Session) -> bool:
        if session.phase != "chamber" or session.scores is not None:
            return False
        if self.is_running(session.id):
            return False
        session.phase = "interview"
        session.ready_to_run = True
        session.chamber_log = []
        with self._lock:
            self._save_locked()
        return True

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [self._summary(session) for session in self._sessions.values()]
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        return rows

    def snapshot(self, session: Session) -> dict[str, Any]:
        return {
            "id": session.id,
            "title": session.title,
            "phase": session.phase,
            "question_count": session.question_count,
            "ready_to_run": session.ready_to_run,
            "use_case": session.use_case.model_dump(),
            "chamber_log": [event.model_dump() for event in session.chamber_log],
            "scores": session.scores.model_dump() if session.scores else None,
            "criteria_proposed": session.criteria_proposed,
            "messages": list(session.messages),
            "updated_at": session.updated_at,
            "running": session.id in self._running,
        }

    def touch(self, session: Session, title: str | None = None) -> None:
        session.updated_at = time.time()
        if title and session.title in {"New review", "New decision"}:
            session.title = title[:64]
        with self._lock:
            self._save_locked()

    def _summary(self, session: Session) -> dict[str, Any]:
        return {
            "id": session.id,
            "title": session.title,
            "phase": session.phase,
            "updated_at": session.updated_at,
            "running": session.id in self._running,
        }

    def _load(self) -> None:
        if not HISTORY_PATH.exists():
            return
        try:
            raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        dirty = False
        for item in raw:
            try:
                session = Session(
                    id=item["id"],
                    title=item.get("title", "New decision"),
                    phase=item.get("phase", "interview"),
                    question_count=item.get("question_count", 0),
                    ready_to_run=item.get("ready_to_run", False),
                    use_case=UseCaseCard.model_validate(item.get("use_case") or {}),
                    chamber_log=[
                        ChamberEvent.model_validate(event)
                        for event in item.get("chamber_log") or []
                    ],
                    scores=ScoreReport.model_validate(item["scores"])
                    if item.get("scores")
                    else None,
                    criteria_proposed=bool(item.get("criteria_proposed", False)),
                    messages=item.get("messages") or [],
                    updated_at=item.get("updated_at") or time.time(),
                )
                if session.phase == "chamber" and session.scores is None:
                    session.phase = "interview"
                    session.ready_to_run = True
                    session.chamber_log = []
                    dirty = True
                self._sessions[session.id] = session
            except Exception:
                continue
        if dirty:
            self._save_locked()

    def _save_locked(self) -> None:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = [self.snapshot(session) for session in self._sessions.values()]
        HISTORY_PATH.write_text(json.dumps(payload), encoding="utf-8")


store = SessionStore()


def _sanitize_images(images: list[ImagePayload]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for image in images[:MAX_IMAGES]:
        data = image.data
        mime = image.mime_type or "image/jpeg"
        if data.startswith("data:"):
            header, _, payload = data.partition(",")
            data = payload
            match = re.search(r"data:(image/[a-zA-Z0-9.+-]+)", header)
            if match:
                mime = match.group(1)
        if mime not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "image/heic",
            "image/heif",
        }:
            mime = "image/jpeg"
        if not data or len(data) > MAX_IMAGE_CHARS:
            continue
        cleaned.append({"data": data, "mime_type": mime})
    return cleaned


def _merge_card(card: UseCaseCard, turn: GuideTurn) -> UseCaseCard:
    if turn.decision.strip():
        card.decision = turn.decision.strip()
    if turn.question.strip():
        card.question = turn.question.strip()
    if turn.user.strip():
        card.user = turn.user.strip()
    if turn.context.strip():
        card.context = turn.context.strip()
    if turn.status_quo.strip():
        card.status_quo = turn.status_quo.strip()
    if turn.options:
        card.options = turn.options
    if turn.requirements:
        card.requirements = [item for item in turn.requirements if item.strip()]
    if turn.preferences:
        card.preferences = [item for item in turn.preferences if item.strip()]
    if turn.constraints:
        card.constraints = [item for item in turn.constraints if item.strip()]
    if turn.criteria:
        previous = {item.name.casefold(): item for item in card.criteria}
        merged: list[Criterion] = []
        for item in turn.criteria:
            prior = previous.get(item.name.casefold())
            if prior and not item.source.strip():
                item.source = prior.source
            if prior and not item.description.strip():
                item.description = prior.description
            if prior and item.weight == 0 and prior.weight != 0:
                item.weight = prior.weight
            merged.append(item)
        card.criteria = merged
        if card.criteria_origin != "evidence":
            card.criteria_origin = "user"
    _finalize_weights(card)
    card = UseCaseCard.model_validate(card.model_dump())
    card.missing_fields = _missing(card)
    card.complete = not card.missing_fields
    return card


def _apply_equal_weights(criteria: list[Criterion]) -> None:
    count = len(criteria)
    if count == 0:
        return
    base, extra = divmod(100, count)
    for index, item in enumerate(criteria):
        item.weight = base + (1 if index < extra else 0)


def _weights_sum_valid(criteria: list[Criterion]) -> bool:
    return bool(criteria) and all(0 <= item.weight <= 100 for item in criteria) and (
        sum(item.weight for item in criteria) == 100
    )


def _is_equal_split(criteria: list[Criterion]) -> bool:
    if not criteria:
        return False
    weights = [item.weight for item in criteria]
    return max(weights) - min(weights) <= 1 and sum(weights) == 100


def _finalize_weights(card: UseCaseCard) -> None:
    if not card.criteria:
        return
    if all(item.weight == 0 for item in card.criteria):
        _apply_equal_weights(card.criteria)
        card.weights_origin = "equal_default"
        return
    if not _weights_sum_valid(card.criteria):
        card.weights_origin = ""
        return
    if _is_equal_split(card.criteria) and card.weights_origin == "equal_default":
        return
    card.weights_origin = "user"


def _missing(card: UseCaseCard) -> list[str]:
    missing: list[str] = []
    if not (card.decision.strip() or card.question.strip()):
        missing.append("decision")
    if len(card.options) < 2:
        missing.append("options")
    if not card.criteria:
        missing.append("criteria")
    elif not _weights_sum_valid(card.criteria):
        missing.append("weights")
    return missing


def _looks_like_go(message: str) -> bool:
    text = message.strip().lower()
    return text in {
        "go",
        "yes",
        "y",
        "ok",
        "okay",
        "start",
        "run",
        "do it",
        "start the chamber",
        "start the debate",
        "start deliberation",
        "start the deliberation",
        "run it",
        "go ahead",
        "yes start",
        "yes, start",
    } or text.startswith("start the")


def _card_json(card: UseCaseCard) -> str:
    return json.dumps(card.model_dump(), indent=2)


def _guide_tools(text: str) -> list[dict[str, str]] | None:
    tools: list[dict[str, str]] = []
    if URL_RE.search(text or ""):
        tools.append({"type": "url_context"})
    if MODEL_RE.search(text or ""):
        tools.append({"type": "google_search"})
    return tools or None


def _run_guide(
    session: Session, user_text: str, images: list[dict[str, str]] | None
) -> GuideTurn:
    follow = ""
    if session.scores:
        follow = (
            "\nScores already exist. Answer follow-ups from them. "
            "Only set user_wants_chamber true if they explicitly ask to rescore.\n"
            f"{session.scores.model_dump_json(indent=2)}"
        )
    recent = session.messages[-8:]
    history = "\n".join(f"{item['role']}: {item['text']}" for item in recent) or "(none)"
    prompt = (
        f"Current DecisionBrief:\n{_card_json(session.use_case)}\n\n"
        f"Recent thread:\n{history}\n\n"
        f"Server question count: {session.question_count} (cap {QUESTION_CAP}).\n"
        f"Phase: {session.phase}.\n"
        f"{follow}\n"
        f"User message:\n{user_text}"
    )
    data, interaction_id = generate_json(
        system=GUIDE_SYSTEM,
        prompt=prompt,
        schema=GUIDE_SCHEMA,
        images=images,
        previous_interaction_id=session.guide_interaction_id,
        tools=_guide_tools(user_text),
    )
    if interaction_id:
        session.guide_interaction_id = interaction_id
    return GuideTurn.model_validate(data)


def _json_call(
    system: str,
    prompt: str,
    model: type,
    tools: list[dict[str, str]] | None = None,
    schema: dict[str, Any] | None = None,
) -> Any:
    data, _ = generate_json(
        system=system,
        prompt=prompt,
        schema=schema or model.model_json_schema(),
        tools=tools,
    )
    return model.model_validate(data)


def _score_schema(card: UseCaseCard) -> dict[str, Any]:
    schema = RawScoreTurn.model_json_schema()
    defs = schema["$defs"]
    rows = []
    for criterion in card.criteria:
        row = copy.deepcopy(defs["CriterionScore"])
        row["properties"]["criterion"] = {"type": "string", "enum": [criterion.name]}
        row["required"] = [
            "criterion",
            "raw_score",
            "confidence",
            "evidence_claim_ids",
            "rationale",
        ]
        rows.append(row)
    options = []
    for option in card.options:
        item = copy.deepcopy(defs["OptionScore"])
        item["properties"]["name"] = {"type": "string", "enum": [option.name]}
        item["properties"]["criterion_breakdown"] = {
            "type": "array",
            "prefixItems": copy.deepcopy(rows),
            "minItems": len(rows),
            "maxItems": len(rows),
        }
        item["required"] = ["name", "criterion_breakdown", "confidence", "rationale"]
        options.append(item)
    schema["properties"]["options"] = {
        "type": "array",
        "prefixItems": options,
        "minItems": len(options),
        "maxItems": len(options),
    }
    schema["required"] = ["options"]
    return schema


def _unverified_research(card: UseCaseCard) -> ResearchOutput:
    coverage = list(card.requirements) + [item.name for item in card.criteria]
    options: list[OptionResearch] = []
    for option_index, option in enumerate(card.options):
        claims = [
            Claim(
                id=f"unverified_{option_index}_{item_index}",
                option=option.name,
                statement=f"Evidence is unavailable for {option.name} against {item}.",
                claim_type="UNCERTAINTY",
                assumptions=["No grounded source was available."],
                confidence="low",
                counterargument="Grounded evidence may materially change this assessment.",
                status="UNVERIFIED",
            )
            for item_index, item in enumerate(coverage)
        ]
        options.append(
            OptionResearch(option=option.name, claims=claims, coverage=coverage)
        )
    return ResearchOutput(
        options=options,
        grounded=False,
        limitations=["Grounded search was unavailable; all claims remain unverified."],
    )


def _proposal_message(criteria: list[Criterion]) -> str:
    described = [
        f"{item.name} ({item.source})" if item.source else item.name for item in criteria
    ]
    return (
        "You didn't set criteria, so I checked reviews and comparisons. "
        "People mostly compare these on: "
        + "; ".join(described)
        + ". I would weight them equally, since you didn't rank them. "
        "Use these, or tell me what to change?"
    )


def _discover_criteria(card: UseCaseCard) -> list[Criterion]:
    prompt = (
        f"Decision brief:\n{_card_json(card)}\n\n"
        "Search reviews and owner discussions. Which criteria do they use to compare "
        "these options?"
    )
    try:
        grounded = generate_grounded_text(system=CRITERIA_DISCOVERY_SYSTEM, prompt=prompt)
    except Exception:
        return []
    try:
        found = _json_call(
            CRITERIA_DISCOVERY_SYSTEM,
            f"Decision brief:\n{_card_json(card)}\n\n"
            f"Search notes:\n{grounded}\n\n"
            "Return 2 to 6 criteria that these notes attribute to reviews or owner "
            "discussions. Drop any criterion that has no source.",
            CriteriaDiscovery,
        )
    except Exception:
        return []
    criteria: list[Criterion] = []
    seen: set[str] = set()
    for item in found.criteria:
        name = " ".join(item.name.split())
        source = " ".join(item.source.split())
        if not name or not source:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        criteria.append(
            Criterion(
                name=name,
                weight=0,
                description=item.description.strip(),
                source=source,
            )
        )
        if len(criteria) == 6:
            break
    if len(criteria) < 2:
        return []
    return criteria


def _offer_evidence_criteria(session: Session) -> str | None:
    card = session.use_case
    if session.phase == "scored" or session.criteria_proposed or card.criteria:
        return None
    if _missing(card) != ["criteria"]:
        return None
    proposed = _discover_criteria(card)
    session.criteria_proposed = True
    if not proposed:
        return (
            "I looked for review and comparison coverage and couldn't derive criteria from it. "
            "Name the factors you care about if you want a scored comparison."
        )
    card.criteria = proposed
    card.criteria_origin = "evidence"
    card.weights_origin = "equal_default"
    _apply_equal_weights(card.criteria)
    card.missing_fields = _missing(card)
    card.complete = not card.missing_fields
    return _proposal_message(card.criteria)


def _run_structured_research(card: UseCaseCard) -> ResearchOutput:
    prompt = (
        f"Decision brief:\n{_card_json(card)}\n\n"
        "Search once for checkable evidence. Cover every option independently against "
        "every requirement and criterion. Return source citations and do not rank."
    )
    try:
        grounded_text = generate_grounded_text(system=RESEARCH_SYSTEM, prompt=prompt)
        if not grounded_text.strip():
            return _unverified_research(card)
        structured = _json_call(
            EVIDENCE_ANALYST_SYSTEM,
            f"Decision brief:\n{_card_json(card)}\n\n"
            f"Grounded research:\n{grounded_text}\n\n"
            "Organize this evidence independently per named option. Include atomic "
            "claims with every epistemic field and explicit coverage.",
            ResearchOutput,
        )
        expected = {option.name for option in card.options}
        found = {option.option for option in structured.options}
        if expected != found:
            raise ValueError("Research output did not cover every named option.")
        structured.grounded = True
        return structured
    except Exception:
        return _unverified_research(card)


def _run_evidence_claims(
    card: UseCaseCard, research: ResearchOutput
) -> list[OpeningTurn]:
    common = (
        f"Decision brief:\n{_card_json(card)}\n\n"
        f"Structured research:\n{research.model_dump_json(indent=2)}\n\n"
        "Every claim's option field must be one of these names, verbatim: "
        f"{json.dumps([item.name for item in card.options])}. "
        "A point that applies to several options is repeated once per option; never "
        "write a grouping label such as \"all options\".\n\n"
    )
    analyst = _json_call(
        EVIDENCE_ANALYST_SYSTEM,
        common
        + "Evaluate each option independently. Cover every requirement and criterion "
        "with atomic, non-persuasive claims.",
        OpeningTurn,
    )
    analyst.speaker = "Evidence Analyst"
    validator = _json_call(
        REQUIREMENTS_VALIDATOR_SYSTEM,
        common
        + "Validate only satisfaction of the user's stated requirements. Do not invent "
        "technical evidence.",
        OpeningTurn,
    )
    validator.speaker = "Requirements Validator"
    return [analyst, validator]


def _run_skeptic(
    card: UseCaseCard, claims: list[OpeningTurn]
) -> ChallengeTurn:
    turn = _json_call(
        SKEPTIC_SYSTEM,
        f"Decision brief:\n{_card_json(card)}\n\n"
        f"Claims:\n{json.dumps([item.model_dump() for item in claims], indent=2)}\n\n"
        "Challenge only specific claim ids. Do not write generic arguments.",
        ChallengeTurn,
    )
    turn.speaker = "Skeptic"
    return turn


def _run_resolution(
    card: UseCaseCard,
    claims: list[OpeningTurn],
    challenges: ChallengeTurn,
) -> ResolutionTurn:
    return _json_call(
        EVIDENCE_JUDGE_SYSTEM,
        f"Decision brief:\n{_card_json(card)}\n\n"
        f"Claims:\n{json.dumps([item.model_dump() for item in claims], indent=2)}\n\n"
        f"Challenges:\n{challenges.model_dump_json(indent=2)}\n\n"
        "Resolve every claim id neutrally before scoring, including unchallenged claims. "
        "Agreement or lack of a challenge is not evidence.",
        ResolutionTurn,
    )


def _run_raw_scores(
    card: UseCaseCard,
    claims: list[OpeningTurn],
    resolution: ResolutionTurn,
    red_team: RedTeamTurn | None = None,
    correction: str = "",
) -> RawScoreTurn:
    extra = (
        f"\nRed-team findings to resolve in the final scoring:\n"
        f"{red_team.model_dump_json(indent=2)}"
        if red_team
        else ""
    )
    options = json.dumps([item.name for item in card.options])
    criteria = json.dumps([item.name for item in card.criteria])
    return _json_call(
        EVIDENCE_JUDGE_SYSTEM,
        f"Decision brief:\n{_card_json(card)}\n\n"
        f"Claims:\n{json.dumps([item.model_dump() for item in claims], indent=2)}\n\n"
        f"Claim resolutions:\n{resolution.model_dump_json(indent=2)}"
        f"{extra}\n\nScore exactly these options, using these names verbatim: {options}. "
        f"Give each one a row for exactly these criteria, verbatim: {criteria}. "
        "Add no other option and no other criterion; a claim written against a group of "
        "options is evidence about each named option, not an option of its own. "
        "Do not calculate weighted totals and do not force a winner."
        f"{correction}",
        RawScoreTurn,
        schema=_score_schema(card),
    )


def _scored_report(
    card: UseCaseCard,
    claims: list[OpeningTurn],
    resolution: ResolutionTurn,
    red_team: RedTeamTurn | None = None,
) -> ScoreReport:
    raw = _run_raw_scores(card, claims, resolution, red_team)
    try:
        return _compute_score_report(card, raw, red_team.findings if red_team else None)
    except ValueError as exc:
        retry = _run_raw_scores(
            card,
            claims,
            resolution,
            red_team,
            correction=(
                f"\n\nYour previous answer was rejected: {exc} "
                "Return one row per listed option and one row per listed criterion, "
                "using the exact names given above."
            ),
        )
        return _compute_score_report(
            card, retry, red_team.findings if red_team else None
        )


def _emphasized_weights(criteria: list[Criterion], target: int) -> dict[str, int]:
    base = {item.name: item.weight for item in criteria}
    if len(criteria) < 2 or base[criteria[target].name] == 100:
        return base
    target_name = criteria[target].name
    increase = min(20, 100 - base[target_name])
    donors = [item.name for index, item in enumerate(criteria) if index != target]
    remaining = increase
    result = dict(base)
    for index, name in enumerate(donors):
        available = result[name]
        share = remaining if index == len(donors) - 1 else min(
            available, round(increase * base[name] / max(1, 100 - base[target_name]))
        )
        share = min(share, available, remaining)
        result[name] -= share
        remaining -= share
    if remaining:
        for name in donors:
            take = min(result[name], remaining)
            result[name] -= take
            remaining -= take
            if not remaining:
                break
    result[target_name] += increase - remaining
    return result


def _leader(totals: dict[str, float | None]) -> str:
    scored = {name: score for name, score in totals.items() if score is not None}
    if not scored:
        return ""
    best = max(scored.values())
    leaders = [name for name, score in scored.items() if score == best]
    return leaders[0] if len(leaders) == 1 else ""


def _sensitivity(
    card: UseCaseCard, options: list[OptionScore]
) -> list[SensitivityScenario]:
    scores = {
        option.name: {item.criterion: item.raw_score for item in option.criterion_breakdown}
        for option in options
    }
    scenarios = [("Base weights", {item.name: item.weight for item in card.criteria})]
    scenarios.extend(
        (
            f"Emphasize {criterion.name}",
            _emphasized_weights(card.criteria, index),
        )
        for index, criterion in enumerate(card.criteria)
    )
    output: list[SensitivityScenario] = []
    base_leader = ""
    for name, weights in scenarios:
        totals = {
            option.name: (
                round(
                    sum(
                        scores[option.name][criterion.name] * weights[criterion.name] / 100
                        for criterion in card.criteria
                    ),
                    2,
                )
                if option.overall is not None
                else None
            )
            for option in options
        }
        leader = _leader(totals)
        if not output:
            base_leader = leader
        output.append(
            SensitivityScenario(
                name=name,
                weights=weights,
                totals=totals,
                leading_option=leader,
                leader_changed=bool(base_leader and leader and leader != base_leader),
            )
        )
    return output


def _compute_score_report(
    card: UseCaseCard,
    raw: RawScoreTurn,
    red_team_findings: list[RedTeamFinding] | None = None,
) -> ScoreReport:
    missing = _missing(card)
    if missing:
        raise ValueError(f"Decision brief is not scoreable: {', '.join(missing)}")
    expected_options = [item.name for item in card.options]
    expected_criteria = [item.name for item in card.criteria]
    normalize = lambda value: " ".join(value.split()).casefold()
    by_name: dict[str, OptionScore] = {}
    for item in raw.options:
        by_name.setdefault(normalize(item.name), item)
    absent = [name for name in expected_options if normalize(name) not in by_name]
    if absent:
        returned = ", ".join(item.name for item in raw.options) or "(none)"
        raise ValueError(
            f"Scoring did not cover these options: {', '.join(absent)}. Returned: {returned}"
        )
    scored: list[OptionScore] = []
    for option_name in expected_options:
        source = by_name[normalize(option_name)]
        source.name = option_name
        breakdown: dict[str, CriterionScore] = {}
        for item in source.criterion_breakdown:
            breakdown.setdefault(normalize(item.criterion), item)
        absent_criteria = [
            name for name in expected_criteria if normalize(name) not in breakdown
        ]
        if absent_criteria:
            raise ValueError(
                f"Scoring for {option_name} did not cover these criteria: "
                f"{', '.join(absent_criteria)}"
            )
        thin = True
        ordered: list[CriterionScore] = []
        for criterion in card.criteria:
            item = breakdown[normalize(criterion.name)]
            item.criterion = criterion.name
            if item.raw_score in (0, 100) and not any(
                word in item.rationale.lower()
                for word in ("literal", "all", "none", "impossible", "fully")
            ):
                raise ValueError("Extreme scores require an all-or-nothing rationale.")
            item.weighted_contribution = round(
                item.raw_score * criterion.weight / 100, 2
            )
            thin = thin and item.confidence == "low" and not item.evidence_claim_ids
            ordered.append(item)
        overall = None if thin else round(
            sum(item.weighted_contribution for item in ordered), 2
        )
        source.criterion_breakdown = ordered
        source.overall = overall
        source.confidence = (
            "low"
            if any(item.confidence == "low" for item in ordered)
            else "medium"
            if any(item.confidence == "medium" for item in ordered)
            else "high"
        )
        source.dimensions = [
            DimensionScore(name=item.criterion, score=item.raw_score)
            for item in ordered
        ]
        assessments = {
            item.requirement: item for item in source.requirement_satisfaction
        }
        source.requirement_satisfaction = [
            assessments.get(
                requirement,
                RequirementAssessment(
                    requirement=requirement,
                    satisfied=None,
                    confidence="low",
                    rationale="No supported determination was returned.",
                ),
            )
            for requirement in card.requirements
        ]
        source.why = source.rationale
        source.speculation = thin or source.confidence == "low"
        scored.append(source)
    matrix = {
        option.name: {
            assessment.requirement: assessment.satisfied
            for assessment in option.requirement_satisfaction
        }
        for option in scored
    }
    report = ScoreReport(
        message=raw.conclusion,
        options=scored,
        conclusion=raw.conclusion,
        tradeoffs=raw.tradeoffs,
        recommendation=raw.recommendation,
        requirement_matrix=matrix,
        confidence=(
            "low"
            if any(option.confidence == "low" for option in scored)
            else "medium"
            if any(option.confidence == "medium" for option in scored)
            else "high"
        ),
        unresolved_assumptions=raw.unresolved_assumptions,
        unresolved=raw.unresolved_assumptions,
        red_team_findings=red_team_findings or [],
    )
    report.sensitivity = _sensitivity(card, scored)
    return report


def _run_red_team(
    card: UseCaseCard, preliminary: ScoreReport, claims: list[OpeningTurn]
) -> RedTeamTurn:
    return _json_call(
        RED_TEAM_SYSTEM,
        f"Decision brief:\n{_card_json(card)}\n\n"
        f"Preliminary scoring:\n{preliminary.model_dump_json(indent=2)}\n\n"
        f"Claims:\n{json.dumps([item.model_dump() for item in claims], indent=2)}\n\n"
        "Find only the strongest checkable evidence that could overturn the preliminary "
        "conclusion. Reference claims, criteria, and options.",
        RedTeamTurn,
        tools=[{"type": "google_search"}],
    )


def _progress(session: Session, emit: EventSink, speaker: str, text: str) -> None:
    emit({"type": "progress", "speaker": speaker, "text": text})
    if session.phase != "chamber":
        return
    event = ChamberEvent(speaker=speaker, kind="progress", text=text)
    session.chamber_log.append(event)
    emit({"type": "chamber", **event.model_dump()})
    store.touch(session)


def _run_chamber(session: Session, emit: EventSink) -> None:
    missing = _missing(session.use_case)
    if missing:
        session.phase = "interview"
        session.ready_to_run = False
        emit({"type": "phase", "phase": "interview"})
        emit(
            {
                "type": "guide",
                "message": "I need more information before analysis can run: "
                + ", ".join(missing)
                + ".",
                "use_case": session.use_case.model_dump(),
                "phase": "interview",
                "ready_to_run": False,
                "question_count": session.question_count,
            }
        )
        return
    session.phase = "chamber"
    emit({"type": "phase", "phase": "chamber"})
    card = session.use_case
    _progress(session, emit, "Evidence Analyst", "Researching each option independently.")
    research = _run_structured_research(card)
    brief_event = ChamberEvent(
        speaker="Evidence Analyst",
        kind="brief",
        text=research.model_dump_json(),
        claims=[claim for option in research.options for claim in option.claims],
    )
    session.chamber_log.append(brief_event)
    emit({"type": "chamber", **brief_event.model_dump()})
    if research.grounded:
        _progress(session, emit, "Evidence Analyst", "Grounded option research is complete.")
    else:
        _progress(session, emit, "Evidence Analyst", "Search unavailable; claims are unverified.")

    _progress(session, emit, "Evidence Analyst", "Building atomic evidence claims.")
    claims = _run_evidence_claims(card, research)
    for turn in claims:
        event = ChamberEvent(
            speaker=turn.speaker,
            kind="opening",
            text=turn.summary,
            claims=turn.claims,
        )
        session.chamber_log.append(event)
        emit({"type": "chamber", **event.model_dump()})

    _progress(session, emit, "Skeptic", "Challenging specific claim ids.")
    challenges = _run_skeptic(card, claims)
    challenge_event = ChamberEvent(
        speaker="Skeptic",
        kind="challenge",
        text=challenges.summary,
        challenges=challenges.challenges,
    )
    session.chamber_log.append(challenge_event)
    emit({"type": "chamber", **challenge_event.model_dump()})

    _progress(session, emit, "Evidence Judge", "Resolving challenged claims neutrally.")
    resolution = _run_resolution(card, claims, challenges)
    resolution_event = ChamberEvent(
        speaker="Evidence Judge",
        kind="resolution",
        text=resolution.summary,
        resolutions=resolution.resolutions,
    )
    session.chamber_log.append(resolution_event)
    emit({"type": "chamber", **resolution_event.model_dump()})

    _progress(session, emit, "Evidence Judge", "Computing preliminary criterion scores.")
    preliminary = _scored_report(card, claims, resolution)
    preliminary_event = ChamberEvent(
        speaker="Evidence Judge",
        kind="score",
        text="Preliminary scoring complete; totals were computed from criterion weights.",
    )
    session.chamber_log.append(preliminary_event)
    emit(
        {
            "type": "chamber",
            **preliminary_event.model_dump(),
            "report": preliminary.model_dump(),
        }
    )

    _progress(session, emit, "Skeptic", "Red-teaming the preliminary conclusion.")
    red_team = _run_red_team(card, preliminary, claims)
    red_event = ChamberEvent(
        speaker="Skeptic",
        kind="red_team",
        text=red_team.summary,
    )
    session.chamber_log.append(red_event)
    emit(
        {
            "type": "chamber",
            **red_event.model_dump(),
            "findings": [item.model_dump() for item in red_team.findings],
        }
    )

    _progress(session, emit, "Evidence Judge", "Resolving red-team findings and rescoring once.")
    report = _scored_report(card, claims, resolution, red_team)
    session.scores = report
    session.phase = "scored"
    session.ready_to_run = False
    score_event = ChamberEvent(speaker="Evidence Judge", kind="score", text=report.message)
    session.chamber_log.append(score_event)
    _progress(session, emit, "Evidence Judge", "Final weighted scoring is complete.")
    emit({"type": "scores", "report": report.model_dump(), "phase": "scored"})
    emit(
        {
            "type": "guide",
            "message": report.message,
            "use_case": session.use_case.model_dump(),
            "phase": session.phase,
            "ready_to_run": False,
            "question_count": session.question_count,
        }
    )


def handle_chat(
    session_id: str,
    message: str,
    images: list[ImagePayload],
    confirm_run: bool,
) -> Iterator[dict[str, Any]]:
    events: Queue[dict[str, Any] | None] = Queue()

    def emit(event: dict[str, Any]) -> None:
        event["session_id"] = session_id
        events.put(event)

    def start_debate(session: Session, note: str) -> None:
        if not store.begin_run(session.id):
            _progress(
                session,
                emit,
                "Guide",
                "The deliberation is already running. You can switch chats — it will keep going.",
            )
            return
        try:
            session.phase = "chamber"
            _progress(session, emit, "Guide", note)
            _run_chamber(session, emit)
        finally:
            store.end_run(session.id)

    def work() -> None:
        try:
            session = store.get(session_id)
            user_text = (message or "").strip()
            if not user_text and not images and not confirm_run:
                emit({"type": "error", "message": "Describe the decision, or attach a screenshot."})
                return

            if user_text:
                session.messages.append({"role": "user", "text": user_text})
                store.touch(session, user_text)

            if session.phase == "chamber" or store.is_running(session_id):
                _progress(
                    session,
                    emit,
                    "Guide",
                    "The deliberation is already running. You can switch chats — it will keep going.",
                )
                return

            should_run = confirm_run or (
                session.ready_to_run and _looks_like_go(user_text)
            )
            if session.phase == "scored" and confirm_run:
                should_run = True

            if should_run:
                missing = _missing(session.use_case)
                if missing:
                    session.phase = "interview"
                    session.ready_to_run = False
                    emit(
                        {
                            "type": "guide",
                            "message": "Before analysis can start, please provide: "
                            + ", ".join(missing)
                            + ".",
                            "use_case": session.use_case.model_dump(),
                            "phase": "interview",
                            "ready_to_run": False,
                            "question_count": session.question_count,
                        }
                    )
                    return
                start_debate(
                    session,
                    "Starting research before claim evaluation.",
                )
                return

            _progress(session, emit, "Guide", "Reading your message and updating the decision brief.")
            turn = _run_guide(
                session, user_text or "(attached images)", _sanitize_images(images)
            )
            session.use_case = _merge_card(session.use_case, turn)
            proposal: str | None = None
            if (
                session.phase != "scored"
                and not session.criteria_proposed
                and not session.use_case.criteria
                and _missing(session.use_case) == ["criteria"]
            ):
                _progress(
                    session,
                    emit,
                    "Guide",
                    "Checking reviews for what people compare these on.",
                )
                proposal = _offer_evidence_criteria(session)
            if turn.asked_a_question:
                session.question_count += 1
            session.ready_to_run = bool(
                (turn.ready_to_run or session.use_case.complete)
                and not _missing(session.use_case)
            )
            shown = proposal if proposal is not None else turn.message
            session.messages.append({"role": "guide", "text": shown})
            emit(
                {
                    "type": "guide",
                    "message": shown,
                    "use_case": session.use_case.model_dump(),
                    "phase": session.phase,
                    "ready_to_run": session.ready_to_run,
                    "question_count": session.question_count,
                }
            )
            if (
                session.ready_to_run
                and (confirm_run or _looks_like_go(user_text))
            ):
                start_debate(session, "You confirmed. Starting evidence-based deliberation.")
        except KeyError:
            emit({"type": "error", "message": "Session expired. Refresh and start again."})
        except Exception as exc:
            try:
                failed = store.get(session_id)
                store.end_run(session_id)
                if failed.phase == "chamber" and failed.scores is None:
                    failed.phase = "interview"
                    failed.ready_to_run = True
            except Exception:
                pass
            emit({"type": "error", "message": str(exc)})
        finally:
            try:
                session = store.get(session_id)
                store.touch(session)
                emit({"type": "done", "snapshot": store.snapshot(session)})
            except Exception:
                emit({"type": "done"})
            events.put(None)

    Thread(target=work, daemon=True).start()
    while True:
        item = events.get()
        if item is None:
            break
        yield item
