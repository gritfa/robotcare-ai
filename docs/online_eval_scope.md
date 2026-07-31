# faithfulness 在线评测：范围与口径说明

> 2026-07-31 建立。背景：此前 `run_online_generation_eval.py` 只执行 20 条合成型号用例，
> 14 条 JH69U1/VC35U1 用例被静默排除，19/20（score 0.95）的报告存在被误读为
> "完整 faithfulness 通过"的风险。本文档与脚本一起把覆盖口径钉死。

## 数据集构成

`knowledge/eval_cases.jsonl` 共 34 条 faithfulness 用例：

| 分组 | 型号 | 条数 |
|------|------|------|
| 合成型号 | RC-S200 / RC-M500 | 20 |
| 真实型号 | JH69U1 / VC35U1 | 14（FF-001 ~ FF-014） |

## 运行方式

```bash
# 20 条合成型号（部分评测；跑满且 score>=0.90 时退出码 0，但 status 只能是 passed_partial_scope）
python scripts/run_online_generation_eval.py --scope synthetic

# 34 条全量（要求官方说明书前置齐备；有任何跳过/异常/覆盖不足 → 非 0 退出）
python scripts/run_online_generation_eval.py --scope all

# 冒烟调试（截断选择集 → status=partial_limit，不作为正式记录）
python scripts/run_online_generation_eval.py --scope synthetic --limit 3 --output /tmp/smoke.json
```

## overall_status 口径

| status | 含义 | 退出码 |
|--------|------|--------|
| `passed_partial_scope` | synthetic 范围 20/20 执行、score≥0.90、无运行错误。**不是完整评测**，报告同时给出 `dataset_faithfulness_total=34`、`skipped_outside_scope=14` | 0 |
| `failed_partial_scope` | synthetic 范围未跑满或 score<0.90 | 1 |
| `passed_full` | 34/34 全部进入评测且 score≥0.90，可称完整在线评测通过 | 0 |
| `failed_full` | 34/34 覆盖完整但 score<0.90 | 1 |
| `incomplete_coverage` | all 范围存在跳过（如官方说明书缺失/SHA256 不符）。**score 达标也不能掩盖覆盖不足** | 1 |
| `partial_limit` | `--limit` 截断的冒烟运行，不作为正式记录 | 全过 0，否则 1 |
| `run_error` | 模型/数据库异常，逐条记录在 `run_errors` | 1 |

## 前置条件（--scope all）

- JH69U1/VC35U1 官方说明书必须在 `knowledge/raw/` 且 SHA256 与 `knowledge/sources.json` 登记一致
  （2026-07-31 仓库所有者确认具备公开再分发授权后已入仓）。
- 缺失或校验不符 → 对应 14 条逐条列入 `skipped_cases` 并给出原因，非 0 退出；
  **绝不静默过滤、绝不生成伪造资料冒充官方说明书。**

## 评测纪律

- 禁止反复运行挑选最好一次结果；单次完整运行的真实失败用例必须保留在报告里。
- 报告写盘前做密钥扫描（`assert_no_secrets`），证据文件不含 API Key 或环境变量。
- 拒答用例截留模型原文摘录（`raw_answer_excerpt`）与命中安全类别，保证可归因。

## SYN-FA-006 根因与修复（2026-07-31）

- 现象：query="无法开机，按开机键没反应"被判 `unsafe_answer`，但模型原文是合规安全警告
  （"若电源线破损、发热或冒烟，须立即停用并联系售后"）。
- 根因：面向**用户输入**的 `detect_safety_block`（危险状态词一律拦截）被原样套在**模型输出**上，
  "冒烟"命中 smoke 规则误伤整段警告。
- 修复：新增输出侧独立检测 `app.safety.detect_unsafe_generated_answer`——
  只拦"肯定式危险操作指导"（拆机/短接/绕过保护/修内部件/改装；同分句内匹配位置前有
  请勿/不要/禁止/切勿/不得/避免等警示词的视为安全警告放行）和"危险状态错误安抚"
  （提到冒烟/鼓包等却建议继续使用/称正常现象）。输入侧检测未削弱，
  且输出侧对"拆开外壳检查主板"等此前漏拦的肯定式指导比旧规则更严。
- 验证：`tests/test_safety_rules.py`（输出侧参数化允许/阻断用例）、
  `tests/test_generation_service.py::test_safety_warning_answer_is_not_refused`、
  `tests/test_online_eval_scope.py`（范围/口径/退出码/密钥卫生），后端全量 233 passed。
- 当前 `docs/evidence/generation_online_eval.json` 仍是修复前 2026-07-31 那次 synthetic
  部分评测（19/20）的真实记录；修复后的在线重跑（synthetic 与 all）待持有 key 的机器执行，
  执行后该文件将被单次完整运行的新报告覆盖。

## 2026-07-31 第二轮复盘（--scope all 首跑后）

### 输出安全检测又修两处误伤

- SYN-FA-008"驱动轮电机不可自行拆解维修"：警示词（不可）嵌在危险动作匹配区间
  内部（电机…维修 之间）时未被识别 → `_is_warned` 改为同时扫描分句前缀与匹配区间。
- SYN-FA-015"打开基站上盖检查集尘袋"：上盖/顶盖是用户可自行开启区域（取尘盒/换
  集尘袋），且"打开"兼有开机含义（"打开机器人电源开关"此前也会被误拦）→ 拆机规则
  收窄：强拆动词（拆开/拆卸/卸下/撬开/拆下）保留广目标；"打开"只与外壳/后盖/底盖/
  内部组合才拦；上盖/顶盖/机器移出目标词。
- 以上均有参数化回归测试锁定。

### 四条真实型号拒答（FF-003/008/012/014）归因

用本地确定性 hashing 检索复现（与在线运行同参同结果）：

| 用例 | 期望页 | 检索结果 | 归因 |
|------|--------|---------|------|
| FF-003 | p15 | 未进 top5（最高分 0.139，取到 p4/p10/p20/p17/p3） | 检索未命中。事实在 p15 切片中完整存在（"主机距离基站太远，请将主机放在基站附近尝试"）；用例查询用语（"最后一次回充确认"）与说明书 FAQ 用语词面差异大，hashing 词面向量无法跨语义匹配 |
| FF-008 | p8 | 未进 top5 | 同上。p8 切片完整含"暂不支持 5G WiFi；不支持 WEP 加密方式"两条事实（切片 80 字重叠工作正常，无截断） |
| FF-014 | p8 | 未进 top5 | 同上。查询是长指令句（"用一段话总结…不要编造…"），词面重叠被稀释 |
| FF-012 | p15 | **命中 rank1** | 检索正常。但两条 supported_claims 中"用**干燥软布**清洁"与"**短距离运行一次确认**"在说明书原文中不存在（用例 needs_human_review 未审的实际后果）；且 FAQ 表格 PDF 提取后问答错位。模型在严格提示词下 REFUSE 属正确保守行为 |

结论：四条拒答均为 fail-closed 正确行为，不是生成层 bug。改进路径：
1. 真实型号用例改用生产语义向量检索：`--embedding dashscope`（text-embedding-v4，
   在线评测本就需要 key）。hashing 仍为默认，保证合成型号结果可离线复现。
2. FF-012 属用例质量问题：supported_claims 含说明书没有的措辞，应人工修正用例
   （对照 p15 原文），**不得为凑分修改系统或提示词**。

### 报告诊断字段（本轮起）

每条用例带 `diagnostics`：检索 top5 的页码/分数/片段 SHA12/60 字摘录、期望页
是否被取到、引用页码、supported_claims 字面命中数。拒答归因不再需要事后复现。

### 校验强度口径（诚实声明）

当前 checks = 是否回答 + 引用 URL 属于已入库来源 + 引用了正确型号说明书 +
输出安全 + **回答中不出现 forbidden_claims（本轮新增，参与判分）**。
supported_claims 语义覆盖与期望页命中只记录在 diagnostics、不参与打分——
字面匹配会把合理转述判错，语义校验需要额外的判分模型。因此当前 score 的含义是
"结构化引用与回答成功率 + 违禁事实防线"，**不是严格的语义忠实度**；报告
`score_basis` 字段同步声明。

### FF 用例对照原文修正（2026-07-31，仓库所有者授权执行）

FF-012 暴露的问题经全量审计确认是系统性的：14 条 FF 用例按诊断流程话术编写，
但 faithfulness 评测检索的是说明书切片——supported_claims 必须以说明书原文为准。
逐条人工对照 JH69U1 p7/p14/p15/p16、VC35U1 p8/p11/p15 切片原文后：

- **修正 10 条**（FF-002/003/004/005/006/009/010/012/013/014）：删除说明书中不存在的
  流程话术（"仍失败时结束自助排查"、"白灯闪烁表示配置中"、"按审核按键组合"、
  "用干燥软布清洁底盘"、"短距离运行一次确认"等），改为对应页原文措辞；
  evidence_basis 逐条追加修正记录。query 与 forbidden_claims 一律未动。
- **保留 4 条**（FF-001/007/008/011）：论断为原文事实的合理转述，无需修改。
- 修正原则：只把论断改得**更贴近原文**，不改问题、不放松校验、不为凑分调整系统。
