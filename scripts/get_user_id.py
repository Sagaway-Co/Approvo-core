"""邮箱 -> 飞书 user_id。建审批定义、配 default_initiator/审批人时用。

用法:
  FEISHU_APP_ID=.. FEISHU_APP_SECRET=.. python -m scripts.get_user_id user1@example.com user2@example.com
"""
import sys

import requests

from app import feishu


def main(emails):
    r = requests.post(
        f"{feishu.BASE}/contact/v3/users/batch_get_id?user_id_type=user_id",
        headers=feishu._headers(),
        json={"emails": emails},
        timeout=10,
    )
    d = r.json()
    if d.get("code") != 0:
        print("ERROR:", d)
        sys.exit(1)
    for u in d.get("data", {}).get("user_list", []):
        print(f"{u.get('email', '?'):40} -> {u.get('user_id', '<未找到/无权限>')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
