"""手动触发一次告警看板（等价外部按需调用 GET /inner/feishu/alarm/dashboard/report 的推送部分）：
真实查库统计 + LLM 月告警分析 + 日/周趋势折线（VChart 变量）+ 推送测试群。

覆盖看板配置（模板 AAqT1Xd4tu1kR + detail_url）以验证图表卡片样式；直调 service 不经过每日一次锁。
"""
import sys

sys.path.insert(0, ".")

from app.container import load_and_build

container = load_and_build()
try:
    # 看板专属配置（生产走 config.yaml / env：analyse.dashboard.card-template-id、detail-url）
    container.config.analyse.dashboard.card_template_id = "AAqT1Xd4tu1kR"
    container.config.analyse.dashboard.detail_url = "https://www.feishu.cn/"
    content = container.dashboard_report.send_dashboard_report()
    print("=== 看板 content 全文 ===")
    print(content)
finally:
    container.shutdown()
