"""FinAgent — hệ thống đa tác nhân thu thập tin tức và ra quyết định tài chính.

Máy chủ Ubuntu Server điều phối các máy con qua Redis + Celery; máy con thu thập
giá và tin tức, máy chủ tổng hợp, cho LLM suy luận, báo cáo qua Telegram để người
dùng duyệt, rồi tự động đặt lệnh và giám sát thị trường.
"""

__version__ = "0.1.0"
