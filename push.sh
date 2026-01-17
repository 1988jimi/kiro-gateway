#!/bin/bash
# Kiro Gateway 推送脚本 - 同时推送到多个远程仓库

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 获取当前分支
BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo -e "${YELLOW}📦 Kiro Gateway 推送脚本${NC}"
echo "当前分支: $BRANCH"
echo ""

# 检查是否有未提交的更改
if [[ -n $(git status -s) ]]; then
    echo -e "${YELLOW}⚠️  检测到未提交的更改:${NC}"
    git status -s
    echo ""
    read -p "是否要提交这些更改? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "请输入提交信息: " commit_msg
        git add -A
        git commit -m "$commit_msg"
    fi
fi

# 推送到所有远程仓库
REMOTES=$(git remote)
SUCCESS=0
FAILED=0

for remote in $REMOTES; do
    echo -e "\n${YELLOW}➜ 推送到 $remote...${NC}"
    
    # 如果是 cursor 仓库,切换账户
    if [[ "$remote" == "cursor" ]]; then
        gh auth switch -u cursor-0001 2>/dev/null
    elif [[ "$remote" == "origin" ]]; then
        gh auth switch -u 1988jimi 2>/dev/null
    fi
    
    if git push "$remote" "$BRANCH" 2>&1; then
        echo -e "${GREEN}✅ $remote 推送成功${NC}"
        ((SUCCESS++))
    else
        echo -e "${RED}❌ $remote 推送失败${NC}"
        ((FAILED++))
    fi
done

echo ""
echo -e "${GREEN}完成! 成功: $SUCCESS, 失败: $FAILED${NC}"
