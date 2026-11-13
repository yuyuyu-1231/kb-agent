# 部署手册（公网）

> 目标：让面试官点开链接就能用，而不是只看 GitHub 代码。
> 本机已跑通（`docs/PROGRESS.md` 的「验收结果」），这里只写服务器上要做的事。

---

## 0. 买什么

| 项 | 要求 | 说明 |
|---|---|---|
| 规格 | **2 核 4G**（4G 是硬下限） | BGE 模型常驻几百 MB + Qdrant + uvicorn，2G 会 OOM |
| 系统 | Ubuntu 22.04 / 24.04 | 下面命令都按 apt 写 |
| 磁盘 | 40G 起 | 镜像 1.6G + Qdrant 数据 + 构建缓存 |
| 安全组 | 放开 **8000** | 先用 IP:8000 访问，域名备案后再说 80/443 |

阿里云「轻量应用服务器」或腾讯云「轻量」都行，学生认证后 10-40 元/月。

---

## 1. 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
docker --version && docker compose version
```

**国内服务器必做的一步**：给 Docker 配镜像加速，否则拉 `python:3.12-slim` 会卡十几分钟。

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com"
  ]
}
EOF
sudo systemctl restart docker
```

---

## 2. 把代码弄上去

**方式 A：服务器能连 GitHub**

```bash
git clone https://github.com/yuyuyu-1231/kb-agent.git
cd kb-agent
```

**方式 B：连不上（国内服务器常见）**

在本地打包，跳过 `.git` 和 `.venv`：

```powershell
# 本地 PowerShell，在项目根目录
git archive --format=zip -o kb-agent.zip HEAD
scp kb-agent.zip root@<服务器IP>:/root/
```

```bash
# 服务器上
apt-get install -y unzip && unzip -q kb-agent.zip -d kb-agent && cd kb-agent
```

---

## 3. 写 `.env`（**唯一不能提交的文件**）

```bash
cp .env.example .env
# 生成一个口令
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

编辑 `.env`，**这三项必须填对**：

```bash
LLM_API_KEY=sk-...                        # 你的 DeepSeek key
QDRANT_URL=http://qdrant:6333             # 容器里不能写 localhost！
ACCESS_TOKEN=<上一步生成的口令>            # 空着就是裸奔
```

> `QDRANT_URL` 在 `docker-compose.yml` 里已经被 `environment` 覆盖成服务名，
> 但 `.env` 里也不要写成 localhost，以免以后改 compose 时踩回去。

---

## 4. 起服务

```bash
# 先修 data 目录权限：容器里以 appuser（uid 10001）运行，
# 而解压出来的 data/ 归 root —— 不改这两行，容器会建不了 kb.db 而反复重启。
# （Windows 上 Docker Desktop 会把挂载权限放开，所以本机不会遇到这个坑。）
chown -R 10001:10001 /root/kb-agent/data

docker compose up -d --build
```

首次构建 **10-25 分钟**（拉基础镜像 + pip + 下载 95MB embedding 模型），
之后重建只要几十秒（层缓存命中）。

盯日志：

```bash
docker compose logs -f app
```

期望看到这几行：

```
[AUTH] 已启用访问口令，限流 60 次/分钟/IP
[AUTO_INGEST] IT设备与账号管理规范.pdf: 13 chunks
[AUTO_INGEST] 员工手册.pdf: 14 chunks
[AUTO_INGEST] 考勤与休假管理办法.pdf: 13 chunks
[AUTO_INGEST] 费用报销管理办法.pdf: 14 chunks
Application startup complete.
Uvicorn running on http://0.0.0.0:8000
```

**注意**：`[AUTO_INGEST]` 只在 collection 为空时出现。已经有数据时会**幂等跳过**，日志里什么都没有——这是正常行为，不是故障。
要单独验证入库逻辑，用临时 collection（不动现有数据）：

```bash
QDRANT_COLLECTION=kb_chunks_verify AUTO_INGEST=true \
  uvicorn app.main:app --port 8001
# 验证完：curl -X DELETE http://127.0.0.1:6333/collections/kb_chunks_verify
```

---

## 5. 验收（四项，缺一不可）

```bash
# ① 进程活着 + 知识库有数据
curl -s http://127.0.0.1:8000/health
# 期望 {"status":"ok","chunks":54,...}
#
# 54 = 4 份文档合计（员工手册 14 + IT 13 + 考勤 13 + 报销 14）。
# 旧文档写的 14 是 11/10 的单文档口径，已失效。

# ② 容器都 healthy
docker compose ps

# ③ 没口令必须 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/documents      # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Access-Token: $ACCESS_TOKEN" \
     http://127.0.0.1:8000/documents                                          # 200

# ④ 流式没被缓冲（关键）
curl -sN -X POST http://127.0.0.1:8000/qa/ask/stream \
  -H "Content-Type: application/json" \
  -H "X-Access-Token: $ACCESS_TOKEN" \
  -d '{"question":"年假有几天？"}' | grep -c '^event: token'
# 期望几十到几百，而不是 0 或 1
```

然后浏览器打开 `http://<公网IP>:8000/`，在右上角口令框里填 `ACCESS_TOKEN` 的值，点保存。

---

## 6. 收尾

```bash
# README 顶部加一行
echo "在线演示：http://<公网IP>:8000 （访问口令见简历/私信）" >> README.md
```

---

## 常见问题

| 症状 | 原因 | 处理 |
|---|---|---|
| 容器反复重启，日志 `sqlite3.OperationalError: unable to open database file` | **`data/` 目录归 root，容器里是 appuser（uid 10001），建不了 `kb.db`** | `chown -R 10001:10001 /root/kb-agent/data` 后 `docker compose up -d --force-recreate app` |
| 构建卡在 `Fetching 5 files 3/5` 十分钟 | hf-mirror 的网络问题 | 已内置 `HF_HUB_DISABLE_XET=1`；仍卡就重跑一次（层缓存会续上） |
| 构建卡在拉基础镜像 | Docker Hub 慢 | 检查第 1 步的 `registry-mirrors` 是否生效：`docker info \| grep -A2 Mirrors` |
| 服务起来后 `/health` 一直 degraded | Qdrant 没起来 | `docker compose ps` 看 qdrant 是否 healthy；`docker compose logs qdrant` |
| 容器不断重启 | 内存不足 | `dmesg \| grep -i oom` 确认，升到 4G 或加 swap |
| 页面打开但文档列表「需要访问口令」 | 口令没填/填错 | 右上角输入框重新填，存在 localStorage |
| 公网访问不了，服务器上 curl 正常 | 安全组没放开 8000 | 云控制台加一条入方向规则 |

---

## 日常运维

```bash
docker compose logs -f app          # 看日志
docker compose restart app          # 重启（改了 Python 代码要 --build；改前端 HTML 不用，static 是挂载的）
docker compose down                 # 停（保留数据）
docker compose down -v              # 停并删 Qdrant 数据（会触发重新入库）
git pull && docker compose up -d --build   # 更新
```

**数据在哪**：`data/`（trace、kb.db、corpus）挂在宿主机；Qdrant 在 named volume
`kb-agent-qdrant-storage` 里。`docker compose down` 不会丢。

**钱**：DeepSeek 按 token 计费，`/health` 和 `/documents` 不花钱，只有 `/qa/ask*` 花。
限流 60 次/分钟是防刷的兜底，不是成本控制 —— 真要控成本得在 LLM 侧设配额。
