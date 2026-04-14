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
- 💾 **自动备份** - Zip 压缩备份，自动清理旧备份
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

### API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取监控状态和账号列表 |
| `/api/start` | POST | 启动监控 |
| `/api/stop` | POST | 停止监控 |
| `/api/scan` | POST | 触发扫描 |
| `/api/toggle` | POST | 切换自动禁用/恢复 |
| `/api/intervals` | POST | 修改扫描间隔 |
| `/api/backup-now` | POST | 立即备份 |
| `/api/backups` | GET | 获取备份列表 |
| `/api/restore` | POST | 从备份恢复 |
| `/api/enable-all` | POST | 一键解禁所有账号 |
| `/api/export?format=csv` | GET | 导出 CSV |
| `/api/export?format=json` | GET | 导出 JSON |
| `/api/import` | POST | 导入账号 |

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
- 💾 **Auto Backup** - Zip compressed backups with auto-cleanup
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

### Quick Start

1. **Deploy CLIProxyAPI**

   Follow the [CLIProxyAPI documentation](https://github.com/router-for-me/Cli-Proxy-API) to deploy.

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set management key**

   ```bash
   export CLIPROXYAPI_MANAGEMENT_KEY=your-password
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

### Account File Naming

CLIProxyAPI only reads `.json` files, so disabling is done by renaming:

| Suffix | Status | CLIProxyAPI Reads |
|--------|--------|-------------------|
| `.json` | Valid | ✅ Yes |
| `.json.no_quota` | No quota | ❌ No |
| `.json.invalid` | Invalid/Banned | ❌ No |
| `.json.unknown` | Unknown | ❌ No |

### License

[MIT](LICENSE)
