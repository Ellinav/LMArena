# 1. 使用一个官方的、轻量级的 Python 3.9 镜像作为基础
FROM python:3.10-slim

# 2. 在容器内部创建一个工作目录
WORKDIR /app

# 3. 将 requirements.txt 文件复制到工作目录中
#    我们先复制并安装依赖，这样可以利用Docker的层缓存，
#    如果只有代码变动，就不需要重新安装依赖，构建速度更快。
COPY requirements.txt .

# 4. 安装所有 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 5. 将项目中的所有其他文件（.py, .json, .md 等）复制到工作目录
COPY . .

# 6. 向 Docker 声明容器将监听 7860 端口
EXPOSE 7860

# 7. 定义容器启动时要执行的命令
#    这将启动 Uvicorn 服务器来运行你的 FastAPI 应用
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "7860"]