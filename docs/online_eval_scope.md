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
