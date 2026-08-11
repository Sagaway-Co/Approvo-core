"""列出机器人所在的群及其 chat_id,填到 config 的 notify_chat_id。

前提:已把应用(机器人)拉进目标群。

  FEISHU_APP_ID=.. FEISHU_APP_SECRET=.. python -m scripts.list_chats
"""
import requests

from app import feishu


def main():
    r = requests.get(f"{feishu.BASE}/im/v1/chats?page_size=100",
                     headers=feishu._headers(), timeout=10)
    d = r.json()
    if d.get("code") != 0:
        print("ERROR:", d)
        return
    items = d.get("data", {}).get("items", [])
    if not items:
        print("机器人不在任何群里。先把应用拉进目标群(群设置→添加应用/机器人)。")
        return
    for c in items:
        print(f"{c.get('chat_id'):40} {c.get('name', '(无名)')}")


if __name__ == "__main__":
    main()
