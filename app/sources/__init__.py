"""变更清单来源抽象层.

approvo 在审批卡片里展示"上次成功部署 commit → 本次 commit"之间的 PR + 直接提交清单,
用于审批人 review 时判断本次发版包含了哪些改动.

当前内置 GitHub source(用 compare API + PR 归属提取).后续可加 GitLab / Gitea 等.
"""
from abc import ABC, abstractmethod


class ChangelogSource(ABC):

    @abstractmethod
    def release_changes(self, source_repo: str, base: str, head: str,
                        max_api_lookups: int = 30) -> dict:
        """拉取 base...head 之间的变更清单.

        返回:
          {
            "status": "ok" | "nodiff",
            "prs": [{"number", "title", "author", "url"}],
            "direct_commits": [{"sha", "message", "author"}],
            "total_commits": int,
            "compare_url": str,
          }
        异常直接抛,由调用方降级(变更清单失败不阻塞审批).
        """


def get_changelog_source() -> ChangelogSource:
    from app.sources.github import GithubChangelogSource
    return GithubChangelogSource()
