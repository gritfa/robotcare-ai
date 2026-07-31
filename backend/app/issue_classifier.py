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
        CategoryRule(
            "power_on_failure",
            _patterns(r"无法开机", r"开不了机", r"按开机键.{0,6}没(?:有)?反应", r"开机没反应"),
        ),
        CategoryRule(
            "cleaning_noise",
            _patterns(
                r"(?:清扫|扫地|运行|主刷|边刷|轮子).{0,8}(?:异响|噪音|声音异常)",
                r"(?:异响|噪音).{0,8}(?:清扫|扫地|运行|主刷|边刷|轮子)",
            ),
        ),
        CategoryRule(
            "suction_drop",
            _patterns(r"吸力(?:变小|下降|变弱|不足)", r"吸不干净", r"扫不干净", r"扫地能力下降", r"掉灰"),
        ),
        CategoryRule(
            "device_offline",
            # 刻意不含"配网/联网"词：配网失败类症状不应跨界成为候选
            _patterns(r"(?:设备|主机).{0,6}离线", r"离线状态", r"无法连接\s*APP", r"APP.{0,8}(?:连不上|无法连接)"),
        ),
        CategoryRule(
            "schedule_not_run",
            _patterns(
                r"预约清洁.{0,8}(?:不工作|不启动|失败|没执行)",
                r"定时(?:清扫|清洁).{0,8}(?:不执行|不工作|不启动|没反应)",
            ),
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
        CategoryRule(
            "power_on_failure",
            _patterns(r"无法开机", r"开不了机", r"按开机键.{0,6}没(?:有)?反应", r"开机没反应"),
        ),
        CategoryRule(
            "return_to_dock_failure",
            _patterns(r"无法回充", r"回充失败", r"找不到(?:基站|充电座)", r"不能返回充电", r"无法返回充电"),
        ),
        CategoryRule(
            "suction_drop",
            _patterns(r"吸力(?:变小|下降|变弱|不足)", r"吸不干净", r"扫不干净", r"清扫能力下降"),
        ),
        CategoryRule(
            "schedule_not_run",
            _patterns(
                r"定时(?:清扫|清洁).{0,8}(?:不执行|不工作|不启动|没反应)",
                r"预约清洁.{0,8}(?:不工作|不启动|失败|没执行)",
            ),
        ),
        CategoryRule(
            "battery_runtime_decline",
            _patterns(r"续航(?:变短|下降|变差)", r"电池不耐用", r"用一会.{0,4}就没电", r"电量掉得(?:快|很快)"),
        ),
    ),
    # ---- 以下为 D1 合成演示型号（synthetic demo）。规则与 knowledge/ 合成流程同源维护。 ----
    "RC-S200": (
        CategoryRule(
            "return_to_dock_failure",
            _patterns(r"无法回充", r"回充失败", r"找不到(?:基站|充电座)", r"回不了(?:基站|充电座)"),
        ),
        CategoryRule(
            "wifi_setup_failure",
            _patterns(r"配网.{0,8}(?:失败|不成功|异常)", r"(?:wifi|wi-fi|无线网).{0,8}(?:连不上|连接不上|失败)", r"无法联网"),
        ),
        CategoryRule(
            "cleaning_noise",
            _patterns(r"(?:清扫|运行|滚刷|边刷|轮子).{0,8}(?:异响|噪音|声音异常)", r"(?:异响|噪音)(?:很大|异常|变大)"),
        ),
        CategoryRule(
            "suction_drop",
            _patterns(r"吸力(?:变小|下降|变弱|不足)", r"吸不干净", r"扫不干净"),
        ),
        CategoryRule(
            "mop_water_issue",
            _patterns(r"拖布不出水", r"拖地.{0,6}(?:没有水|不出水)", r"出水.{0,4}(?:异常|太少)"),
        ),
        CategoryRule(
            "power_on_failure",
            _patterns(r"无法开机", r"开不了机", r"按.{0,4}(?:没反应|无反应)", r"开机没反应"),
        ),
        CategoryRule(
            "stuck_navigation",
            _patterns(r"(?:经常|总是|老是).{0,4}(?:被困|卡住)", r"被(?:线|线缆|电线).{0,4}(?:缠住|困住)", r"过不去门槛"),
        ),
        CategoryRule(
            "error_code_drive",
            _patterns(r"(?:驱动轮|轮子).{0,6}(?:报错|错误|异常提示)"),
            _patterns(r"^E[-_ ]?01$"),
        ),
        CategoryRule(
            "battery_runtime_decline",
            _patterns(r"续航(?:变短|缩短|下降|明显变差)", r"电池不耐用", r"(?:一会|很快)就没电"),
        ),
        CategoryRule(
            "schedule_not_run",
            _patterns(
                r"定时(?:清扫|打扫|任务).{0,6}(?:没有执行|不执行|没执行|不启动|没启动)",
                r"预约(?:清扫|打扫).{0,4}不(?:启动|执行)",
            ),
        ),
    ),
    "RC-M500": (
        CategoryRule(
            "return_to_dock_failure",
            _patterns(r"无法回充", r"回充失败", r"找不到基站", r"回不了基站"),
        ),
        CategoryRule(
            "wifi_setup_failure",
            _patterns(r"配网.{0,8}(?:失败|不成功|异常)", r"(?:wifi|wi-fi|无线网).{0,8}(?:连不上|连接不上|失败)", r"无法联网"),
        ),
        CategoryRule(
            "base_station_water_tank_issue",
            _patterns(
                r"(?:清洗拖布|洗拖布).{0,6}不进水",
                r"基站.{0,6}不进水",
                r"清水箱.{0,8}(?:异常|缺水)",
            ),
        ),
        CategoryRule(
            "dirty_tank_alarm",
            _patterns(r"污水箱.{0,8}(?:报警|满|异常)"),
            _patterns(r"^E[-_ ]?08$"),
        ),
        CategoryRule(
            "dust_collection_issue",
            _patterns(r"不集尘", r"集尘.{0,6}(?:失败|无力|异常)", r"集尘袋.{0,6}(?:不工作|没反应)"),
        ),
        CategoryRule(
            "cleaning_noise",
            _patterns(r"(?:清扫|运行|滚刷|边刷|轮子).{0,8}(?:异响|噪音|声音异常)", r"(?:异响|噪音)(?:很大|异常|变大)"),
        ),
        CategoryRule(
            "suction_drop",
            _patterns(r"吸力(?:变小|下降|变弱|不足)", r"吸不干净", r"扫不干净"),
        ),
        CategoryRule(
            "power_on_failure",
            _patterns(r"无法开机", r"开不了机", r"按.{0,4}(?:没反应|无反应)", r"开机没反应"),
        ),
        CategoryRule(
            "error_code_water",
            _patterns(r"水路异常"),
            _patterns(r"^E[-_ ]?07$"),
        ),
        CategoryRule(
            "device_offline",
            _patterns(r"设备离线", r"离线.{0,6}连不上", r"固件升级失败", r"(?:app|App|APP).{0,6}显示离线"),
        ),
        CategoryRule(
            "drying_odor",
            _patterns(
                r"烘干.{0,6}(?:异味|臭味|有味)",
                r"(?:拖布|基站).{0,6}(?:异味|臭味|发臭|有臭味|发霉)",
            ),
        ),
    ),
    "RC-X800": (
        CategoryRule(
            "return_to_dock_failure",
            _patterns(r"无法回充", r"回充失败", r"找不到基站", r"回不了基站"),
        ),
        CategoryRule(
            "map_lost",
            _patterns(r"地图.{0,4}(?:丢失|不见了|没了|消失)", r"重新建图", r"地图.{0,4}(?:错乱|异常)"),
        ),
        CategoryRule(
            "missed_cleaning_area",
            _patterns(r"漏扫", r"重复(?:清扫|扫|打扫)", r"有的房间不(?:扫|去)"),
        ),
        CategoryRule(
            "collision_damage",
            _patterns(r"撞(?:家具|坏|上)", r"碰撞.{0,6}(?:厉害|很猛|太猛)", r"避障.{0,4}(?:失灵|不灵|失效)"),
        ),
        CategoryRule(
            "wifi_setup_failure",
            _patterns(r"配网.{0,8}(?:失败|不成功|异常)", r"(?:wifi|wi-fi|无线网).{0,8}(?:连不上|连接不上|失败)", r"无法联网"),
        ),
        CategoryRule(
            "cleaning_noise",
            _patterns(r"(?:清扫|运行|滚刷|边刷|轮子).{0,8}(?:异响|噪音|声音异常)", r"(?:异响|噪音)(?:很大|异常|变大)"),
        ),
        CategoryRule(
            "suction_drop",
            _patterns(r"吸力(?:变小|下降|变弱|不足)", r"吸不干净", r"扫不干净"),
        ),
        CategoryRule(
            "error_code_lidar",
            _patterns(r"激光雷达.{0,6}(?:报错|异常|受阻)"),
            _patterns(r"^E[-_ ]?03$"),
        ),
        CategoryRule(
            "no_go_zone_issue",
            _patterns(
                r"禁区.{0,8}(?:不生效|还是闯|无效|不起作用)",
                r"虚拟墙.{0,6}(?:不生效|失效|无效)",
                r"设了禁区还是",
            ),
        ),
        CategoryRule(
            "multi_floor_map",
            _patterns(
                r"(?:换|切换)楼层.{0,8}地图",
                r"楼层.{0,4}地图.{0,6}(?:不对|错误|识别错)",
                r"地图.{0,4}识别错",
            ),
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
