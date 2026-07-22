# DUT-Environment-Setup-Check

## Project Structure

hub/
├── main.py                # FastAPI 入口（API + 内存缓存）
├── aggregator.py          # PC 聚合/转发逻辑（httpx）
├── nodes.json             # 节点配置
├── cache.json             # 自动生成：持久化缓存（重启不丢数据）
└── frontend/
    └── app.html           # 纯前端（HTML / CSS / JS）

## Installation

```bash
pip install fastapi uvicorn httpx
```

## Usage

### Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8001
```

### Manual startup (PowerShell)

```powershell
cd "c:\workspace\windows\DUT-Environment-Setup-Check\hub"
c:/workspace/windows/DUT-Environment-Setup-Check/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### Access the application

- **Frontend**: https://chenchangxinx.github.io/DUT-Environment-Setup-Check/
- **Local API**: http://localhost:8001

### Update DUT INFO

```bash
c:/workspace/windows/DUT-Environment-Setup-Check/.venv/Scripts/python.exe excel_to_nodes.py test.xlsx
```

### Health Check

```
✅ http://localhost:8001/api/health
```

## Jira 同步规则

### 同步入口与范围

- Jira 同步面板为全局面板（不再挂在每个 DUT 详情里）。
- 同步后，筛选条件对当前会话全局生效。

### 同步过滤规则（Sync Jira Cases）

点击 `Sync Jira Cases` 后，后端会按以下规则从 Jira/Zephyr 拉取用例：

- `Status = Approved`
- `Execution Type = Auto`
- `Folder NOT IN (/DEV_NVL_HX_NIT, /FPGA_NIT, /NVL_CIT_NIT, /PIT-Lite, Blank)`

说明：这里的 `Blank` 指 Folder 字段为空值（空白），不是字符串字面量 `"blank"` 或 `"blanks"`。

说明：

- 支持分页拉取并汇总结果。
- 同步结果会写入缓存（内存 + `hub/cache.json` 持久化）。

### 匹配规则（配置按钮）

匹配基于“当前选中的 DUT + 全局筛选器”执行。

- 当前 DUT：来自左侧节点树选中的 DUT。
- 四个筛选字段：
    - `Specific DUT`
    - `Light Equipment`
    - `Test Chart`
    - `Test Scene`

筛选值语义：

- `Any`：该字段不参与过滤。
- `(Empty)`：该字段必须为空。
- 具体值：按字段值精确匹配。

### 同步中的前端反馈

点击同步后会立即出现等待状态，而不是无响应：

- 全局按钮变为 `⏳ Syncing...` 且禁用。
- 弹窗显示 `正在同步 Jira cases，请稍候...`。
- 顶部状态栏显示 `Syncing Jira cases...`。
- 完成后恢复按钮并更新 `Synced: xxx cases @ time`。

## GitHub Pages Deployment

### 1. Initialize git at workspace root

```bash
git init
git add .
git commit -m "init: hub backend and github pages frontend"
```

### 2. Create GitHub repository and bind remote

```bash
git branch -M main
git remote add origin https://github.com/<your-account>/camera_lab_server.git
git push -u origin main
```

### 3. Enable GitHub Pages

- Go to Repository Settings → Pages
- Build and deployment → Source: Deploy from a branch
- Select Branch: main, Folder: /docs

### 4. Access your deployed page

```
https://<your-account>.github.io/camera_lab_server/
```

### 5. Configure backend API

- The docs page supports custom API Base input (e.g., https://your-server-host)
- If API Base is empty, it uses same-origin /api/*

