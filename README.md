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