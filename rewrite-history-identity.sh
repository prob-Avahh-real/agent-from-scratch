#!/usr/bin/env bash
# 把 /Users/skat/projects（monorepo）所有提交的作者/提交者身份统一为 GitHub 身份。
#
# 不可逆操作（会改写全部历史哈希），--apply 模式下内置三重保护：
#   1. 每个本地分支打 backup/pre-rewrite/<branch> tag
#   2. 全量 bundle 备份
#   3. 先 stash 工作区改动，退出时自动恢复（失败也会恢复）
#
# 用法：
#   bash rewrite-history-identity.sh            # 演练：只读，打印分布与计划
#   bash rewrite-history-identity.sh --apply    # 备份 + 改写 + 强推远端 3 个分支
#
# 可覆盖（主要给自测用）：
#   AGENT_REPO_OVERRIDE  目标仓库路径，默认 /Users/skat/projects
#   AGENT_BUNDLE_DIR     bundle 备份目录，默认 /tmp
set -euo pipefail

REPO="${AGENT_REPO_OVERRIDE:-/Users/skat/projects}"
BUNDLE_DIR="${AGENT_BUNDLE_DIR:-/tmp}"
NAME="prob-Avahh-real"
EMAIL="272769682+prob-Avahh-real@users.noreply.github.com"
REMOTE_BRANCHES="master cursor/bilingual-translate-extension feat/travel-frog-ui"

APPLY=0
if [ $# -gt 0 ] && [ "$1" = "--apply" ]; then APPLY=1; fi

cd "$REPO"

echo "== 0. 当前身份分布 =="
git log --branches --format='%an <%ae>' | sort | uniq -c | sort -rn

echo "== 1. 将被改写的本地分支 =="
git for-each-ref --format='  %(refname:short)' refs/heads

echo "== 2. 将被强推的远端分支 =="
for rb in $REMOTE_BRANCHES; do echo "  $rb"; done

if [ "$APPLY" != "1" ]; then
  echo
  echo "== 演练结束：本次没有任何写入。加 --apply 才备份 + 改写 + 推送。 =="
  exit 0
fi

TS=$(date +%Y%m%d-%H%M%S)

echo "== 3. 给每个本地分支打备份 tag =="
for b in $(git for-each-ref --format='%(refname:short)' refs/heads); do
  SAFE=$(printf '%s' "$b" | tr '/' '-')
  git tag -f "backup/pre-rewrite/$SAFE" "$b" >/dev/null
  echo "  backup/pre-rewrite/$SAFE -> $b"
done

echo "== 4. 全量 bundle 备份 =="
mkdir -p "$BUNDLE_DIR"
BUNDLE="$BUNDLE_DIR/projects-backup-$TS.bundle"
git bundle create "$BUNDLE" --all | tail -1
echo "  备份文件: $BUNDLE"

echo "== 5. 暂存工作区 tracked 改动 =="
STASHED=0
if ! git diff-index --ignore-submodules --quiet HEAD --; then
  git stash push -m "pre-identity-rewrite-$TS" >/dev/null
  STASHED=1
fi
restore() {
  if [ "$STASHED" = "1" ]; then
    git stash pop >/dev/null 2>&1 && echo "(工作区改动已恢复)" || echo "!! stash pop 失败：请手动执行 git stash pop"
  fi
}
trap restore EXIT
git diff-index --ignore-submodules --quiet HEAD -- || { echo "!! 仍有未暂存改动，放弃"; exit 1; }

echo "== 6. filter-branch 改写全部本地分支 =="
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter "
export GIT_AUTHOR_NAME='$NAME'
export GIT_AUTHOR_EMAIL='$EMAIL'
export GIT_COMMITTER_NAME='$NAME'
export GIT_COMMITTER_EMAIL='$EMAIL'
" -- --branches

echo "== 7. 校验（旧身份必须清零） =="
if git log --branches --format='%ae' | grep -qE 'review@local|auto@local'; then
  echo "!! 仍有旧身份，放弃推送"; exit 1
fi
git log --branches --format='%an <%ae>' | sort | uniq -c

echo "== 8. 强推远端分支 =="
git push --force-with-lease origin $REMOTE_BRANCHES

echo "== 完成 =="
echo "回滚单支: git reset --hard backup/pre-rewrite/<branch>"
echo "回滚全部: git bundle unbundle $BUNDLE"
