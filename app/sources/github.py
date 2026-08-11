"""GithubChangelogSource - 用 GitHub compare API + PR 归属提取拉变更清单.

薄 wrapper 调用 app.github.release_changes(),A3 阶段零逻辑改动.
"""
from app import github
from app.sources import ChangelogSource


class GithubChangelogSource(ChangelogSource):

    def release_changes(self, source_repo: str, base: str, head: str,
                        max_api_lookups: int = 30) -> dict:
        return github.release_changes(source_repo, base, head, max_api_lookups=max_api_lookups)
