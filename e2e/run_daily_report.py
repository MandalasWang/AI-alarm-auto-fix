"""手动触发一次告警日报（等价 xxl-job 调度角色）：真实查库统计 + LLM 洞察 + 推送测试群。"""
import sys

sys.path.insert(0, ".")

from app.container import load_and_build

container = load_and_build()
try:
    content = container.daily_report.send_daily_report()
    print("=== 日报全文 ===")
    print(content)
finally:
    container.shutdown()
