# 海尔扫地机器人说明书证据记录

更新日期：2026-07-20

## 1. 状态边界

- 【已验证】：两份 PDF 可从海尔官方直链下载；官方下载内容与本地忽略文件的字节数和 SHA256 一致；文件可读取、可提取文本，且指定页面已渲染抽检。
- 【已验证】：两份说明书已完成分页切片、`text-embedding-v4` 256 维向量生成和 SQLite 持久化，并核对文档、分片与非空向量数量。
- 【已验证：实现与静态/单元】：PostgreSQL/pgvector 双方言类型、固定 256 维、数据库 Top-K/型号过滤 SQL、HNSW 迁移和知识替换失败回滚已经实现并通过单元、静态编译与 SQLite 回归。
- 【待验证】：真实 PostgreSQL/pgvector 迁移、索引、Top-K 和健康检查尚未运行；当前完成 5 条真实查询的 SQLite 检索冒烟，以及 113 条评测数据上的三项离线审计。113 条全部为 `needs_human_review`；五项真实检索/分类/生成指标、阈值校准和生成式回答仍为【计划】。

## 2. 官方来源与文件指纹

| 型号 | 官方产品页 | 官方 PDF 直链 | 本地忽略文件 | 字节数 | SHA256 | 页数 | 可提取字符数 | 下载 | 文本提取 | 向量入库 |
| --- | --- | --- | --- | ---: | --- | ---: | ---: | --- | --- | --- |
| JH69U1 | <https://www.haier.com/xjd/sdjqr/20241029_252007.shtml> | <https://download.haier.com/cn/xjd/sdjqr/W020241029385536323887.pdf?appdesc=JH69U1说明书> | `knowledge/raw/JH69U1_manual.pdf` | 27,364,132 | `672c614516feb428f3dd6090d1f7835c4ce0eacbfca3b12c6b59e954e5c49171` | 20 | 9,539 | 【已验证】 | 【已验证】 | 【已验证：31向量】 |
| VC35U1 | <https://www.haier.com/xjd/sdjqr/20200902_146632.shtml> | <https://download.haier.com/cn/xjd/sdjqr/W020200902576527552250.pdf?appdesc=VC35U1说明书> | `knowledge/raw/VC35U1_manual.pdf` | 12,920,954 | `67604668d578e166935a0e6a4e0cb9443d5b028bceb15f55dcfb31fbd2b71614` | 20 | 4,667 | 【已验证】 | 【已验证】 | 【已验证：22向量】 |

官方产品页明确列出对应型号说明书及 PDF 格式。2026-07-20 对两个官方 PDF 直链执行流式下载校验，响应类型均为 `application/pdf`，下载字节数和 SHA256 均与本地文件一致。

## 3. 可复现验证命令

以下命令在 `robotcare-ai` 根目录执行。字符数定义为 `pypdf.PdfReader` 对每页执行 `extract_text()` 后所得字符串长度之和；空页按 0 计算。因此它是当前提取器的可读取文本量，不等同于排版后的字数或 OCR 字数。

```powershell
Get-FileHash -Algorithm SHA256 knowledge/raw/JH69U1_manual.pdf
Get-FileHash -Algorithm SHA256 knowledge/raw/VC35U1_manual.pdf
```

```powershell
$env:PYTHONUTF8='1'
@'
from pathlib import Path
from pypdf import PdfReader

for name in ("JH69U1_manual.pdf", "VC35U1_manual.pdf"):
    path = Path("knowledge/raw") / name
    reader = PdfReader(str(path))
    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    print(name, "pages=", len(reader.pages), "text_chars=", text_chars)
'@ | python -
```

官方直链与本地文件的一致性验证采用流式下载，并比较下载字节数和 SHA256。验证结果为：

```text
JH69U1 status=200 content-type=application/pdf bytes=27364132 sha256=672c614516feb428f3dd6090d1f7835c4ce0eacbfca3b12c6b59e954e5c49171
VC35U1 status=200 content-type=application/pdf bytes=12920954 sha256=67604668d578e166935a0e6a4e0cb9443d5b028bceb15f55dcfb31fbd2b71614
```

指定页面使用 Poppler 渲染到系统临时目录，渲染文件不提交仓库：

```powershell
$out = Join-Path $env:TEMP 'robotcare_manual_evidence'
New-Item -ItemType Directory -Force -Path $out | Out-Null
pdftoppm -f 15 -l 16 -png -r 140 knowledge/raw/JH69U1_manual.pdf (Join-Path $out 'jh69u1')
pdftoppm -f 15 -l 15 -png -r 140 knowledge/raw/VC35U1_manual.pdf (Join-Path $out 'vc35u1')
```

## 4. 渲染抽检

抽检只记录页面主题和可见性，不复制大段说明书正文。

| 文件页码（从 1 开始） | 【已验证】可见内容概述 | 目视结果 |
| --- | --- | --- |
| JH69U1 PDF 第 15 页 | “常见问题”表格，覆盖开机、启动、回充、充电、离线、APP 连接、刷具异物、清洁能力及基站清洗等问题。 | 中文、表格线、页码和双栏信息均清晰，无裁切或黑块。 |
| JH69U1 PDF 第 16 页 | “故障排除”表格，覆盖激光测距、防跌落、碰撞、尘盒/滤网、拖布、清洗槽、水箱、集尘袋、定位和建图等提示。 | 双栏排版完整，中文可辨，未发现重叠或缺页。 |
| VC35U1 PDF 第 15 页 | “常见问题”表格，覆盖开机、充电、回充、配网/离线、刷具异响、清洁能力及定时清扫等问题。 | 表格完整、正文清晰、页码可见，无裁切或渲染异常。 |

这些页面能证明 PDF 包含与项目故障范围相关的可读资料，但诊断步骤是否采用、如何拆分以及安全等级仍需逐条人工审核；不能仅凭文本提取结果自动发布给用户。

## 5. 版权与仓库边界

- 说明书版权归海尔及相关权利人所有，仅作为产品研发中的官方事实来源和内部检索材料。
- `knowledge/raw/*.pdf` 为本地忽略文件，不提交到公开 Git 仓库，不重新分发原始 PDF，也不在文档中复制长篇正文。
- 公开仓库只保存官方来源 URL、文件指纹、页数、提取统计、处理状态、下载/解析脚本和少量必要的测试摘要。
- 对外展示时必须标注本项目是独立第三方工具，不得暗示海尔官方授权、认证或售后背书。
- 用户可见答案必须保留型号与页码来源；涉及拆机、内部维修、电池处理或其他高风险操作时停止自助指导并建议联系官方售后。

## 6. 向量入库与检索证据

2026-07-20 使用以下配置完成真实入库：

- Embedding：`text-embedding-v4`
- 维度：256
- 文档文本类型：`document`
- 查询文本类型：`query`
- 批量大小：最多 10
- 切片：每页独立，500 字，80 字重叠
- JH69U1：1 文档 / 31 分片 / 31 向量
- VC35U1：1 文档 / 22 分片 / 22 向量

真实检索冒烟评测位于 `docs/evidence/rag_smoke_20260720.json`：5 条用例全部通过，包括回充、清水箱、配网、异响和高阈值无关问题拒绝。该结果只能证明当前冒烟样例，不代表完整 RAG 质量已经达标。

分项评测数据位于 `knowledge/eval_cases.jsonl`，当前 113 条；离线审计报告位于 `docs/evidence/rag_eval_offline_audit.json` 和 `.md`：

- `safety_block`：15/15，通过后端确定性规则实际执行。
- `source_page`：14/14，仅核对期望页是否存在于当前已发布流程，不是在线回答引用命中率。
- `step_selection`：14/14，仅核对流程顺序和单步期望，不是 Agent 运行命中率。
- `model_isolation`、`retrieval_recall`、`refusal`、`classification`、`faithfulness`：均为 `not_run`。
- 113 条用例全部为 `needs_human_review`，没有逐条人工确认记录，不能称为人工审核评测集。

## 7. 下一阶段验收条件

以下向量入库条件已经满足；剩余完整 RAG 验收仍需要：

1. 对 113 条数据逐条人工复核，记录审核人、审核时间、依据和修改原因；未经复核保持 `needs_human_review`。
2. 运行 `model_isolation`、`retrieval_recall`、`refusal`、`classification` 和 `faithfulness` 的真实链路评测，并按维度单独报告，不用总平均分掩盖安全或型号隔离失败。
3. 校准默认 `min_score=0.25`，避免仅依靠人为提高阈值实现无关问题拒答。
4. 在专用 `robotcare_test` PostgreSQL 数据库运行已编写的集成测试与 smoke 脚本，保存 `vector` 扩展、`vector(256)` 字段、HNSW、Top-K、型号隔离和 Alembic head 的真实输出；当前只有实现与静态/单元证据。
5. 对全部用户可见片段执行人工安全审核和页码抽检。
6. 如加入生成式回答，单独验证忠实度、引用正确率和提示注入防护。
