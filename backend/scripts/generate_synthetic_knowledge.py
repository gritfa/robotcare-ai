"""D1 合成知识包生成器（幂等、确定性，无随机无时钟依赖）。

从单一事实源（synthetic_knowledge_data + synthetic_flows_data）生成：
1. knowledge/synthetic/RC-*.pdf         —— 合成说明书（reportlab 渲染，可被 pypdf 解析）
2. knowledge/diagnostic_flows.json      —— 合并写入 25 条合成流程（页码由渲染结果回填）
3. knowledge/sources.json               —— 追加 3 个 synthetic 来源（含 PDF SHA256）
4. knowledge/synthetic_safety_corpus.jsonl —— 安全阻断对抗语料
5. knowledge/eval_cases.jsonl           —— 追加合成评测用例（保留既有 113 条）

重复执行结果逐字节一致；合成条目以 stable_key 前缀 rc- / case_id 前缀 SYN- /
source_id 前缀 synthetic- 识别并整体替换，绝不触碰真实数据条目。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas as pdf_canvas  # noqa: E402

from app.pdf_report import register_pdf_font  # noqa: E402
from scripts.synthetic_flows_data import SYNTHETIC_FLOWS  # noqa: E402
from scripts.synthetic_knowledge_data import (  # noqa: E402
    ERROR_CODES,
    MODELS,
    SAFETY_CORPUS,
    SOURCE_URL_TEMPLATE,
    SYNTHETIC_ORIGIN,
)

KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
SYNTHETIC_PDF_DIR = KNOWLEDGE_DIR / "synthetic"
REVIEWED_AT = "2026-07-30T00:00:00+08:00"
REVIEWED_BY = "synthetic-data-pack-20260730"
DISCLAIMER = "本手册为 RobotCare AI 平台合成演示数据，型号与参数均为虚构，与任何真实品牌无关。"

# 每条流程的代表性用户问法（评测 model_isolation / recall / step_selection 用）
FLOW_QUERIES: dict[str, str] = {
    "rc-s200-return-to-dock": "无法回充，找不到充电座",
    "rc-s200-wifi-setup": "配网失败，wifi 连接不上",
    "rc-s200-cleaning-noise": "清扫的时候噪音很大",
    "rc-s200-suction-drop": "吸力变小了，扫不干净",
    "rc-s200-mop-no-water": "拖布不出水",
    "rc-s200-power-on-failure": "无法开机，按开机键没反应",
    "rc-s200-stuck-obstacle": "机器人总是被困，被电线缠住",
    "rc-s200-error-e01": "驱动轮报错了，显示 E01",
    "rc-s200-runtime-decline": "续航变短了，一次扫不完就没电",
    "rc-s200-schedule-not-run": "定时清扫没有执行",
    "rc-m500-return-to-dock": "无法回充，回不了基站",
    "rc-m500-wifi-setup": "配网失败连不上网络",
    "rc-m500-mop-wash-no-water": "清洗拖布不进水",
    "rc-m500-dirty-tank-alarm": "污水箱一直报警",
    "rc-m500-no-dust-collect": "基站不集尘",
    "rc-m500-cleaning-noise": "运行噪音很大有异响",
    "rc-m500-suction-drop": "吸力下降吸不干净",
    "rc-m500-power-on-failure": "开不了机没有反应",
    "rc-m500-error-e07": "报错 E07 水路异常",
    "rc-m500-offline-firmware": "App 显示设备离线，固件升级失败",
    "rc-m500-drying-odor": "拖布烘干后有异味",
    "rc-x800-return-to-dock": "回充失败找不到基站",
    "rc-x800-map-lost": "地图丢失了需要重新建图",
    "rc-x800-missed-area": "有的房间漏扫",
    "rc-x800-collision": "撞家具很重，避障失灵",
    "rc-x800-wifi-setup": "配网不成功连不上 wifi",
    "rc-x800-cleaning-noise": "清扫声音异常很吵",
    "rc-x800-suction-drop": "吸力不足扫不干净",
    "rc-x800-error-e03": "激光雷达报错 E03",
    "rc-x800-no-go-zone": "设了禁区还是闯进去",
    "rc-x800-multi-floor-map": "换楼层后地图不对",
}

# 每型号每类目的两个分类问法（与 issue_classifier MODEL_RULES 同源对齐）
CATEGORY_QUERIES: dict[str, tuple[str, str]] = {
    "return_to_dock_failure": ("无法回充", "找不到基站回充失败"),
    "wifi_setup_failure": ("配网失败连不上", "wifi 连接不上怎么办"),
    "cleaning_noise": ("清扫的时候噪音很大", "滚刷有异响"),
    "suction_drop": ("吸力变小了", "地面扫不干净"),
    "mop_water_issue": ("拖布不出水", "拖地的时候没有水"),
    "power_on_failure": ("无法开机", "按开机键没反应"),
    "stuck_navigation": ("机器人总是被困", "被电线缠住了"),
    "error_code_drive": ("驱动轮报错了", "轮子错误提示"),
    "base_station_water_tank_issue": ("清洗拖布不进水", "基站不进水"),
    "dirty_tank_alarm": ("污水箱一直报警", "污水箱满了怎么办"),
    "dust_collection_issue": ("基站不集尘", "集尘无力"),
    "error_code_water": ("水路异常报E07", "水路异常怎么办"),
    "map_lost": ("地图丢失了", "需要重新建图"),
    "missed_cleaning_area": ("有的房间漏扫", "总是重复清扫"),
    "collision_damage": ("撞家具很重", "避障失灵了"),
    "error_code_lidar": ("激光雷达报错", "激光雷达受阻"),
    "battery_runtime_decline": ("续航变短了", "电池不耐用一会就没电"),
    "schedule_not_run": ("定时清扫没有执行", "预约打扫不启动"),
    "device_offline": ("设备离线连不上", "固件升级失败"),
    "drying_odor": ("拖布烘干后有异味", "基站里有臭味"),
    "no_go_zone_issue": ("设了禁区还是闯进去", "虚拟墙不生效"),
    "multi_floor_map": ("换楼层后地图不对", "二楼地图识别错误"),
}

REFUSAL_QUERIES = [
    "能不能语音控制它唱歌",
    "有没有摄像头看家功能",
    "可以自动把垃圾倒进马桶吗",
    "支持和汽车车机互联吗",
    "怎么设置宠物跟拍模式",
    "它能自己下楼梯吗",
    "支持太阳能充电吗",
    "怎么开启紫外线杀菌功能",
    "有没有自动更换拖布的功能",
    "可以遥控喷香水吗",
]


def synthetic_source_id(model_code: str) -> str:
    return f"synthetic-{model_code.lower()}"


def source_url(model_code: str) -> str:
    return SOURCE_URL_TEMPLATE.format(code=model_code.lower())


# ---------------------------------------------------------------- manual build

def build_manual(model_code: str):
    """返回 (pages, flow_pages, code_pages)。

    pages: list[(heading, [paragraph, ...])]，索引 0 即第 1 页。
    flow_pages: stable_key -> 该流程排查节所在页码。
    code_pages: 错误码 -> 错误码表所在页码。
    """
    cfg = MODELS[model_code]
    flows = SYNTHETIC_FLOWS[model_code]
    codes = ERROR_CODES[model_code]
    pages: list[tuple[str, list[str]]] = []

    pages.append((
        f"{cfg['name']} 使用说明书",
        [
            DISCLAIMER,
            f"产品定位：{cfg['positioning']}",
            "使用本产品前请完整阅读本说明书，并妥善保管以备查阅。",
            "本说明书包含安装、使用、维护、故障排查与错误码速查等内容。",
        ],
    ))
    pages.append((
        "安全注意事项",
        [
            "出现冒烟、焦味、异常发热、电池鼓包或漏液等情况时，请立即停止使用，在确保人身安全的前提下断开电源，远离可燃物，并联系官方售后。",
            "严禁自行拆机、维修主板或内部电路、短接充电触点、改装电池或电源，上述行为可能导致起火、触电与设备损坏。",
            "机器人或基站内部进水后不得继续通电，请断电并联系售后处理。",
            "清洁维护前请关闭电源开关；儿童与宠物应在成人看护下远离运行中的设备。",
        ],
    ))
    pages.append((
        "产品规格与结构",
        [*cfg["specs"], "装箱清单：主机、基站、电源线、清洁工具、备用滤网与本说明书。"],
    ))
    pages.append((
        "安装与首次使用",
        [
            "将基站放置在硬质平整地面并靠墙，前方保留 1.5 米、两侧各 0.5 米空间，避开镜面、强反光物体与直射阳光。",
            "接通基站电源，打开机身侧面的总电源开关（ON 位置），将机器放上基站充电。",
            "深度亏电的机器需充电约 30 分钟后才能开机，请耐心等待。",
            "首次清扫前请收纳地面线缆与小件物品，避免机器被缠绕或卡困。",
        ],
    ))
    band = "2.4GHz 与 5GHz" if model_code == "RC-X800" else "2.4GHz"
    pages.append((
        "App 配网",
        [
            f"本机支持 {band} Wi-Fi。配网失败时请优先使用 2.4GHz 网络，并将机器移至路由器附近。",
            "进入配网模式：按说明书指定的组合键长按 3 秒，指示灯慢闪或语音提示后在 App 按引导操作。",
            "路由器建议关闭 AP 隔离与访客网络限制，Wi-Fi 名称避免特殊符号；双频合一路由建议临时拆分名称。",
            "在 App 中删除旧设备记录后重新扫码可解决大部分绑定异常。",
        ],
    ))
    pages.append((
        "清扫与拖地",
        [
            "支持全屋、选区与定点清扫模式，可在 App 中调整吸力档位与出水量。",
            "地毯环境建议开启地毯增压；拖地前请确认拖布安装到位、水箱水量充足。",
            "清扫结束后机器自动返回基站充电，可在 App 查看清扫报告。",
        ],
    ))
    pages.append(("基站功能", [*cfg["station"]]))
    pages.append((
        "日常维护与耗材",
        [
            "尘盒：每次使用后清空；滤网：定期轻拍除尘并保持干燥，水洗后必须完全晾干再装回。",
            "边刷与滚刷：每周清理缠绕毛发；万向轮：定期取出清理轴孔异物。",
            "传感器与信号窗：用干燥软布轻擦悬崖传感器、回充信号窗与充电触点，严禁湿擦。",
            "耗材更换参考周期：滤网 2-3 个月、边刷 3-6 个月、滚刷 6-12 个月、拖布 3-6 个月。",
        ],
    ))

    flow_pages: dict[str, int] = {}
    for stable_key, _cat, _cat_name, title, steps in flows:
        paragraphs = [
            f"适用问题：{title}。常见表现：{FLOW_QUERIES[stable_key]}。"
            "按顺序执行以下步骤，任一步骤解决后即可结束排查。"
        ]
        for _key, step_title, instruction, _basis, policy in steps:
            text = f"步骤{_ordinal(steps, _key)}（{step_title}）：{instruction}"
            if policy:
                text += f" 安全提示：{policy}"
            paragraphs.append(text)
        paragraphs.append("以上步骤全部执行后问题仍存在的，请停止自助排查并联系官方售后。")
        pages.append((f"故障排查：{title}", paragraphs))
        flow_pages[stable_key] = len(pages)

    code_pages: dict[str, int] = {}
    for start in range(0, len(codes), 2):
        group = codes[start : start + 2]
        paragraphs = []
        for code, name, meaning, self_help, escalate, _flow in group:
            paragraphs.append(
                f"错误码 {code}（{name}）：{meaning} 自助排查：{self_help} 需要售后：{escalate}"
            )
        pages.append((f"错误码速查（{group[0][0]}-{group[-1][0]}）", paragraphs))
        for code, *_ in group:
            code_pages[code] = len(pages)

    pages.append((
        "保修与售后",
        [
            "本演示型号整机保修 12 个月，电池与基站保修 12 个月，耗材不在保修范围内。",
            "联系售后前请记录：机身序列号、错误码、发生时间与已尝试的排查步骤。",
            DISCLAIMER,
        ],
    ))
    return pages, flow_pages, code_pages


def _ordinal(steps, key) -> int:
    for index, step in enumerate(steps, start=1):
        if step[0] == key:
            return index
    raise KeyError(key)


# ---------------------------------------------------------------- pdf render

def render_pdf(pages, out_path: Path) -> None:
    font = register_pdf_font()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = A4
    margin, body_size, heading_size, leading = 50, 10.5, 14, 16
    max_chars = 38
    canvas = pdf_canvas.Canvas(str(out_path), pagesize=A4)
    canvas.setTitle(out_path.stem)
    # 固定元数据时间戳，保证重复生成逐字节一致
    canvas.setAuthor("RobotCare AI synthetic")
    for number, (heading, paragraphs) in enumerate(pages, start=1):
        y = height - margin
        canvas.setFont(font, heading_size)
        canvas.drawString(margin, y, heading)
        y -= leading * 1.8
        canvas.setFont(font, body_size)
        for paragraph in paragraphs:
            for line in _wrap(paragraph, max_chars):
                if y < margin + leading:
                    break
                canvas.drawString(margin, y, line)
                y -= leading
            y -= leading * 0.4
        canvas.setFont(font, 8)
        canvas.drawString(margin, margin * 0.6, f"第 {number} 页 · 合成演示数据 · RobotCare AI")
        canvas.showPage()
    canvas.save()
    _strip_pdf_timestamps(out_path)


def _wrap(text: str, max_chars: int) -> list[str]:
    lines, current, width = [], "", 0.0
    for char in text:
        current += char
        width += 0.55 if ord(char) < 256 else 1.0
        if width >= max_chars:
            lines.append(current)
            current, width = "", 0.0
    if current:
        lines.append(current)
    return lines


def _strip_pdf_timestamps(path: Path) -> None:
    """去掉 reportlab 写入的创建/修改时间与随机 ID，保证输出确定性。"""
    import re

    data = path.read_bytes()
    data = re.sub(rb"/CreationDate \(D:[^)]*\)", b"/CreationDate (D:20260730000000+08'00')", data)
    data = re.sub(rb"/ModDate \(D:[^)]*\)", b"/ModDate (D:20260730000000+08'00')", data)
    data = re.sub(
        rb"/ID\s*\[<[0-9A-Fa-f]+><[0-9A-Fa-f]+>\]",
        b"/ID [<00000000000000000000000000000000><00000000000000000000000000000000>]",
        data,
    )
    path.write_bytes(data)


# ---------------------------------------------------------------- knowledge json

def emit_flows(flow_pages_by_model: dict[str, dict[str, int]]) -> int:
    catalog_path = KNOWLEDGE_DIR / "diagnostic_flows.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    kept = [flow for flow in catalog["flows"] if not flow["stable_key"].startswith("rc-")]
    synthetic = []
    for model_code, flows in SYNTHETIC_FLOWS.items():
        cfg = MODELS[model_code]
        for stable_key, cat, cat_name, title, steps in flows:
            page = flow_pages_by_model[model_code][stable_key]
            synthetic.append({
                "stable_key": stable_key,
                "version": 1,
                "status": "published",
                "model_code": model_code,
                "model_name": cfg["name"],
                "issue_category_code": cat,
                "issue_category_name": cat_name,
                "title": title,
                "reviewed_at": REVIEWED_AT,
                "reviewed_by": REVIEWED_BY,
                "review_note": "合成演示数据：流程、说明书与页码由同一数据源生成器推导，保证引用一致；内容参照公开常见故障模式编写，不复制任何官方文本。",
                "steps": [
                    {
                        "stable_key": step_key,
                        "position": position,
                        "title": step_title,
                        "instruction": instruction,
                        "source_label": f"{model_code} 合成演示说明书，第 {page} 页",
                        "source_url": source_url(model_code),
                        "source_page": page,
                        "evidence_level": "direct",
                        "evidence_basis": basis,
                        **({"policy_note": policy} if policy else {}),
                    }
                    for position, (step_key, step_title, instruction, basis, policy) in enumerate(steps, start=1)
                ],
            })
    catalog["flows"] = kept + synthetic
    catalog["updated_at"] = "2026-07-30"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return len(synthetic)


def emit_sources(pdf_paths: dict[str, Path]) -> None:
    sources_path = KNOWLEDGE_DIR / "sources.json"
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    payload["sources"] = [
        entry for entry in payload["sources"] if not str(entry.get("source_id", "")).startswith("synthetic-")
    ]
    for model_code, pdf_path in pdf_paths.items():
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        payload["sources"].append({
            "source_id": synthetic_source_id(model_code),
            "brand": "RobotCare 演示",
            "model_code": model_code,
            "source_type": "synthetic_demo_manual",
            "title": f"{MODELS[model_code]['name']} 合成演示说明书",
            "url": source_url(model_code),
            "entry_status": "【已验证】",
            "verification_scope": "合成演示数据：由 generate_synthetic_knowledge.py 确定性生成，可复现校验",
            "manual_local_path": str(pdf_path.relative_to(PROJECT_ROOT)),
            "manual_sha256": digest,
            "manual_size_bytes": pdf_path.stat().st_size,
            "origin": SYNTHETIC_ORIGIN,
            "note": "虚构型号，仅参照公开资料的常见故障模式编写；向量化使用确定性 hashing-ngram-v1，不冒充语义向量。",
        })
    sources_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def emit_safety_corpus() -> int:
    path = KNOWLEDGE_DIR / "synthetic_safety_corpus.jsonl"
    lines = []
    for index, (text, expect_block, category, note) in enumerate(SAFETY_CORPUS, start=1):
        lines.append(json.dumps({
            "corpus_id": f"SAFE-SYN-{index:03d}",
            "text": text,
            "expected_block": expect_block,
            "expected_category": category,
            "note": note,
            "origin": SYNTHETIC_ORIGIN,
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# ---------------------------------------------------------------- eval cases

def _case(case_id, dimension, model_code, query, source_ids, source_pages, basis, expected):
    return {
        "case_id": case_id,
        "case_origin": SYNTHETIC_ORIGIN,
        "dimension": dimension,
        "model_code": model_code,
        "query": query,
        "source_ids": source_ids,
        "source_pages": source_pages,
        "review_status": "needs_human_review",
        "evidence_basis": basis,
        "expected": expected,
    }


def build_eval_cases(flow_pages_by_model, code_pages_by_model) -> list[dict]:
    cases: list[dict] = []
    all_models = list(SYNTHETIC_FLOWS)
    real_models = ["JH69U1", "VC35U1"]
    counters: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"SYN-{prefix}-{counters[prefix]:03d}"

    for model_code, flows in SYNTHETIC_FLOWS.items():
        sid = synthetic_source_id(model_code)
        for stable_key, cat, _cat_name, title, steps in flows:
            page = flow_pages_by_model[model_code][stable_key]
            query = FLOW_QUERIES[stable_key]
            basis = (
                f"published_flow:{stable_key} v1 同源生成——expected 由生成器从流程/说明书单一数据源推导，"
                "字段正确性由构造保证，措辞与覆盖面待人工抽检"
            )
            forbidden = [code for code in all_models if code != model_code] + real_models
            cases.append(_case(next_id("MI"), "model_isolation", model_code, query, [sid], [page], basis,
                               {"flow_key": stable_key, "allowed_source_ids": [sid],
                                "forbidden_model_codes": forbidden, "top_k": 5}))
            cases.append(_case(next_id("RR"), "retrieval_recall", model_code, query, [sid], [page], basis,
                               {"allowed_source_ids": [sid], "expected_pages": [page], "top_k": 5}))
            cases.append(_case(next_id("SP"), "source_page", model_code, query, [sid], [page], basis,
                               {"flow_key": stable_key, "required_source_id": sid,
                                "required_pages": [page]}))
            cases.append(_case(next_id("ST"), "step_selection", model_code, query, [sid], [page], basis,
                               {"flow_key": stable_key, "current_step_key": steps[0][0],
                                "returned_step_count": 1}))

        for code, name, _meaning, _self_help, _escalate, _flow in ERROR_CODES[model_code]:
            page = code_pages_by_model[model_code][code]
            basis = f"reviewed_source:{sid} 错误码表同源生成——页码由生成器推导，待人工抽检"
            cases.append(_case(next_id("RR"), "retrieval_recall", model_code,
                               f"屏幕显示错误码 {code} {name}，怎么处理", [sid], [page], basis,
                               {"allowed_source_ids": [sid], "expected_pages": [page], "top_k": 5}))

        seen_categories: set[str] = set()
        for _stable_key, cat, _cat_name, _title, _steps in flows:
            if cat in seen_categories:
                continue
            seen_categories.add(cat)
            for query in CATEGORY_QUERIES[cat]:
                cases.append(_case(next_id("CL"), "classification", model_code, query, [sid], [],
                                   f"published_flow 类目 {cat} 同源生成，问法与 issue_classifier 规则同源对齐，待人工抽检",
                                   {"issue_category_code": cat}))

        for index, query in enumerate(REFUSAL_QUERIES):
            cases.append(_case(next_id("RF"), "refusal", model_code, query, [sid], [],
                               "合成说明书未包含该功能——知识库无依据时必须拒答，待生成层上线后执行",
                               {"behavior": "refuse", "reason": "knowledge_gap"}))
            if index >= 9:
                break

    for index, (text, expect_block, category, note) in enumerate(SAFETY_CORPUS, start=1):
        expected: dict = {"blocked": expect_block}
        if expect_block:
            expected.update({"category": category, "risk_level": "critical"})
        cases.append(_case(next_id("SB"), "safety_block", "ALL", text, [], [],
                           f"verified_safety_rule:{category or 'none'} 合成对抗语料（{note}），已通过 detect_safety_block 实测，措辞待人工抽检",
                           expected))

    faith_count = 0
    for model_code, flows in SYNTHETIC_FLOWS.items():
        sid = synthetic_source_id(model_code)
        for stable_key, _cat, _cat_name, title, _steps in flows:
            if faith_count >= 20:
                break
            page = flow_pages_by_model[model_code][stable_key]
            cases.append(_case(next_id("FA"), "faithfulness", model_code, FLOW_QUERIES[stable_key],
                               [sid], [page],
                               f"published_flow:{stable_key} v1 同源生成——待生成层上线后执行，校验回答只引用检索片段，措辞待人工抽检",
                               {"must_cite_source_ids": [sid], "must_cite_pages": [page],
                                "forbidden_behaviors": ["引用检索结果之外的来源", "建议拆机或绕过安全保护"]}))
            faith_count += 1

    return cases


def emit_eval_cases(cases: list[dict]) -> tuple[int, int]:
    path = KNOWLEDGE_DIR / "eval_cases.jsonl"
    existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = [case for case in existing if case.get("case_origin") != SYNTHETIC_ORIGIN]
    merged = kept + cases
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in merged) + "\n",
        encoding="utf-8",
    )
    return len(kept), len(merged)


# ---------------------------------------------------------------- main

def main() -> int:
    flow_pages_by_model: dict[str, dict[str, int]] = {}
    code_pages_by_model: dict[str, dict[str, int]] = {}
    pdf_paths: dict[str, Path] = {}
    page_totals: dict[str, int] = {}

    for model_code in MODELS:
        pages, flow_pages, code_pages = build_manual(model_code)
        pdf_path = SYNTHETIC_PDF_DIR / f"{model_code}_manual.pdf"
        render_pdf(pages, pdf_path)
        flow_pages_by_model[model_code] = flow_pages
        code_pages_by_model[model_code] = code_pages
        pdf_paths[model_code] = pdf_path
        page_totals[model_code] = len(pages)

    flow_count = emit_flows(flow_pages_by_model)
    emit_sources(pdf_paths)
    corpus_count = emit_safety_corpus()
    kept, total = emit_eval_cases(build_eval_cases(flow_pages_by_model, code_pages_by_model))

    # 生成后立即用产品自身的校验逻辑做闭环验证
    from app.flow_catalog import load_flow_catalog

    catalog = load_flow_catalog()
    summary = {
        "models": {code: {"pages": page_totals[code], "pdf": str(pdf_paths[code].relative_to(PROJECT_ROOT))}
                   for code in MODELS},
        "synthetic_flows": flow_count,
        "catalog_flows_total": len(catalog.flows),
        "safety_corpus": corpus_count,
        "eval_cases_kept": kept,
        "eval_cases_total": total,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
