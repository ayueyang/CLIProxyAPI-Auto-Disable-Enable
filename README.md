# CLIProxyAPI-Auto-Disable-Enable

[中文](#中文说明) | [English](#english)

---

## 中文说明

CLIProxyAPI Codex 账号自动禁用/解禁监控工具。通过 Web UI 管理 OpenAI Codex 账号，自动检测账号状态并禁用/恢复。

**本项目依赖 [CLIProxyAPI](https://github.com/router-for-me/Cli-Proxy-API)，请先部署 CLIProxyAPI。**

### 前置要求

- **[CLIProxyAPI](https://github.com/router-for-me/Cli-Proxy-API)** - 已部署并运行
- Python 3.9+
- pip

### 功能特性

- 🔍 **自动扫描** - 按分组间隔自动检测账号状态（valid / no_quota / invalid / unknown）
- 🚫 **自动禁用** - 无额度/无效账号自动重命名为 `.json.no_quota` / `.json.invalid`，CLIProxyAPI 不读取后缀文件
- ✅ **自动恢复** - 额度重置后自动恢复为 `.json`
- 🔓 **一键解禁** - 批量恢复所有被禁用的账号
- 💾 **智能备份** - Zip 压缩备份，显示备份份数和占用空间，自动清理需手动开启
- 📂 **路径管理** - 支持 Web UI 修改账号目录，带教程指引
- 🌍 **中英双语** - Web UI 支持中文/英文切换
- 🐳 **Docker 部署** - 支持 Docker 和 Docker Compose 容器化部署
- 📊 **导出/导入** - 支持 CSV 和 JSON 格式的账号导出与导入
- 📅 **重置时间** - 显示额度重置时间（X天后 / X小时后）
- 🌐 **Web UI** - 浏览器管理界面，实时日志

### 部署方式

#### 方式一：放在 CLIProxyAPI 目录内（简单）

将本项目文件直接放到 CLIProxyAPI 目录下，程序会自动读取同目录的 `config.yaml` 和 `data/` 文件夹。

```
CLIProxyAPI/
├── cli-proxy-api.exe
├── config.yaml
├── data/                    ← 账号文件目录
│   ├── codex-xxx@xxx.json
│   └── codex-xxx@xxx.json.no_quota
├── account_monitor_web.py   ← 本项目
├── manage_codex_accounts.py
├── start_account_monitor.bat
├── requirements.txt
└── ...
```

启动：

```bash
$env:CLIPROXYAPI_MANAGEMENT_KEY='your-password'
python account_monitor_web.py --port 8320
```

#### 方式二：独立目录部署（推荐）

将本项目放在独立目录，通过 `--config` 和 `--auth-dir` 参数指向 CLIProxyAPI 的配置和账号目录。

```
/path/to/CLIProxyAPI-Auto-Disable-Enable/
├── account_monitor_web.py
├── manage_codex_accounts.py
├── start_account_monitor.bat
├── requirements.txt
└── ...

/path/to/CLIProxyAPI/
├── cli-proxy-api.exe
├── config.yaml              ← --config 指向这里
└── data/                    ← --auth-dir 指向这里
```

启动：

```bash
# Linux/macOS
export CLIPROXYAPI_MANAGEMENT_KEY=your-password
python account_monitor_web.py --port 8320 --config /path/to/CLIProxyAPI/config.yaml --auth-dir /path/to/CLIProxyAPI/data

# Windows PowerShell
$env:CLIPROXYAPI_MANAGEMENT_KEY='your-password'
python account_monitor_web.py --port 8320 --config D:\CLIProxyAPI\config.yaml --auth-dir D:\CLIProxyAPI\data
```

#### 方式三：Docker 容器部署

使用 Docker Compose 一键部署 CLIProxyAPI 和 Monitor：

```yaml
# docker-compose.yml
services:
  cliproxyapi:
    image: cliproxyapi:latest
    container_name: cliproxyapi
    ports:
      - "8317:8317"
    volumes:
      - cliproxyapi-data:/app/data
      - ./config.yaml:/app/config.yaml:ro
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped

  monitor:
    build: .
    container_name: cliproxyapi-monitor
    ports:
      - "8320:8320"
    volumes:
      - cliproxyapi-data:/app/data:ro
    environment:
      - AUTH_DIR=/app/data
      - CLIPROXYAPI_MANAGEMENT_KEY=admin123
      - CLIPROXYAPI_URL=http://cliproxyapi:8317
      - TZ=Asia/Shanghai
    depends_on:
      - cliproxyapi
    restart: unless-stopped

volumes:
  cliproxyapi-data:
```

启动：

```bash
docker-compose up -d
```

**容器环境变量说明**：

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `AUTH_DIR` | 账号文件目录（容器内路径） | `/app/data` |
| `CLIPROXYAPI_MANAGEMENT_KEY` | CLIProxyAPI 管理密钥 | `admin123` |
| `CLIPROXYAPI_URL` | CLIProxyAPI 服务地址（容器间通信） | `http://cliproxyapi:8317` |
| `TZ` | 时区 | `Asia/Shanghai` |

> ⚠️ 容器中 `AUTH_DIR` 必须使用容器内路径（如 `/app/data`），不是宿主机路径。数据通过 Docker Volume 共享。

### 快速开始

1. **部署 CLIProxyAPI**

   参考 [CLIProxyAPI 文档](https://github.com/router-for-me/Cli-Proxy-API) 部署并启动。

2. **安装依赖**

   ```bash
   pip install -r requirements.txt
   ```

3. **设置管理密钥**

   CLIProxyAPI 的管理密钥（在 `config.yaml` 的 `remote-management.secret-key` 中设置）：

   ```bash
   # Linux/macOS
   export CLIPROXYAPI_MANAGEMENT_KEY=your-password

   # Windows PowerShell
   $env:CLIPROXYAPI_MANAGEMENT_KEY='your-password'

   # Windows CMD
   set CLIPROXYAPI_MANAGEMENT_KEY=your-password
   ```

4. **启动监控**

   ```bash
   python account_monitor_web.py --port 8320
   ```

   或使用批处理文件（Windows）：

   ```cmd
   start_account_monitor.bat
   ```

5. **访问 Web UI**

   打开浏览器访问 `http://127.0.0.1:8320`

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | 8320 | Web UI 端口 |
| `--host` | 0.0.0.0 | Web UI 绑定地址 |
| `--management-key` | - | CLIProxyAPI 管理密钥（也可通过环境变量设置） |
| `--config` | - | CLIProxyAPI 的 config.yaml 路径（独立部署时使用） |
| `--auth-dir` | data | 账号文件目录（独立部署时使用绝对路径） |

### 环境变量

| 变量 | 说明 | 优先级 |
|------|------|--------|
| `CLIPROXYAPI_MANAGEMENT_KEY` | CLIProxyAPI 管理密钥 | 必需 |
| `AUTH_DIR` | 账号文件目录路径 | `--auth-dir` > `AUTH_DIR` > config.yaml > 默认 `data` |
| `CLIPROXYAPI_URL` | CLIProxyAPI 服务地址 | 设置后覆盖 config.yaml 中的端口配置 |

### 账号文件命名规则

CLIProxyAPI 只读取 `.json` 文件，因此通过修改文件后缀实现禁用：

| 后缀 | 状态 | CLIProxyAPI 是否读取 |
|------|------|---------------------|
| `.json` | 正常/有效 | ✅ 是 |
| `.json.no_quota` | 无额度 | ❌ 否 |
| `.json.invalid` | 无效/被封 | ❌ 否 |
| `.json.unknown` | 状态未知 | ❌ 否 |

### 扫描分组间隔

| 分组 | 默认间隔 | 说明 |
|------|----------|------|
| valid | 60 分钟 | 有效账号定期检查 |
| no_quota | 30 分钟 | 无额度账号等待重置 |
| invalid | 120 分钟 | 无效账号低频检查 |
| unknown | 60 分钟 | 未知状态账号 |

### 备份管理

- **自动备份**：每次扫描完成后自动创建 Zip 备份
- **备份信息**：UI 显示备份份数和占用空间（如「总备份: 11份 1.7MB」）
- **自动清理**：默认关闭，需手动开启。开启时有警告确认弹窗
- **保留份数**：可自定义保留备份数量（默认 30 份）
- **手动备份**：点击「立即备份」按钮
- **恢复文件**：从备份恢复不存在的账号文件（已有文件不会被覆盖）

### API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取监控状态和账号列表 |
| `/api/start` | POST | 启动监控 |
| `/api/stop` | POST | 停止监控 |
| `/api/scan` | POST | 触发扫描 |
| `/api/toggle` | POST | 切换自动禁用/恢复/备份/清理 |
| `/api/intervals` | POST | 修改扫描间隔和保留份数 |
| `/api/backup-now` | POST | 立即备份 |
| `/api/backups` | GET | 获取备份列表 |
| `/api/restore` | POST | 从备份恢复 |
| `/api/enable-all` | POST | 一键解禁所有账号 |
| `/api/export?format=csv` | GET | 导出 CSV |
| `/api/export?format=json` | GET | 导出 JSON |
| `/api/import` | POST | 导入账号 |
| `/api/auth-dir` | GET | 获取当前账号目录 |
| `/api/auth-dir` | POST | 修改账号目录 |

### License

[MIT](LICENSE)

---

## English

CLIProxyAPI Codex Account Auto-Disable/Enable Monitor. Manage OpenAI Codex accounts via Web UI with automatic status detection and disable/restore.

**This project depends on [CLIProxyAPI](https://github.com/router-for-me/Cli-Proxy-API). Please deploy CLIProxyAPI first.**

### Prerequisites

- **[CLIProxyAPI](https://github.com/router-for-me/Cli-Proxy-API)** - Deployed and running
- Python 3.9+
- pip

### Features

- 🔍 **Auto Scan** - Periodically check account status by group (valid / no_quota / invalid / unknown)
- 🚫 **Auto Disable** - Rename no-quota/invalid accounts to `.json.no_quota` / `.json.invalid` (CLIProxyAPI ignores non-`.json` files)
- ✅ **Auto Restore** - Automatically restore accounts when quota resets
- 🔓 **One-Click Enable All** - Batch restore all disabled accounts
- 💾 **Smart Backup** - Zip compressed backups with backup count and size display, auto-cleanup requires manual enable
- 📂 **Path Management** - Change account directory via Web UI with built-in tutorial
- 🌍 **Bilingual UI** - Chinese/English language switching in Web UI
- 🐳 **Docker Support** - Docker and Docker Compose containerized deployment
- 📊 **Export/Import** - CSV and JSON format account export and import
- 📅 **Reset Time** - Display quota reset time (X days/hours until reset)
- 🌐 **Web UI** - Browser-based management interface with real-time logs

### Deployment

#### Option 1: Inside CLIProxyAPI directory (Simple)

Place the project files directly in the CLIProxyAPI directory. The program will automatically read `config.yaml` and `data/` from the same directory.

#### Option 2: Independent directory (Recommended)

Place the project in its own directory and use `--config` and `--auth-dir` to point to CLIProxyAPI's configuration and account directory.

```bash
export CLIPROXYAPI_MANAGEMENT_KEY=your-password
python account_monitor_web.py --port 8320 --config /path/to/CLIProxyAPI/config.yaml --auth-dir /path/to/CLIProxyAPI/data
```

#### Option 3: Docker Container Deployment

Deploy CLIProxyAPI and Monitor together using Docker Compose:

```yaml
# docker-compose.yml
services:
  cliproxyapi:
    image: cliproxyapi:latest
    container_name: cliproxyapi
    ports:
      - "8317:8317"
    volumes:
      - cliproxyapi-data:/app/data
      - ./config.yaml:/app/config.yaml:ro
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped

  monitor:
    build: .
    container_name: cliproxyapi-monitor
    ports:
      - "8320:8320"
    volumes:
      - cliproxyapi-data:/app/data:ro
    environment:
      - AUTH_DIR=/app/data
      - CLIPROXYAPI_MANAGEMENT_KEY=admin123
      - CLIPROXYAPI_URL=http://cliproxyapi:8317
      - TZ=Asia/Shanghai
    depends_on:
      - cliproxyapi
    restart: unless-stopped

volumes:
  cliproxyapi-data:
```

Start:

```bash
docker-compose up -d
```

**Container Environment Variables**:

| Variable | Description | Example |
|----------|-------------|---------|
| `AUTH_DIR` | Account directory path (container path) | `/app/data` |
| `CLIPROXYAPI_MANAGEMENT_KEY` | CLIProxyAPI management key | `admin123` |
| `CLIPROXYAPI_URL` | CLIProxyAPI service URL (inter-container) | `http://cliproxyapi:8317` |
| `TZ` | Timezone | `Asia/Shanghai` |

> ⚠️ In containers, `AUTH_DIR` must use the container path (e.g., `/app/data`), not the host path. Data is shared via Docker Volume.

### Quick Start

1. **Deploy CLIProxyAPI**

   Follow the [CLIProxyAPI documentation](https://github.com/router-for-me/Cli-Proxy-API) to deploy.

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set management key**

   ```bash
   # Linux/macOS
   export CLIPROXYAPI_MANAGEMENT_KEY=your-password

   # Windows PowerShell
   $env:CLIPROXYAPI_MANAGEMENT_KEY='your-password'

   # Windows CMD
   set CLIPROXYAPI_MANAGEMENT_KEY=your-password
   ```

4. **Start the monitor**

   ```bash
   python account_monitor_web.py --port 8320
   ```

5. **Open Web UI**

   Navigate to `http://127.0.0.1:8320` in your browser.

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--port` | 8320 | Web UI port |
| `--host` | 0.0.0.0 | Web UI bind address |
| `--management-key` | - | CLIProxyAPI management key (or use env var) |
| `--config` | - | Path to CLIProxyAPI config.yaml (for independent deployment) |
| `--auth-dir` | data | Path to CLIProxyAPI auth directory (for independent deployment) |

### Environment Variables

| Variable | Description | Priority |
|----------|-------------|----------|
| `CLIPROXYAPI_MANAGEMENT_KEY` | CLIProxyAPI management key | Required |
| `AUTH_DIR` | Account directory path | `--auth-dir` > `AUTH_DIR` > config.yaml > default `data` |
| `CLIPROXYAPI_URL` | CLIProxyAPI service URL | Overrides port from config.yaml when set |

### Account File Naming

CLIProxyAPI only reads `.json` files, so disabling is done by renaming:

| Suffix | Status | CLIProxyAPI Reads |
|--------|--------|-------------------|
| `.json` | Valid | ✅ Yes |
| `.json.no_quota` | No quota | ❌ No |
| `.json.invalid` | Invalid/Banned | ❌ No |
| `.json.unknown` | Unknown | ❌ No |

### Scan Group Intervals

| Group | Default Interval | Description |
|-------|-----------------|-------------|
| valid | 60 min | Periodic check for valid accounts |
| no_quota | 30 min | Wait for quota reset |
| invalid | 120 min | Low-frequency check for invalid accounts |
| unknown | 60 min | Check unknown status accounts |

### Backup Management

- **Auto Backup**: Zip backup created after each scan cycle
- **Backup Info**: UI shows backup count and size (e.g., "Total Backups: 11copies 1.7MB")
- **Auto Cleanup**: Disabled by default, requires manual enable with warning confirmation
- **Retention Count**: Customizable max backup count (default: 30)
- **Manual Backup**: Click "Backup Now" button
- **Restore**: Restore missing account files from backup (existing files are not overwritten)

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Get monitor status and account list |
| `/api/start` | POST | Start monitoring |
| `/api/stop` | POST | Stop monitoring |
| `/api/scan` | POST | Trigger scan |
| `/api/toggle` | POST | Toggle auto-disable/enable/backup/cleanup |
| `/api/intervals` | POST | Update scan intervals and retention count |
| `/api/backup-now` | POST | Create backup now |
| `/api/backups` | GET | Get backup list |
| `/api/restore` | POST | Restore from backup |
| `/api/enable-all` | POST | Enable all disabled accounts |
| `/api/export?format=csv` | GET | Export CSV |
| `/api/export?format=json` | GET | Export JSON |
| `/api/import` | POST | Import accounts |
| `/api/auth-dir` | GET | Get current account directory |
| `/api/auth-dir` | POST | Change account directory |

### License

[MIT](LICENSE)
