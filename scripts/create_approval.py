"""创建「发版审批」定义,打印 approval_code。

控件 id 用 forms.FORM_FIELDS 的逻辑名(repo/tag/...),这样 server.py 创建实例时
form 的 id 能直接对上,不用再做映射。审批节点是「或签」,任一审批人通过即过。

用法:
  FEISHU_APP_ID=.. FEISHU_APP_SECRET=.. \
    python -m scripts.create_approval --approvers user_id_1,user_id_2 [--name 发版审批]

注意:create 审批定义接口对 form/node 的 schema 较挑剔。若这里报错,
退而用「审批后台」UI 可视化建定义(把控件 id/GUID 填到 config.form_field_ids),
两条路都行,见 README。
"""
import argparse
import json

import requests

from app import feishu, forms


def build_body(name: str, approvers: list[str]) -> dict:
    # i18n 文案:审批名 + 各控件 label + 节点名
    texts = [{"key": "@i18n@approval_name", "value": name},
             {"key": "@i18n@node_start", "value": "发起"},
             {"key": "@i18n@node_approve", "value": "审批"}]
    controls = []
    for key, label, ctype in forms.FORM_FIELDS:
        texts.append({"key": f"@i18n@field_{key}", "value": label})
        controls.append({
            "id": key,
            "type": ctype,
            "name": f"@i18n@field_{key}",
            "required": key in ("repo", "tag"),
        })

    # approvers 为空 = 动态(自选/Free)节点:审批人在创建实例时由 gate 传入
    # (= 审批群当前成员),或签;非空 = 固定 Personal 审批人。
    if approvers:
        approver = [{"type": "Personal", "user_id": u} for u in approvers]
    else:
        approver = [{"type": "Free"}]

    return {
        "approval_name": "@i18n@approval_name",
        "form": {"form_content": json.dumps(controls, ensure_ascii=False)},
        "viewers": [{"viewer_type": "TENANT"}],
        "node_list": [
            {"id": "START", "name": "@i18n@node_start", "node_type": "AND"},
            {"id": "APPROVE", "name": "@i18n@node_approve", "node_type": "OR",
             "approver": approver},
            {"id": "END", "name": "@i18n@node_approve", "node_type": "AND"},
        ],
        "i18n_resources": [{"locale": "zh-CN", "texts": texts, "is_default": True}],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--approvers", default="",
                    help="审批人 user_id,逗号分隔。留空=动态自选节点(审批人创建实例时传入,即审批群成员)")
    ap.add_argument("--name", default="发版审批")
    args = ap.parse_args()

    body = build_body(args.name, [u.strip() for u in args.approvers.split(",") if u.strip()])
    # user_id_type=user_id:让接口把 approver 里的 id 当 user_id 解析(默认当 open_id)
    r = requests.post(f"{feishu.BASE}/approval/v4/approvals?user_id_type=user_id",
                      headers=feishu._headers(), json=body, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        print("创建失败:", json.dumps(d, ensure_ascii=False, indent=2))
        print("\n→ 接口报错可改用审批后台 UI 建定义,见 README『方式 B』。")
        return
    code = d["data"]["approval_code"]
    print(f"approval_code = {code}")
    print("把它填进 config.yaml 的 feishu.approval_code")


if __name__ == "__main__":
    main()
