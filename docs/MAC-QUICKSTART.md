# KBase 在 Mac 上从零跑起来（零环境）

面向一台**什么都没装**的 Mac（Intel 或 Apple Silicon 都可）。跟着做，
先证明代码能跑（测试），再把服务真正启动起来试用。

预计时间：装环境 ~15 分钟；跑测试 ~5 分钟；启动服务首次 ~2–5 分钟。

---

## 0. 三条路线，先选一条

| 目标 | 需要什么 | 看哪节 |
|---|---|---|
| **只想证明代码没问题** | Python，**不需要任何 API Key、不下载模型** | §1 → §2 → §3 |
| **想真正启动服务试用问答** | 上面 + 一个大模型 API Key | §1 → §2 → §4 |
| **还想改前端并重新构建** | 上面 + Node（前端已预构建入库，不改前端可跳过） | §5 |

> 关键点：**跑测试不需要 Key 也不下载模型**（测试用内置假向量，且默认跳过
> 需要联网/PostgreSQL 的用例）。真正启动服务才需要一个 LLM Key。

---

## 1. 装基础环境

### 1.1 Homebrew（Mac 的包管理器）

打开「终端」(Terminal)，粘贴执行：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

装完按提示把 brew 加进 PATH（Apple Silicon 通常是这两行，终端会告诉你）：

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 1.2 Python 3.11 与 git

```bash
brew install python@3.11 git
python3.11 --version   # 应显示 Python 3.11.x
```

> 项目要求 Python **3.11**（`pyproject.toml` 里 `requires-python = ">=3.11"`）。
> 别用系统自带的旧 Python。

---

## 2. 克隆代码 + 建虚拟环境 + 装依赖

```bash
# 克隆并切到当前开发分支
git clone https://github.com/EricZehuaChang/KBase.git
cd KBase
git checkout feature/m5-1

# 建独立虚拟环境（隔离依赖，不污染系统 Python）
python3.11 -m venv .venv
source .venv/bin/activate     # 之后每开一个新终端都要先跑这句

# 升级 pip 后安装项目 + 开发依赖 + 本地向量化依赖
python -m pip install --upgrade pip
python -m pip install -e ".[dev,local-embed]"
```

`local-embed` 会拉 `sentence-transformers` 和 `torch`（Apple Silicon 自动用
GPU/MPS）。这一步下载较多、耗时几分钟，正常。

> 如果只想跑测试、暂时不想装 torch，可改成 `pip install -e ".[dev]"`
> （去掉 local-embed）。测试用假向量，不需要真模型。

---

## 3. 跑测试（证明代码能跑，无需 Key、无需模型）

```bash
source .venv/bin/activate   # 若刚才没激活
python -m pytest -q
```

预期结果：**400+ 个测试 passed，9 个 deselected**（deselected 是需要联网
下载模型或真实 PostgreSQL 的用例，按设计默认跳过，不是失败）。

看到 `xxx passed` 就说明整套后端在你的 Mac 上工作正常。

前端也有测试（可选，需要 Node，见 §5）：`cd web-app && npm install && npm test`。

---

## 4. 真正启动服务试用问答

### 4.1 配一个大模型 Key（至少一个）

服务默认用智谱 GLM 做问答、GLM-OCR 做扫描件识别，都读环境变量里的 Key。
在项目根目录创建 `.env` 文件（此文件已被 `.gitignore` 忽略，**不会进 git，
不会外泄**），填你自己的 Key：

```bash
cat > .env <<'EOF'
# 至少填一个大模型 Key（下面是默认配置用到的智谱）
ZHIPU_API_KEY=你自己的智谱key
# 可选：其它厂商，配了才能在页面切换
DASHSCOPE_API_KEY=你自己的通义key
DEEPSEEK_API_KEY=你自己的deepseek-key
OPENAI_API_KEY=你自己的openai-key
EOF
```

> Key 从各厂商开放平台申请（智谱 open.bigmodel.cn、通义 dashscope、
> DeepSeek、OpenAI）。**不要用别人的 Key。** 一个都不填也能启动，但问答会
>报错（没有可用的 LLM）。

### 4.2 让 .env 生效并启动

```bash
source .venv/bin/activate
# 把 .env 加载进当前终端环境
set -a; source .env; set +a

# 启动（首次会下载 bge-m3 向量模型，约 2GB，需几分钟；之后秒启）
uvicorn --factory kbase.api.main:create_app --port 8100
```

> 首次下载 bge-m3 慢或失败时，可先设镜像再启动：
> `export HF_ENDPOINT=https://hf-mirror.com`（在国内网络下有帮助）。

启动成功后浏览器打开 **http://localhost:8100** ：

- 使用端（问答）：http://localhost:8100
- 管理端（建库、传文档、设置）：http://localhost:8100/admin

**首次启动会自动创建管理员账号 `admin`，随机密码打印在启动日志里**
（终端里搜「首启引导」四个字）。也可以启动前用
`export KBASE_ADMIN_PASSWORD=你要的密码` 自己指定。

### 4.3 试一遍

1. 用 `admin` + 日志里的密码登录管理端
2. 新建一个知识库 → 上传几个 `.md`/`.pdf`/`.docx`（含表格的文档试试表格能力）
3. 到使用端选这个库提问，看带引用的回答
4. 管理端「设置」页能看运营看板、配模型、调检索策略

> 想省事免登录试用：本仓库有个开发启动脚本 `scripts/dev_app.py`
> （`uvicorn --factory scripts.dev_app:create_dev_app --port 8100`），
> 它用假向量秒启、免登录，但检索质量不作数，只适合走通流程。

---

## 5.（可选）改前端并重新构建

前端产物（`web/` 目录）**已经预构建并入库**，只跑后端不需要 Node。
只有你要改前端源码（`web-app/`）时才需要：

```bash
brew install node            # 装 Node 20+
cd web-app
npm install
npm run dev                  # 开发模式，改代码热更新（另需后端在 8100 跑着）
# 或
npm run build                # 重新构建到 ../web/，供后端直接托管
```

---

## 常见问题

- **`pytest` 报找不到 `kbase`**：确认在项目根目录、且 `source .venv/bin/activate`
  激活了虚拟环境、`pip install -e .` 成功。
- **`torch` 装不上/太慢**：先用 `pip install -e ".[dev]"`（不带 local-embed）
  跑测试；要真启动再补 `pip install "sentence-transformers>=3.0"`。
- **启动后问答报 503/错误**：`.env` 里没有可用的 LLM Key，或没执行
  `set -a; source .env; set +a`。
- **扫描件/图片文档一直「待OCR」**：没配 `ZHIPU_API_KEY`（GLM-OCR 用它）。
  文档 Key 补上后在文档行点「重试」即可。
- **端口被占**：换 `--port 8200` 等其它端口。

---

## 一眼速查（复制即用）

```bash
# 一次性装好并跑测试
brew install python@3.11 git
git clone https://github.com/EricZehuaChang/KBase.git && cd KBase
git checkout feature/m5-1
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip && python -m pip install -e ".[dev,local-embed]"
python -m pytest -q          # 看到 passed 即成功

# 启动服务（先建好 .env 填 Key）
set -a; source .env; set +a
uvicorn --factory kbase.api.main:create_app --port 8100
```
