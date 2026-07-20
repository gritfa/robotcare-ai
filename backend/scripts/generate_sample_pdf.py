from datetime import UTC, datetime
from pathlib import Path
import sys
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.pdf_report import render_report_pdf


def main() -> None:
    output = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "sample_service_report.pdf"
    report = SimpleNamespace(
        report_number="RC-DEMO-20260720",
        created_at=datetime.now(UTC),
        content="""RobotCare AI 第三方售后诊断报告
设备型号：VC35U1
用户问题：手机多次搜索不到设备，配网失败。
错误码：WIFI-01
已执行的安全排查步骤：
1. 确认家庭网络为 2.4GHz - 未解决
操作：确认手机连接的是 2.4GHz Wi-Fi，不要在报告中填写 Wi-Fi 密码。
来源：海尔 VC35U1 官方说明书，第 15 页
2. 重新进入配网模式 - 未解决
操作：按说明书指示重新进入配网模式并等待指示灯闪烁。
来源：海尔 VC35U1 官方说明书，第 15 页
附件文件：
- wifi-error-screen.png
最终结果：自助排查未解决，建议联系海尔官方售后。
免责声明：本报告由独立第三方工具生成，不代表海尔官方诊断结论。""",
    )
    render_report_pdf(report, output)
    print(output)


if __name__ == "__main__":
    main()
