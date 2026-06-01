from datetime import datetime


def send_line_notification(message: str):
    """
    Version 1: mock แจ้งเตือน
    ตอนนี้จะ print ออก console ก่อน
    ถ้าจะต่อ LINE Official จริง ให้เอา logic นี้ไปยิง Messaging API ได้เลย
    """
    print("\n=== LINE OFFICIAL NOTIFICATION ===")
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(message)
    print("=== END NOTIFICATION ===\n")