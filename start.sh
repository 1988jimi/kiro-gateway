#!/bin/bash
# Kiro Gateway 启动脚本

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 默认端口
PORT=${1:-8085}

# 检查虚拟环境是否存在
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    /opt/homebrew/bin/python3.11 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo "🚀 Starting Kiro Gateway on port $PORT..."
python main.py --port "$PORT"
