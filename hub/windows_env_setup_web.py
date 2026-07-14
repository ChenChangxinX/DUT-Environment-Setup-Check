import base64
import concurrent.futures
import json
import secrets
import socket
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PureWindowsPath

import paramiko


DEFAULT_VALIDATION_SHARE = r"C:\workspace\ValidationExecutionConfig.zip"
DEFAULT_AUTO_SHARE = r"C:\workspace\AutoCaseEnvInstall.bat"
DEFAULT_WORKSPACE_DIR = r"C:\AutoPackageSetup"
DEFAULT_SSH_PORT = 22
DEFAULT_BOARD_COUNT = 13
MAX_WORKERS = 6


def utc_now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def get_preferred_local_ip():
  probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    probe.connect(("8.8.8.8", 80))
    return probe.getsockname()[0]
  except OSError:
    return "127.0.0.1"
  finally:
    probe.close()


def encode_powershell(script_text):
    encoded = base64.b64encode(script_text.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def split_unc_path(path_text):
    if not path_text:
        raise ValueError("Share path is empty.")

    normalized = path_text.strip().replace("/", "\\")
    if not normalized.startswith("\\\\"):
        raise ValueError(f"Share path must be UNC format: {path_text}")

    parts = [part for part in normalized.split("\\") if part]
    if len(parts) < 3:
        raise ValueError(f"Share path is incomplete: {path_text}")

    share_root = "\\\\" + parts[0] + "\\" + parts[1]
    relative_path = "\\".join(parts[2:])
    return share_root, relative_path


def parse_source_path(path_text):
    if not path_text:
        raise ValueError("Source path is empty.")

    normalized = path_text.strip().replace("/", "\\")
    if normalized.startswith("\\\\"):
        share_root, relative_path = split_unc_path(normalized)
        return {
            "kind": "unc",
            "path": normalized,
            "share_root": share_root,
            "relative_path": relative_path,
        }

    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "\\":
        return {
            "kind": "local",
            "path": normalized,
        }

    raise ValueError(f"Source path must be UNC format or an absolute Windows path: {path_text}")


def ps_string(value):
    return "'" + value.replace("'", "''") + "'"


def parse_ssh_port(value):
  raw_value = str(value or DEFAULT_SSH_PORT).strip()
  try:
    port = int(raw_value)
  except ValueError as exc:
    raise ValueError(f"SSH port must be an integer: {raw_value}") from exc
  if port < 1 or port > 65535:
    raise ValueError(f"SSH port must be between 1 and 65535: {port}")
  return port


def append_share_copy_step(lines, label, share_root, share_relative, destination, drive_name):
  lines.extend([
    f"Write-Host '[STEP] Copy {label}'",
    f"Copy-ShareFile -ShareRoot {ps_string(share_root)} -RelativePath {ps_string(share_relative)} -Destination {destination} -Credential $shareCredential -DriveName '{drive_name}'",
  ])


def append_copy_step(lines, label, source, destination, drive_name):
  if source["kind"] == "unc":
    append_share_copy_step(
      lines,
      label,
      source["share_root"],
      source["relative_path"],
      destination,
      drive_name,
    )
    return

  lines.extend([
    f"Write-Host '[STEP] Use uploaded {label}'",
    f"if (-not (Test-Path -LiteralPath {destination})) {{ throw 'Uploaded file was not found: ' + {destination} }}",
  ])


def append_exit_code_check(lines, target_name):
  lines.append(
    f"if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {{ throw ('{target_name} failed with exit code ' + $LASTEXITCODE) }}"
  )


def build_remote_script(config):
    workspace_dir = PureWindowsPath(config["workspace_dir"])
    validation_dir = str(workspace_dir / "ValidationExecutionConfig")
    auto_dir = str(workspace_dir / "AutoEnv")
    validation_zip = str(PureWindowsPath(validation_dir) / "ValidationExecutionConfig.zip")
    validation_extract = str(PureWindowsPath(validation_dir) / "payload")
    validation_root = str(PureWindowsPath(validation_extract) / "ValidationExecutionConfig")
    validation_script = str(PureWindowsPath(validation_root) / "DUTConfig" / "ValidationExecutionConfig.ps1")
    auto_bat = str(PureWindowsPath(auto_dir) / "AutoCaseEnvInstall.bat")
    share_user = config.get("share_username", "").strip()
    share_password = config.get("share_password", "")

    lines = [
        "$ErrorActionPreference = 'Stop'",
      "$ProgressPreference = 'SilentlyContinue'",
        "Set-StrictMode -Version Latest",
        "function New-ShareCredential {",
        "    param([string]$UserName, [string]$Password)",
        "    if ([string]::IsNullOrWhiteSpace($UserName)) { return $null }",
        "    $secure = ConvertTo-SecureString $Password -AsPlainText -Force",
        "    return New-Object System.Management.Automation.PSCredential($UserName, $secure)",
        "}",
        "function Copy-ShareFile {",
        "    param(",
        "        [string]$ShareRoot,",
        "        [string]$RelativePath,",
        "        [string]$Destination,",
        "        [pscredential]$Credential,",
        "        [string]$DriveName",
        "    )",
        "    $parent = Split-Path -Path $Destination -Parent",
        "    New-Item -ItemType Directory -Path $parent -Force | Out-Null",
        "    if ($Credential) {",
        "        if (Get-PSDrive -Name $DriveName -ErrorAction SilentlyContinue) {",
        "            Remove-PSDrive -Name $DriveName -Force -ErrorAction SilentlyContinue",
        "        }",
        "        New-PSDrive -Name $DriveName -PSProvider FileSystem -Root $ShareRoot -Credential $Credential -Scope Script | Out-Null",
        "        try {",
        "            Copy-Item -LiteralPath ($DriveName + ':\\' + $RelativePath) -Destination $Destination -Force",
        "        } finally {",
        "            Remove-PSDrive -Name $DriveName -Force -ErrorAction SilentlyContinue",
        "        }",
        "    } else {",
        "        Copy-Item -LiteralPath (Join-Path -Path $ShareRoot -ChildPath $RelativePath) -Destination $Destination -Force",
        "    }",
        "}",
        f"$shareCredential = New-ShareCredential -UserName {ps_string(share_user)} -Password {ps_string(share_password)}",
        f"$workspaceDir = {ps_string(str(workspace_dir))}",
        "New-Item -ItemType Directory -Path $workspaceDir -Force | Out-Null",
        "Write-Host '[STEP] Workspace prepared'",
    ]

    if config["run_validation"]:
      validation_source = parse_source_path(config["validation_share"])
      lines.extend([
        f"$validationDir = {ps_string(validation_dir)}",
        f"$validationZip = {ps_string(validation_zip)}",
        f"$validationExtract = {ps_string(validation_extract)}",
        f"$validationRoot = {ps_string(validation_root)}",
        f"$validationScript = {ps_string(validation_script)}",
      ])
      append_copy_step(
        lines,
        "ValidationExecutionConfig.zip",
        validation_source,
        "$validationZip",
        "VEC",
      )
      lines.extend([
        "if (Test-Path -LiteralPath $validationExtract) { Remove-Item -LiteralPath $validationExtract -Recurse -Force }",
        "Write-Host '[STEP] Expand ValidationExecutionConfig.zip'",
        "Add-Type -AssemblyName System.IO.Compression.FileSystem",
        "[System.IO.Compression.ZipFile]::ExtractToDirectory($validationZip, $validationExtract)",
        "Write-Host '[STEP] Run ValidationExecutionConfig.ps1'",
        "if (-not (Test-Path -LiteralPath $validationRoot)) { throw 'ValidationExecutionConfig folder was not found after extraction: ' + $validationRoot }",
        "if (-not (Test-Path -LiteralPath $validationScript)) { throw 'ValidationExecutionConfig.ps1 was not found after extraction: ' + $validationScript }",
        "$validationScriptContent = Get-Content -LiteralPath $validationScript -Raw",
        "$validationScriptContent = $validationScriptContent.Replace('Write-Host \"Press any key to exit...\"', '')",
        "$validationScriptContent = $validationScriptContent.Replace('$host.UI.RawUI.ReadKey(\"NoEcho,IncludeKeyDown\") | Out-Null', '')",
        "Set-Content -LiteralPath $validationScript -Value $validationScriptContent -Encoding UTF8",
        "Push-Location -LiteralPath ([System.IO.Path]::GetDirectoryName($validationScript))",
        "try { & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $validationScript } finally { Pop-Location }",
      ])
      append_exit_code_check(lines, "ValidationExecutionConfig.ps1")

    if config["run_auto"]:
      auto_source = parse_source_path(config["auto_share"])
      lines.extend([
        f"$autoDir = {ps_string(auto_dir)}",
        f"$autoBat = {ps_string(auto_bat)}",
      ])
      append_copy_step(lines, "AutoCaseEnvInstall.bat", auto_source, "$autoBat", "AEC")
      lines.extend([
        "Write-Host '[STEP] Run AutoCaseEnvInstall.bat'",
        "cmd.exe /c ('\"' + $autoBat + '\"')",
      ])
      append_exit_code_check(lines, "AutoCaseEnvInstall.bat")

    lines.append("Write-Host '[DONE] Environment setup completed successfully'")
    return "\n".join(lines)


def build_remote_script_path(board_config):
    workspace_dir = PureWindowsPath(board_config["workspace_dir"])
    return str(workspace_dir / "copilot_remote_setup.ps1")


def ensure_remote_directory(client, remote_path):
  command = encode_powershell(
    f"New-Item -ItemType Directory -Path {ps_string(remote_path)} -Force | Out-Null"
  )
  _, stdout, stderr = client.exec_command(command, timeout=60)
  exit_code = stdout.channel.recv_exit_status()
  stderr_text = normalize_powershell_stderr(
    stderr.read().decode("utf-8", errors="replace")
  )
  if exit_code != 0:
    raise RemoteSetupError(build_remote_failure_message(exit_code, "", stderr_text))


def normalize_powershell_stderr(stderr_text):
  cleaned = (stderr_text or "").strip()
  if not cleaned:
    return ""
  if cleaned.startswith("#< CLIXML") and 'S="progress"' in cleaned and 'S="Error"' not in cleaned:
    return ""
  return cleaned


def upload_local_file(sftp, local_path, remote_path):
  with open(local_path, "rb") as source_file:
    with sftp.open(remote_path, "wb") as remote_file:
      while True:
        chunk = source_file.read(1024 * 1024)
        if not chunk:
          break
        remote_file.write(chunk)


class JobStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def create(self, board_views, options):
        job_id = secrets.token_hex(8)
        payload = {
            "job_id": job_id,
            "created_at": utc_now(),
            "status": "running",
            "options": options,
            "summary": {
                "total": len(board_views),
                "pending": len(board_views),
                "running": 0,
                "success": 0,
                "failed": 0,
            },
            "boards": {
                board["id"]: {
                    "id": board["id"],
                    "name": board["name"],
                    "host": board["host"],
                    "status": "pending",
                    "message": "Waiting to start.",
                    "started_at": "",
                    "finished_at": "",
                    "stdout": "",
                    "stderr": "",
                  "logs": [],
                }
                for board in board_views
            },
        }
        with self._lock:
            self._jobs[job_id] = payload
        return job_id

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return json.loads(json.dumps(job))

    def update_board(self, job_id, board_id, **changes):
        with self._lock:
            board = self._jobs[job_id]["boards"][board_id]
            board.update(changes)
            self._recalculate_summary(job_id)

    def append_board_log(self, job_id, board_id, line):
        entry = f"[{utc_now()}] {line}"
        with self._lock:
            board = self._jobs[job_id]["boards"][board_id]
            logs = board.setdefault("logs", [])
            logs.append(entry)
            if len(logs) > 200:
                del logs[:-200]

    def finish(self, job_id):
        with self._lock:
            job = self._jobs[job_id]
            summary = job["summary"]
            job["status"] = "failed" if summary["failed"] else "finished"
            self._recalculate_summary(job_id)

    def _recalculate_summary(self, job_id):
        job = self._jobs[job_id]
        states = [board["status"] for board in job["boards"].values()]
        summary = {
            "total": len(states),
            "pending": states.count("pending"),
            "running": states.count("running"),
            "success": states.count("success"),
            "failed": states.count("failed"),
        }
        job["summary"] = summary


def append_board_logs(job_id, board_id, text, prefix=""):
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        JOB_STORE.append_board_log(job_id, board_id, f"{prefix}{line}")


def stream_remote_command(channel, job_id, board_id):
    stdout_chunks = []
    stderr_chunks = []
    stdout_buffer = ""
    stderr_buffer = ""

    while True:
        made_progress = False

        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            stdout_chunks.append(chunk)
            stdout_buffer += chunk
            lines = stdout_buffer.splitlines(keepends=True)
            stdout_buffer = ""
            for line in lines:
                if line.endswith(("\n", "\r")):
                    JOB_STORE.append_board_log(job_id, board_id, line.strip())
                else:
                    stdout_buffer = line
            made_progress = True

        if channel.recv_stderr_ready():
            chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
            stderr_chunks.append(chunk)
            stderr_buffer += chunk
            lines = stderr_buffer.splitlines(keepends=True)
            stderr_buffer = ""
            for line in lines:
                if line.endswith(("\n", "\r")):
                    JOB_STORE.append_board_log(job_id, board_id, f"stderr: {line.strip()}")
                else:
                    stderr_buffer = line
            made_progress = True

        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break

        if not made_progress:
            time.sleep(0.2)

    if stdout_buffer.strip():
        JOB_STORE.append_board_log(job_id, board_id, stdout_buffer.strip())
    if stderr_buffer.strip():
        JOB_STORE.append_board_log(job_id, board_id, f"stderr: {stderr_buffer.strip()}")

    return "".join(stdout_chunks).strip(), "".join(stderr_chunks).strip(), channel.recv_exit_status()


JOB_STORE = JobStore()


class RemoteSetupError(RuntimeError):
    pass


def build_remote_failure_message(exit_code, stdout_text, stderr_text):
  details = []
  if exit_code is not None:
    details.append(f"Remote PowerShell failed with exit code {exit_code}.")
  if stderr_text:
    details.append(f"stderr:\n{stderr_text}")
  if stdout_text:
    details.append(f"stdout:\n{stdout_text}")
  return "\n\n".join(details) or "Remote PowerShell command failed."


def build_auth_failure_message(board_config):
  if board_config.get("password", "") == "":
    return "SSH 认证失败。目标板可能禁止空密码远程登录，请为该账号设置密码，或调整 Windows 安全策略后重试。"
  return "SSH 认证失败。请检查用户名、密码，以及目标板是否允许密码登录。"


def build_connection_reset_message(board_config):
  if board_config.get("password", "") == "":
    return "SSH 连接被目标板主动断开。目标板很可能不接受空密码远程登录，请先为该账号设置密码后重试。"
  return "SSH 连接被目标板主动断开。请检查目标板的 OpenSSH 配置、账号权限和安全策略。"


def build_session_closed_message(board_config):
  if board_config.get("password", "") == "":
    return "SSH 会话未建立或已被目标板立即关闭。目标板很可能不接受空密码远程登录，请先为该账号设置密码后重试。"
  return "SSH 会话未建立或已被目标板关闭。请检查目标板的 OpenSSH 配置、账号权限和安全策略。"


def run_remote_setup(board_config, job_id):
  board_id = board_config["id"]
  remote_script_path = build_remote_script_path(board_config)
  workspace_dir = PureWindowsPath(board_config["workspace_dir"])
  validation_zip_path = str((workspace_dir / "ValidationExecutionConfig") / "ValidationExecutionConfig.zip")
  auto_bat_path = str((workspace_dir / "AutoEnv") / "AutoCaseEnvInstall.bat")
  JOB_STORE.update_board(
    job_id,
    board_id,
    status="running",
    message="Connecting to remote host.",
    started_at=utc_now(),
  )
  JOB_STORE.append_board_log(job_id, board_id, f"开始连接 {board_config['host']}:{board_config['port']}")

  client = paramiko.SSHClient()
  client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
  stdout_text = ""
  stderr_text = ""
  exit_code = None

  try:
    script_text = build_remote_script(board_config)
    client.connect(
      hostname=board_config["host"],
      port=board_config["port"],
      username=board_config["username"],
      password=board_config["password"],
      timeout=15,
      banner_timeout=15,
      auth_timeout=15,
    )
    JOB_STORE.append_board_log(job_id, board_id, "SSH 连接已建立")
    ensure_remote_directory(client, str(workspace_dir))
    JOB_STORE.append_board_log(job_id, board_id, f"正在上传远端脚本: {remote_script_path}")
    sftp = client.open_sftp()
    try:
      if board_config["run_validation"]:
        validation_source = parse_source_path(board_config["validation_share"])
      else:
        validation_source = None
      if board_config["run_auto"]:
        auto_source = parse_source_path(board_config["auto_share"])
      else:
        auto_source = None

      if validation_source and validation_source["kind"] == "local":
        ensure_remote_directory(client, str(workspace_dir / "ValidationExecutionConfig"))
        JOB_STORE.append_board_log(job_id, board_id, f"正在上传 ValidationExecutionConfig.zip: {validation_zip_path}")
        upload_local_file(sftp, validation_source["path"], validation_zip_path)
      if auto_source and auto_source["kind"] == "local":
        ensure_remote_directory(client, str(workspace_dir / "AutoEnv"))
        JOB_STORE.append_board_log(job_id, board_id, f"正在上传 AutoCaseEnvInstall.bat: {auto_bat_path}")
        upload_local_file(sftp, auto_source["path"], auto_bat_path)
      with sftp.open(remote_script_path, "w") as remote_script_file:
        remote_script_file.write(script_text)
    finally:
      sftp.close()
    JOB_STORE.update_board(
      job_id,
      board_id,
      message="Remote command started.",
    )
    command = (
      "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
      f'-File "{remote_script_path}"'
    )
    _, stdout, stderr = client.exec_command(command, timeout=3600)
    JOB_STORE.append_board_log(job_id, board_id, "远端命令已启动，正在接收执行日志")
    stdout_text, stderr_text, exit_code = stream_remote_command(stdout.channel, job_id, board_id)
    stderr_text = normalize_powershell_stderr(stderr_text)

    if exit_code != 0:
      raise RemoteSetupError(build_remote_failure_message(exit_code, stdout_text, stderr_text))

    JOB_STORE.update_board(
      job_id,
      board_id,
      status="success",
      message="Environment setup completed." if not stderr_text else "Environment setup completed with stderr output.",
      finished_at=utc_now(),
      stdout=stdout_text,
      stderr=stderr_text,
    )
    JOB_STORE.append_board_log(job_id, board_id, "环境配置完成")
  except paramiko.AuthenticationException:
    failure_text = build_auth_failure_message(board_config)
    JOB_STORE.append_board_log(job_id, board_id, failure_text)
    JOB_STORE.update_board(
      job_id,
      board_id,
      status="failed",
      message=failure_text,
      finished_at=utc_now(),
      stdout=stdout_text,
      stderr=failure_text,
    )
  except ConnectionResetError:
    failure_text = build_connection_reset_message(board_config)
    JOB_STORE.append_board_log(job_id, board_id, failure_text)
    JOB_STORE.update_board(
      job_id,
      board_id,
      status="failed",
      message=failure_text,
      finished_at=utc_now(),
      stdout=stdout_text,
      stderr=failure_text,
    )
  except paramiko.SSHException as exc:
    failure_text = str(exc)
    if failure_text == "No existing session":
      failure_text = build_session_closed_message(board_config)
    JOB_STORE.append_board_log(job_id, board_id, failure_text)
    JOB_STORE.update_board(
      job_id,
      board_id,
      status="failed",
      message=failure_text,
      finished_at=utc_now(),
      stdout=stdout_text,
      stderr=failure_text,
    )
  except Exception as exc:
    failure_text = getattr(exc, "args", [str(exc)])[0]
    JOB_STORE.append_board_log(job_id, board_id, f"执行失败: {failure_text.splitlines()[0]}")
    JOB_STORE.update_board(
      job_id,
      board_id,
      status="failed",
      message=failure_text.splitlines()[0],
      finished_at=utc_now(),
      stdout=stdout_text,
      stderr=stderr_text or failure_text,
    )
  finally:
    client.close()


def sanitize_board_view(board):
    return {
        "id": board["id"],
        "name": board["name"],
        "host": board["host"],
    }


def validate_payload(payload):
  global_username = payload.get("global_username", "").strip()
  global_password = payload.get("global_password", "")
  ssh_port = parse_ssh_port(payload.get("ssh_port"))
  workspace_dir = (payload.get("workspace_dir") or DEFAULT_WORKSPACE_DIR).strip()
  validation_share = (payload.get("validation_share") or DEFAULT_VALIDATION_SHARE).strip()
  auto_share = (payload.get("auto_share") or DEFAULT_AUTO_SHARE).strip()
  share_username = payload.get("share_username", "").strip()
  share_password = payload.get("share_password", "")
  run_validation = bool(payload.get("run_validation", True))
  run_auto = bool(payload.get("run_auto", True))

  if not run_validation and not run_auto:
    raise ValueError("Select at least one install step.")
  if not workspace_dir:
    raise ValueError("Workspace directory is required.")

  boards = []
  for index, board in enumerate(payload.get("boards", []), start=1):
    if not board.get("enabled"):
      continue

    host = (board.get("host") or "").strip()
    if not host:
      continue

    username = (board.get("username") or global_username).strip()
    password = board.get("password")
    if password in (None, ""):
      password = global_password

    if not username:
      raise ValueError(f"SSH username is required for board {index}.")

    boards.append(
      {
        "id": board.get("id") or f"board-{index}",
        "name": board.get("name") or f"Board {index}",
        "host": host,
        "port": ssh_port,
        "username": username,
        "password": password,
        "workspace_dir": workspace_dir,
        "validation_share": validation_share,
        "auto_share": auto_share,
        "share_username": share_username,
        "share_password": share_password,
        "run_validation": run_validation,
        "run_auto": run_auto,
      }
    )

  if not boards:
    raise ValueError("Provide at least one enabled board with an IP address.")

  options = {
    "run_validation": run_validation,
    "run_auto": run_auto,
    "workspace_dir": workspace_dir,
    "validation_share": validation_share,
    "auto_share": auto_share,
  }
  return boards, options


def execute_job(job_id, boards):
  try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(boards))) as executor:
      futures = [executor.submit(run_remote_setup, board, job_id) for board in boards]
      for future in concurrent.futures.as_completed(futures):
        future.result()
  finally:
    JOB_STORE.finish(job_id)


PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Windows RVP环境配置</title>
  <style>
    :root {
      --bg: #eaf3ff;
      --panel: rgba(245, 250, 255, 0.94);
      --panel-strong: #f9fcff;
      --ink: #17324d;
      --muted: #5b728c;
      --accent: #1668c7;
      --accent-2: #0d4f99;
      --danger: #b42318;
      --border: rgba(23, 50, 77, 0.12);
      --shadow: 0 18px 45px rgba(22, 104, 199, 0.14);
      --radius: 22px;
      --font: "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--font);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(76, 154, 255, 0.24), transparent 28%),
        radial-gradient(circle at top right, rgba(22, 104, 199, 0.2), transparent 24%),
        linear-gradient(180deg, #f4f9ff 0%, #ddeeff 100%);
    }
    .shell {
      width: min(1680px, calc(100% - 24px));
      margin: 12px auto 28px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1fr;
      gap: 20px;
      margin-bottom: 20px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }
    .hero-main {
      padding: 22px 24px;
    }
    h1, h2, h3, p { margin: 0; }
    h1 {
      font-size: clamp(22px, 2.4vw, 32px);
      line-height: 1.12;
      letter-spacing: -0.02em;
      margin-bottom: 8px;
      max-width: 100%;
    }
    .subtext {
      color: var(--muted);
      max-width: 100ch;
      line-height: 1.5;
      font-size: 14px;
    }
    .pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }
    .pill {
      border: 1px solid rgba(22, 104, 199, 0.2);
      color: var(--accent);
      background: rgba(22, 104, 199, 0.08);
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.7fr) minmax(360px, 0.85fr);
      gap: 16px;
      align-items: start;
    }
    .section {
      padding: 18px;
    }
    .section-title {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 14px;
    }
    .section-title h2 {
      font-size: 22px;
      letter-spacing: -0.03em;
    }
    .section-title span {
      font-size: 13px;
      color: var(--muted);
    }
    .grid-2, .grid-3 {
      display: grid;
      gap: 12px;
    }
    .grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    label {
      display: block;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 8px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    input {
      width: 100%;
      border: 1px solid rgba(31, 41, 51, 0.14);
      border-radius: 12px;
      background: #fff;
      padding: 11px 12px;
      font: inherit;
      color: var(--ink);
    }
    input:focus {
      outline: 2px solid rgba(22, 104, 199, 0.18);
      border-color: var(--accent-2);
    }
    .toggle-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .toggle {
      display: inline-flex;
      gap: 10px;
      align-items: center;
      padding: 9px 12px;
      border-radius: 12px;
      background: rgba(22, 104, 199, 0.08);
      border: 1px solid rgba(22, 104, 199, 0.12);
    }
    .toggle input { width: auto; }
    .boards {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }
    .board-card {
      border: 1px solid rgba(31, 41, 51, 0.1);
      background: var(--panel-strong);
      border-radius: 16px;
      padding: 14px;
      transition: transform 140ms ease, box-shadow 140ms ease;
    }
    .board-card:hover {
      transform: translateY(-2px);
      box-shadow: 0 10px 24px rgba(22, 104, 199, 0.1);
    }
    .board-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
    }
    .board-body {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr) minmax(220px, 0.95fr) auto;
      gap: 10px;
      align-items: end;
    }
    .board-actions {
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 150px;
    }
    .board-head strong {
      font-size: 15px;
    }
    .mini {
      font-size: 12px;
      color: var(--muted);
      margin-top: 6px;
    }
    .actions {
      display: flex;
      gap: 12px;
      align-items: center;
      margin-top: 16px;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 12px;
      padding: 11px 16px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .primary {
      color: #fff;
      background: linear-gradient(135deg, var(--accent), #4c9aff);
      box-shadow: 0 14px 24px rgba(22, 104, 199, 0.28);
    }
    .secondary {
      background: rgba(22, 104, 199, 0.08);
      color: var(--ink);
    }
    .board-run-validation {
      color: #fff;
      background: linear-gradient(135deg, #0d4f99, #1668c7);
      box-shadow: 0 8px 16px rgba(13, 79, 153, 0.18);
      padding: 7px 10px;
      border-radius: 10px;
      font-size: 12px;
      line-height: 1.25;
    }
    .board-run-auto {
      color: #fff;
      background: linear-gradient(135deg, #1f7ae0, #69aefc);
      box-shadow: 0 8px 16px rgba(31, 122, 224, 0.18);
      padding: 7px 10px;
      border-radius: 10px;
      font-size: 12px;
      line-height: 1.25;
    }
    .board-note {
      margin-top: 8px;
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
    }
    .status-panel {
      position: sticky;
      top: 12px;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric {
      border-radius: 14px;
      padding: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(255,255,255,0.74));
      border: 1px solid rgba(31, 41, 51, 0.1);
    }
    .metric strong {
      display: block;
      font-size: 24px;
      margin-top: 8px;
      letter-spacing: -0.04em;
    }
    .status-list {
      display: grid;
      gap: 12px;
    }
    .status-item {
      border: 1px solid rgba(31, 41, 51, 0.1);
      border-radius: 14px;
      background: #fff;
      padding: 12px;
    }
    .status-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 88px;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .pending { background: rgba(31, 41, 51, 0.08); color: var(--ink); }
    .running { background: rgba(22, 104, 199, 0.12); color: var(--accent-2); }
    .success { background: rgba(27, 135, 78, 0.14); color: #17603c; }
    .failed { background: rgba(180, 35, 24, 0.12); color: var(--danger); }
    .log-title {
      margin: 12px 0 6px;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    pre {
      margin: 12px 0 0;
      padding: 12px;
      border-radius: 12px;
      background: #f8f5ef;
      color: #334155;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 220px;
      overflow: auto;
      border: 1px solid rgba(31, 41, 51, 0.08);
    }
    .job-note {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 10px;
      min-height: 20px;
    }
    @media (max-width: 1120px) {
      .hero, .layout { grid-template-columns: 1fr; }
      .status-panel { position: static; }
      .board-body { grid-template-columns: 1fr 1fr; }
      .board-actions { grid-column: 1 / -1; }
    }
    @media (max-width: 720px) {
      .shell { width: min(100% - 20px, 100%); }
      .grid-2, .grid-3, .summary { grid-template-columns: 1fr; }
      .board-body { grid-template-columns: 1fr; }
      .board-actions { min-width: 100%; }
      .section { padding: 18px; }
      h1 { max-width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="panel hero-main">
        <p class="pill">Windows OpenSSH Batch Setup</p>
        <h1>Windows RVP环境配置</h1>
        <p class="subtext">页面会通过 Windows OpenSSH 连接目标板执行 ValidationExecutionConfig 和 Auto ENV 安装流程。你可以批量执行，也可以在每一块板子上单独触发某一个安装步骤。</p>
        <div class="pill-row">
          <div class="pill">ValidationExecutionConfig.zip</div>
          <div class="pill">AutoCaseEnvInstall.bat</div>
          <div class="pill">支持并发</div>
        </div>
      </div>
    </section>

    <div class="layout">
      <section class="panel section">
        <div class="section-title">
          <h2>连接与安装配置</h2>
          <span>默认支持 13 台板子</span>
        </div>

        <div class="toggle-row">
          <label class="toggle"><input type="checkbox" id="run-validation" checked> 执行 ValidationExecutionConfig</label>
          <label class="toggle"><input type="checkbox" id="run-auto" checked> 执行 Auto ENV 安装</label>
        </div>

        <div class="grid-3">
          <div>
            <label for="global-username">默认 SSH 用户名</label>
            <input id="global-username" placeholder="例如 Administrator，作为所有板子的默认账号">
          </div>
          <div>
            <label for="global-password">默认 SSH 密码</label>
            <input id="global-password" type="password" placeholder="作为所有板子的默认密码">
          </div>
          <div>
            <label for="ssh-port">SSH 端口</label>
            <input id="ssh-port" value="22">
          </div>
        </div>

        <div class="grid-2" style="margin-top:14px;">
          <div>
            <label for="share-username">共享盘用户名</label>
            <input id="share-username" placeholder="可留空，表示使用远端当前账号访问共享盘">
          </div>
          <div>
            <label for="share-password">共享盘密码</label>
            <input id="share-password" type="password" placeholder="共享盘密码，可留空">
          </div>
        </div>

        <div class="grid-1" style="margin-top:14px; display:grid; gap:14px;">
          <div>
            <label for="workspace-dir">远端工作目录</label>
            <input id="workspace-dir" value="C:\AutoPackageSetup">
          </div>
          <div>
            <label for="validation-share">ValidationExecutionConfig 源路径</label>
            <input id="validation-share" value="C:\workspace\ValidationExecutionConfig.zip">
          </div>
          <div>
            <label for="auto-share">Auto ENV 源路径</label>
            <input id="auto-share" value="C:\workspace\AutoCaseEnvInstall.bat">
          </div>
        </div>

        <div class="section-title" style="margin-top:22px;">
          <h2>Windows Boards</h2>
          <span>13 块板子按 13 行独立排列</span>
        </div>
        <div id="boards" class="boards"></div>

        <div class="actions">
          <button class="primary" id="run-btn">开始批量配置</button>
          <button class="secondary" id="fill-btn">重置 13 个空白板卡</button>
          <button class="secondary" id="add-btn">添加 板卡</button>
          <button class="secondary" id="remove-btn">删除 最后一个板卡</button>
          <span class="job-note" id="submit-note"></span>
        </div>
      </section>

      <section class="panel section status-panel">
        <div class="section-title">
          <h2>任务状态</h2>
          <span id="job-id">未启动</span>
        </div>
        <div class="summary">
          <div class="metric"><span>总数</span><strong id="total-count">0</strong></div>
          <div class="metric"><span>运行中</span><strong id="running-count">0</strong></div>
          <div class="metric"><span>成功</span><strong id="success-count">0</strong></div>
          <div class="metric"><span>失败</span><strong id="failed-count">0</strong></div>
        </div>
        <div class="job-note" id="job-note">提交后会自动轮询每台板子的执行结果。</div>
        <div id="status-list" class="status-list"></div>
      </section>
    </div>
  </div>

  <script>
    let boardCount = 13;
    const boardContainer = document.getElementById('boards');
    const statusList = document.getElementById('status-list');
    const jobIdNode = document.getElementById('job-id');
    const jobNoteNode = document.getElementById('job-note');
    const submitNoteNode = document.getElementById('submit-note');
    let pollTimer = null;

    function addBoard() {
      boardCount += 1;
      boardContainer.insertAdjacentHTML('beforeend', boardCard(boardCount));
      submitNoteNode.textContent = `已添加板卡 ${boardCount}`;
      updateCountsDisplay();
    }

    function removeLastBoard() {
      const last = boardContainer.querySelector('.board-card:last-child');
      if (!last) {
        submitNoteNode.textContent = '没有可删除的板卡';
        return;
      }
      last.remove();
      boardCount = Math.max(0, boardContainer.querySelectorAll('.board-card').length);
      submitNoteNode.textContent = `已删除最后一个板卡，当前数量 ${boardCount}`;
      updateCountsDisplay();
    }

    function updateCountsDisplay() {
      document.getElementById('total-count').textContent = boardContainer.querySelectorAll('.board-card').length;
    }

    function boardCard(index, data = {}) {
      const enabled = data.enabled !== undefined ? data.enabled : (index <= boardCount);
      const name = (data.name !== undefined && data.name !== null) ? data.name : `Board ${index}`;
      const host = (data.host !== undefined && data.host !== null) ? data.host : '';
      const username = (data.username !== undefined && data.username !== null) ? data.username : '';
      const password = (data.password !== undefined && data.password !== null) ? data.password : '';
      return `
        <div class="board-card" data-board-index="${index}">
          <div class="board-head">
            <strong>Board ${index}</strong>
            <label class="toggle" style="padding:6px 10px; margin:0;"><input type="checkbox" data-field="enabled" ${enabled ? 'checked' : ''}> 启用</label>
          </div>
          <div class="board-body">
            <div>
              <label>板卡名称</label>
              <input data-field="name" value="${escapeHtml(name)}">
            </div>
            <div>
              <label>IP 地址</label>
              <input data-field="host" value="${escapeHtml(host)}" placeholder="例如 10.239.x.x">
            </div>
            <div class="grid-2">
              <div>
                <label>单板覆盖用户名</label>
                <input data-field="username" value="${escapeHtml(username)}" placeholder="留空则使用默认 SSH 用户名">
              </div>
              <div>
                <label>单板覆盖密码</label>
                <input data-field="password" type="password" value="${escapeHtml(password)}" placeholder="留空则使用默认 SSH 密码">
              </div>
            </div>
            <div class="board-actions">
              <button class="board-run-validation" type="button" data-action="validation">执行 ValidationExecutionConfig</button>
              <button class="board-run-auto" type="button" data-action="auto">执行 Auto ENV 安装</button>
              <button class="secondary" type="button" data-action="remove">删除</button>
            </div>
          </div>
          <div class="board-note" data-role="board-note"></div>
        </div>`;
    }

    function renderBoards() {
      // Preserve existing values where possible
      const existing = [...document.querySelectorAll('.board-card')].map((card) => ({
        index: Number(card.dataset.boardIndex),
        enabled: card.querySelector('[data-field="enabled"]').checked,
        name: card.querySelector('[data-field="name"]').value,
        host: card.querySelector('[data-field="host"]').value,
        username: card.querySelector('[data-field="username"]').value,
        password: card.querySelector('[data-field="password"]').value,
      }));
      const existingMap = Object.fromEntries(existing.map((e) => [e.index, e]));

      boardContainer.innerHTML = Array.from({ length: boardCount }, (_, i) => {
        const idx = i + 1;
        const data = existingMap[idx] || {};
        return boardCard(idx, data);
      }).join('');
      updateCountsDisplay();
    }

    function collectBoards() {
      return [...document.querySelectorAll('.board-card')].map((card, index) => ({
        id: `board-${index + 1}`,
        enabled: card.querySelector('[data-field="enabled"]').checked,
        name: card.querySelector('[data-field="name"]').value.trim() || `Board ${index + 1}`,
        host: card.querySelector('[data-field="host"]').value.trim(),
        username: card.querySelector('[data-field="username"]').value.trim(),
        password: card.querySelector('[data-field="password"]').value,
      }));
    }

    function collectBoard(card) {
      const index = Number(card.dataset.boardIndex);
      return {
        id: `board-${index}`,
        enabled: card.querySelector('[data-field="enabled"]').checked,
        name: card.querySelector('[data-field="name"]').value.trim() || `Board ${index}`,
        host: card.querySelector('[data-field="host"]').value.trim(),
        username: card.querySelector('[data-field="username"]').value.trim(),
        password: card.querySelector('[data-field="password"]').value,
      };
    }

    function collectBasePayload() {
      return {
        global_username: document.getElementById('global-username').value.trim(),
        global_password: document.getElementById('global-password').value,
        ssh_port: document.getElementById('ssh-port').value.trim(),
        share_username: document.getElementById('share-username').value.trim(),
        share_password: document.getElementById('share-password').value,
        workspace_dir: document.getElementById('workspace-dir').value.trim(),
        validation_share: document.getElementById('validation-share').value.trim(),
        auto_share: document.getElementById('auto-share').value.trim(),
      };
    }

    function collectPayload() {
      return {
        ...collectBasePayload(),
        run_validation: document.getElementById('run-validation').checked,
        run_auto: document.getElementById('run-auto').checked,
        boards: collectBoards(),
      };
    }

    function setBoardNote(card, message) {
      card.querySelector('[data-role="board-note"]').textContent = message;
    }

    function renderStatus(job) {
      jobIdNode.textContent = `Job ${job.job_id}`;
      jobNoteNode.textContent = `创建时间: ${job.created_at} | 状态: ${job.status}`;
      document.getElementById('total-count').textContent = job.summary.total;
      document.getElementById('running-count').textContent = job.summary.running;
      document.getElementById('success-count').textContent = job.summary.success;
      document.getElementById('failed-count').textContent = job.summary.failed;

      const boards = Object.values(job.boards);
      statusList.innerHTML = boards.map((board) => {
        const details = [board.message];
        if (board.started_at) details.push(`开始: ${board.started_at}`);
        if (board.finished_at) details.push(`结束: ${board.finished_at}`);
        const logText = (board.logs || []).join('\n');
        const outputBlocks = [];
        if (board.stderr) outputBlocks.push(`stderr\n${board.stderr}`);
        if (board.stdout) outputBlocks.push(`stdout\n${board.stdout}`);
        const output = outputBlocks.join('\n\n');
        return `
          <div class="status-item">
            <div class="status-top">
              <div>
                <strong>${board.name}</strong>
                <div class="mini">${board.host}</div>
              </div>
              <span class="badge ${board.status}">${board.status}</span>
            </div>
            <div class="mini" style="margin-top:10px;">${details.join(' | ')}</div>
            ${logText ? `<div class="log-title">执行日志</div><pre>${escapeHtml(logText)}</pre>` : ''}
            ${output ? `<div class="log-title">最终输出</div><pre>${escapeHtml(output)}</pre>` : ''}
          </div>`;
      }).join('');
    }

    function escapeHtml(text) {
      return text
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    }

    async function pollJob(jobId) {
      const response = await fetch(`/api/jobs/${jobId}`);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || '读取任务状态失败');
      }
      renderStatus(payload);
      if (payload.status === 'running') {
        pollTimer = setTimeout(() => pollJob(jobId), 2000);
      } else {
        pollTimer = null;
      }
    }

    async function startJob() {
      submitNoteNode.textContent = '正在提交任务...';
      const response = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectPayload()),
      });
      const payload = await response.json();
      if (!response.ok) {
        submitNoteNode.textContent = payload.error || '任务提交失败';
        return;
      }
      submitNoteNode.textContent = '任务已启动，正在轮询状态。';
      if (pollTimer) {
        clearTimeout(pollTimer);
      }
      await pollJob(payload.job_id);
    }

    async function startSingleBoardJob(card, action) {
      const board = collectBoard(card);
      setBoardNote(card, '正在提交单板任务...');
      const response = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...collectBasePayload(),
          run_validation: action === 'validation',
          run_auto: action === 'auto',
          boards: [{ ...board, enabled: true }],
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setBoardNote(card, payload.error || '单板任务提交失败');
        return;
      }
      setBoardNote(card, `任务已提交: ${payload.job_id}`);
      submitNoteNode.textContent = `已提交 ${board.name} 的${action === 'validation' ? ' ValidationExecutionConfig' : ' Auto ENV'}任务。`;
      if (pollTimer) {
        clearTimeout(pollTimer);
      }
      await pollJob(payload.job_id);
    }

    document.getElementById('run-btn').addEventListener('click', () => {
      startJob().catch((error) => {
        submitNoteNode.textContent = error.message;
      });
    });

    document.getElementById('fill-btn').addEventListener('click', () => {
      boardCount = 13;
      renderBoards();
      submitNoteNode.textContent = `${boardCount} 个板卡输入框已重置。`;
    });

    document.getElementById('add-btn').addEventListener('click', () => {
      addBoard();
    });

    document.getElementById('remove-btn').addEventListener('click', () => {
      removeLastBoard();
    });

    boardContainer.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) {
        return;
      }
      const action = button.dataset.action;
      const card = button.closest('.board-card');
      if (action === 'remove') {
        // remove this specific card
        const idx = Number(card.dataset.boardIndex);
        card.remove();
        // renumber remaining cards' data-board-index and titles
        [...boardContainer.querySelectorAll('.board-card')].forEach((c, i) => {
          const newIndex = i + 1;
          c.dataset.boardIndex = newIndex;
          const strong = c.querySelector('.board-head strong');
          if (strong) strong.textContent = `Board ${newIndex}`;
        });
        boardCount = Math.max(0, boardContainer.querySelectorAll('.board-card').length);
        submitNoteNode.textContent = `已删除 ${idx} 号板卡`; 
        updateCountsDisplay();
        return;
      }
      startSingleBoardJob(card, action).catch((error) => {
        setBoardNote(card, error.message);
      });
    });

    renderBoards();
  </script>
</body>
</html>
"""


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "WindowsEnvSetup/1.0"

    def do_GET(self):
        if self.path == "/":
            self._write_html(PAGE_HTML)
            return

        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = JOB_STORE.get(job_id)
            if not job:
                self._write_json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
                return
            self._write_json(job)
            return

        self._write_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path != "/api/jobs":
            self._write_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body.decode("utf-8"))
            boards, options = validate_payload(payload)
            job_id = JOB_STORE.create([sanitize_board_view(board) for board in boards], options)
            worker = threading.Thread(target=execute_job, args=(job_id, boards), daemon=True)
            worker.start()
            self._write_json({"job_id": job_id}, HTTPStatus.ACCEPTED)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._write_json({"error": f"Unexpected server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format_text, *args):
        return

    def _write_html(self, content, status=HTTPStatus.OK):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    host = "0.0.0.0"
    port = 8765
    server = ThreadingHTTPServer((host, port), RequestHandler)
    local_ip = get_preferred_local_ip()
    print(f"Windows env setup UI running at http://127.0.0.1:{port}")
    print(f"LAN access: http://{local_ip}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()