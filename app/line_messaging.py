import os
import requests

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("XXq1zxu9XZP3Ro6PKkK+p+vAJAfjWxLWGpO502uaGlIg+lO7fYocsl0Ju+bY4M8U3fDnmh3T5zwRl9iTmpsWZpygzwdYhE7AmIxy2BmJ2UOYYpJh/y9T3aL+sTe2sECDN/R87+cIRfaJVOIUgjA3IAdB04t89/1O/w1cDnyilFU=", "")
LINE_TARGET_ID = os.getenv("@147qwxpq", "")  # ใส่ userId หรือ groupId


def send_line_booking_notification(message: str) -> bool:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TARGET_ID:
        print("LINE config missing")
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": LINE_TARGET_ID,
        "messages": [
            {
                "type": "text",
                "text": message,
            }
        ],
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        print("LINE push status:", response.status_code)
        print("LINE push response:", response.text)
        return response.status_code == 200
    except Exception as e:
        print("LINE push error:", str(e))
        return False