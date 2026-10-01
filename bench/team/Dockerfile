FROM python:3.12-slim
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends git ripgrep procps \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple pytest ruff
RUN git config --system user.email team@sciteam && git config --system user.name sciteam \
 && git config --system --add safe.directory '*'
WORKDIR /workspace
