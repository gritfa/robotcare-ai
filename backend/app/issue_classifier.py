from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


EvidenceKind = Literal["error_code", "keyword"]
DecisionKind = Literal["unclassified", "consistent", "conflict", "ambiguous"]


@dataclass(frozen=True)
class CategoryRule:
    category_code: str
    keywords: tuple[re.Pattern[str], ...]
    error_codes: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class CategoryCandidate:
    category_code: str
    score: int
    evidence_kind: EvidenceKind


@dataclass(frozen=True)
class CategoryDecision:
    kind: DecisionKind
    selected_category_code: str
    candidates: tuple[CategoryCandidate, ...]

    @property
    def suggested_category_code(self) -> str | None:
        return self.candidates[0].category_code if self.candidates else None

    def metadata(self, *, confirmation: str | None = None) -> dict[str, object]:
        return {
            "selected_category_code": self.selected_category_code,
            "candidate_category_codes": [
                candidate.category_code for candidate in self.candidates
            ],
            "final_category_code": self.selected_category_code,
            "decision": self.kind,
            "confirmation": confirmation,
        }


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


# Rules are intentionally model-scoped. A symptom from another model can never
# become a candidate unless that category also has a published flow for this model.
MODEL_RULES: dict[str, tuple[CategoryRule, ...]] = {
    "JH69U1": (
        CategoryRule(
            "return_to_dock_failure",
            _patterns(r"无法回充", r"回充失败", r"找不到(?:基站|充电座)", r"不能返回充电"),
            _patterns(r"^(?:DOCK|CHARGE)[-_ ]?(?:01|FAIL)$"),
        ),
        CategoryRule(
            "base_station_water_tank_issue",
            _patterns(
                r"无法清洗拖布",
                r"拖布(?:清洗|洗涤).{0,6}(?:失败|异常|不工作)",
                r"(?:清水箱|污水箱|清洗槽).{0,8}(?:异常|故障|缺水|满)",
            ),
            _patterns(r"^(?:TANK|MOP|WASH)[-_ ]?(?:01|FAIL)$"),
        ),
    ),
    "VC35U1": (
        CategoryRule(
            "wifi_setup_failure",
            _patterns(
                r"配网.{0,8}(?:失败|不成功|异常)",
                r"(?:wifi|wi-fi|无线网).{0,8}(?:连接不上|失败|异常)",
                r"无法联网",
            ),
            _patterns(r"^(?:WIFI|NET)[-_ ]?(?:01|FAIL)$"),
        ),
        CategoryRule(
            "cleaning_noise",
            _patterns(
                r"(?:清扫|运行|滚刷|边刷|轮子).{0,8}(?:异响|噪音|声音异常)",
                r"(?:异响|噪音).{0,8}(?:清扫|运行|滚刷|边刷|轮子)",
            ),
            _patterns(r"^(?:NOISE|BRUSH)[-_ ]?(?:01|FAIL)$"),
        ),
    ),
}


def classify_issue(
    *,
    model_code: str,
    selected_category_code: str,
    issue_description: str,
    error_code: str | None,
    available_category_codes: set[str],
) -> CategoryDecision:
    rules = MODEL_RULES.get(model_code, ())
    normalized_error = re.sub(r"\s+", "", error_code or "").upper()

    error_matches = [
        CategoryCandidate(rule.category_code, 100, "error_code")
        for rule in rules
        if rule.category_code in available_category_codes
        and normalized_error
        and any(pattern.search(normalized_error) for pattern in rule.error_codes)
    ]
    candidates = error_matches
    if not candidates:
        keyword_matches: list[CategoryCandidate] = []
        for rule in rules:
            if rule.category_code not in available_category_codes:
                continue
            match_count = sum(
                bool(pattern.search(issue_description)) for pattern in rule.keywords
            )
            if match_count:
                keyword_matches.append(
                    CategoryCandidate(rule.category_code, match_count, "keyword")
                )
        candidates = keyword_matches

    candidates.sort(key=lambda item: (-item.score, item.category_code))
    if not candidates:
        kind: DecisionKind = "unclassified"
    elif len(candidates) > 1 and candidates[0].score == candidates[1].score:
        kind = "ambiguous"
    elif candidates[0].category_code == selected_category_code:
        kind = "consistent"
    else:
        kind = "conflict"
    return CategoryDecision(kind, selected_category_code, tuple(candidates))
