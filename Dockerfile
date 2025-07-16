# Dockerfile

# 1. 使用一个官方的、轻量级的 Python 3.10 镜像作为基础
FROM python:3.10-slim

# 2. 在容器内部创建一个工作目录
WORKDIR /app

# 3. 复制 requirements.txt 文件并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 将项目中的所有其他文件复制到工作目录
COPY . .

# 5. 向 Docker 声明容器将监听 7860 端口
EXPOSE 7860

# 6. 定义容器启动时要执行的命令
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "7860"]