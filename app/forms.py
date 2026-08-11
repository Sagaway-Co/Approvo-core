"""审批表单字段定义。

create_approval.py 用它生成审批定义的表单控件(控件 id == 这里的 key),
server.py 用它生成创建实例时的 form 值(value 的 id 必须和定义里的控件 id 对上,
否则飞书不认、值绑不上)。

如果你是在「审批后台」UI 里手搓的定义,UI 的控件 id 是自动生成的 GUID,
没法改成 repo/tag 这种。那就在 config.yaml 里配 form_field_ids 把逻辑名映射到 GUID。
"""

# (逻辑key, 中文label, 控件type)
FORM_FIELDS = [
    ("repo", "发版应用", "input"),
    ("tag", "版本 / 镜像 Tag", "input"),
    ("cluster", "目标集群", "input"),
    ("namespace", "命名空间", "input"),
    ("image", "完整镜像", "input"),
    ("operator", "发起人", "input"),
    ("commit", "Commit", "input"),
    ("notes", "说明", "textarea"),
]

KEYS = [k for k, _, _ in FORM_FIELDS]
