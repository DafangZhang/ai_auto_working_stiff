#!/usr/bin/env python3
"""
Worker - 自主 AI Agent 循环执行器
严格顺序：Designer → Reviewer → Coder → Validator
"""

import json
import sys
import subprocess
import time
import os
import signal
import argparse
import atexit
import fcntl
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

# 配置
MAX_ITERATIONS = 100
AGENT_TIMEOUT = 30 * 60  # 30 分钟
POLL_INTERVAL = 5  # 5 秒轮询

# 目录配置。默认兼容旧的 worker/ 工件目录；UI 会通过 configure_paths 切到目标项目 .ai-worker。
WORKER_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = WORKER_DIR.parent
STORIES_DIR = WORKER_DIR / "stories"
AGENTS_DIR = WORKER_DIR / "agents"
LOGS_DIR = WORKER_DIR / "logs"
SHARED_DIR = WORKER_DIR / "shared"
WORKER_STATE_FILE = WORKER_DIR / "worker_state.json"
ARTIFACT_ROOT = WORKER_DIR
RUNNER_LOCK_FILE = ".runner.lock"
RUNNER_LOCK_HANDLE = None
CURRENT_AGENT_PROCESS = None

CANONICAL_PHASES = {
    "pending",
    "designed",
    "design_reviewed",
    "coding",
    "coding_complete",
    "done",
}

PHASE_ALIASES = {
    "design": "designed",
    "design_review": "design_reviewed",
    "validation": "coding_complete",
    "validation_failed": "coding",
    "coding_blocked": "coding",
}

# 确保目录存在
for dir_path in [STORIES_DIR, LOGS_DIR, SHARED_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)


def configure_paths(project_root: Path = None, artifact_root: Path = None):
    """配置 Runner 使用的项目目录和工件目录。"""
    global PROJECT_ROOT, STORIES_DIR, LOGS_DIR, SHARED_DIR, WORKER_STATE_FILE, ARTIFACT_ROOT

    if project_root is not None:
        PROJECT_ROOT = Path(project_root).resolve()
    if artifact_root is not None:
        ARTIFACT_ROOT = Path(artifact_root).resolve()
    else:
        ARTIFACT_ROOT = WORKER_DIR

    STORIES_DIR = ARTIFACT_ROOT / "stories"
    LOGS_DIR = ARTIFACT_ROOT / "logs"
    SHARED_DIR = ARTIFACT_ROOT / "shared"
    WORKER_STATE_FILE = ARTIFACT_ROOT / "worker_state.json"

    for dir_path in [STORIES_DIR, LOGS_DIR, SHARED_DIR]:
        dir_path.mkdir(parents=True, exist_ok=True)


def acquire_runner_lock():
    """同一个工件目录只允许一个 Runner 实例运行。"""
    global RUNNER_LOCK_HANDLE

    lock_path = ARTIFACT_ROOT / RUNNER_LOCK_FILE
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip()
        handle.close()
        raise RuntimeError(f"已有 Runner 正在使用工件目录: {ARTIFACT_ROOT} ({owner or 'owner unknown'})")

    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({
        "pid": os.getpid(),
        "startedAt": datetime.now().isoformat(),
        "projectRoot": str(PROJECT_ROOT),
        "artifactRoot": str(ARTIFACT_ROOT),
    }, ensure_ascii=False))
    handle.flush()
    RUNNER_LOCK_HANDLE = handle


def release_runner_lock():
    """释放 Runner 锁文件。"""
    global RUNNER_LOCK_HANDLE

    if not RUNNER_LOCK_HANDLE:
        return

    try:
        RUNNER_LOCK_HANDLE.seek(0)
        RUNNER_LOCK_HANDLE.truncate()
        fcntl.flock(RUNNER_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        RUNNER_LOCK_HANDLE.close()
    except Exception:
        pass
    RUNNER_LOCK_HANDLE = None


def terminate_agent_process(grace_seconds: float = 5.0):
    """回收当前 Agent 子进程，避免留下残留的 script/claude。"""
    global CURRENT_AGENT_PROCESS

    process = CURRENT_AGENT_PROCESS
    if process is None:
        return
    if process.poll() is not None:
        CURRENT_AGENT_PROCESS = None
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        CURRENT_AGENT_PROCESS = None
        return
    except Exception:
        try:
            process.terminate()
        except Exception:
            CURRENT_AGENT_PROCESS = None
            return

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            CURRENT_AGENT_PROCESS = None
            return
        time.sleep(0.1)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass

    try:
        process.wait(timeout=1)
    except Exception:
        pass
    CURRENT_AGENT_PROCESS = None


def read_worker_state() -> Dict[str, Any]:
    """读取 worker 全局状态"""
    if not WORKER_STATE_FILE.exists():
        return {
            "currentStoryId": None,
            "currentPhase": None,
            "currentAgent": None,
            "startedAt": None,
            "iteration": 0,
            "totalStories": 0,
            "completedStories": 0,
            "lastUpdate": None
        }
    try:
        return json.loads(WORKER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"读取 worker_state.json 失败: {e}", "ERROR")
        return {}


def write_worker_state(state: Dict[str, Any]):
    """写入 worker 全局状态"""
    state["lastUpdate"] = datetime.now().isoformat()
    try:
        WORKER_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"写入 worker_state.json 失败: {e}", "ERROR")


def update_worker_state(story_id: str = None, phase: str = None, agent: str = None):
    """更新 worker 状态"""
    state = read_worker_state()
    if story_id is not None:
        state["currentStoryId"] = story_id
    if phase is not None:
        state["currentPhase"] = phase
    if agent is not None:
        state["currentAgent"] = agent
    write_worker_state(state)


def log(message: str, level: str = "INFO"):
    """打印并记录日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] {message}"
    print(log_line)

    # 写入日志文件
    log_file = LOGS_DIR / f"worker-{datetime.now().strftime('%Y%m%d')}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")


def get_story_dirs() -> List[Path]:
    """获取所有 story 目录"""
    if not STORIES_DIR.exists():
        return []
    return sorted([d for d in STORIES_DIR.iterdir() if d.is_dir()])


def read_status(story_dir: Path) -> Optional[Dict[str, Any]]:
    """读取 story 的状态文件"""
    status_file = story_dir / "status.json"
    if not status_file.exists():
        return None
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"读取 status.json 失败: {story_dir.name} - {e}", "ERROR")
        return None


def write_status(story_dir: Path, status: Dict[str, Any]):
    """写入 story 的状态文件"""
    status_file = story_dir / "status.json"
    status["updatedAt"] = datetime.now().isoformat()
    try:
        status_file.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"写入 status.json 失败: {story_dir.name} - {e}", "ERROR")


def read_session_step() -> Optional[str]:
    """读取 Web UI 会话步骤。没有 session.json 时视为兼容旧模式。"""
    session_file = ARTIFACT_ROOT / "session.json"
    if not session_file.exists():
        return None
    try:
        session = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"读取 session.json 失败: {e}", "WARNING")
        return None
    step = session.get("step")
    return str(step) if step else None


def get_current_phase(status: Dict[str, Any], story_dir: Optional[Path] = None) -> str:
    """获取并归一化当前阶段。"""
    phase = status.get("phase", "pending")
    if phase in CANONICAL_PHASES:
        return phase
    if phase in PHASE_ALIASES:
        return PHASE_ALIASES[phase]
    if phase == "design_blocked":
        design_status = status.get("phases", {}).get("design", {}).get("status")
        if design_status == "completed" or (story_dir and (story_dir / "design-v1.md").exists()):
            return "designed"
        return "pending"
    return phase


def normalize_status_phase(story_dir: Path, status: Dict[str, Any]) -> str:
    """把历史/错误 phase 名称修正为 runner 使用的规范值。"""
    raw_phase = status.get("phase", "pending")
    phase = get_current_phase(status, story_dir)
    if phase != raw_phase and phase in CANONICAL_PHASES:
        status["phase"] = phase
        write_status(story_dir, status)
        log(f"归一化 Story 阶段: {story_dir.name} {raw_phase} -> {phase}", "WARNING")
    return phase


def find_next_story() -> Optional[Path]:
    """
    找到下一个需要处理的 story
    按 story ID 顺序执行，一个 story 完成后才处理下一个
    """
    stories = get_story_dirs()

    for story_dir in stories:
        status = read_status(story_dir)
        if not status:
            continue

        phase = normalize_status_phase(story_dir, status)
        retry_count = status.get("retryCount", 0)
        max_retries = status.get("maxRetries", 20)

        # 跳过已完成的
        if phase == "done":
            continue

        # 跳过超过重试次数的
        if retry_count >= max_retries:
            continue

        # 检查依赖
        dependencies = status.get("dependencies", [])
        deps_satisfied = True
        for dep_id in dependencies:
            dep_dir = STORIES_DIR / dep_id
            dep_status = read_status(dep_dir)
            if not dep_status or get_current_phase(dep_status, dep_dir) != "done":
                deps_satisfied = False
                break

        if not deps_satisfied:
            continue

        # 找到第一个未完成的 story，按 ID 顺序处理
        return story_dir

    return None


def all_stories_done() -> bool:
    """检查是否所有 story 都已完成"""
    stories = get_story_dirs()
    if not stories:
        return False

    for story_dir in stories:
        status = read_status(story_dir)
        if not status:
            return False
        if get_current_phase(status, story_dir) != "done":
            return False

    return True


def build_agent_command(agent_type: str, story_dir: Path) -> List[str]:
    """构建 Agent 命令"""
    agent_file = AGENTS_DIR / f"{agent_type}.md"
    if not agent_file.exists():
        raise FileNotFoundError(f"Agent 文件不存在: {agent_file}")

    # 读取 agent 指令
    prompt = agent_file.read_text(encoding="utf-8")

    # 替换变量
    story_id = story_dir.name
    prompt = prompt.replace("{story-id}", story_id)
    prompt = prompt.replace("{project_root}", str(PROJECT_ROOT))
    prompt = prompt.replace("{artifact_root}", str(ARTIFACT_ROOT))
    prompt = prompt.replace("{story_dir}", str(story_dir))
    prompt = prompt.replace("{worker_state_file}", str(WORKER_STATE_FILE))
    prompt = prompt.replace("{reviewed_prd_file}", str(SHARED_DIR / "prd.md"))
    prompt = prompt.replace("{project_summary_file}", str(ARTIFACT_ROOT / "context" / "project-summary.md"))
    prompt = prompt.replace("{shared_prd_file}", str(SHARED_DIR / "prd.md"))
    prompt = prompt.replace("{shared_mvp_dir}", str(SHARED_DIR / "mvp"))
    prompt = prompt.replace("{shared_agents_file}", str(SHARED_DIR / "AGENTS.md"))

    # 构建 claude 命令
    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        prompt,
    ]

    return cmd


def run_agent(agent_type: str, story_dir: Path) -> bool:
    """
    运行 Agent
    返回: 是否成功完成
    """
    global CURRENT_AGENT_PROCESS

    story_id = story_dir.name
    log(f"启动 {agent_type.upper()} Agent - Story: {story_id}")

    # 更新全局状态文件，让 Agent 知道当前任务
    status = read_status(story_dir)
    phase = normalize_status_phase(story_dir, status) if status else "unknown"
    update_worker_state(story_id=story_id, phase=phase, agent=agent_type)
    log(f"已更新 worker_state.json: story={story_id}, phase={phase}, agent={agent_type}")

    # 记录开始时间
    start_time = time.time()

    # 构建命令
    try:
        cmd = build_agent_command(agent_type, story_dir)
    except FileNotFoundError as e:
        log(f"错误: {e}", "ERROR")
        return False

    # 创建日志文件
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{agent_type}-{story_id}.log"

    # 使用 script 提供 PTY
    script_cmd = ["script", "-q", "/dev/null"] + cmd

    try:
        # 启动进程
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"=== {agent_type.upper()} Agent - {story_id} ===\n")
            f.write(f"Started at: {datetime.now().isoformat()}\n")
            f.write(f"Command: {' '.join(cmd)}\n")
            f.write("=" * 50 + "\n\n")

        process = subprocess.Popen(
            script_cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        CURRENT_AGENT_PROCESS = process

        # 实时读取输出
        with open(log_file, "a", encoding="utf-8") as f:
            while True:
                # 检查是否超时
                elapsed = time.time() - start_time
                if elapsed > AGENT_TIMEOUT:
                    log(f"{agent_type.upper()} Agent 超时 ({AGENT_TIMEOUT}s)", "WARNING")
                    terminate_agent_process()
                    f.write(f"\n\n[TIMEOUT] Agent timed out after {AGENT_TIMEOUT}s\n")
                    return False

                # 检查进程是否结束
                ret_code = process.poll()
                if ret_code is not None:
                    # 读取剩余输出
                    remaining = process.stdout.read()
                    if remaining:
                        f.write(remaining)
                        print(remaining, end="")

                    f.write(f"\n\n[EXIT] Agent exited with code {ret_code}\n")
                    f.write(f"Finished at: {datetime.now().isoformat()}\n")

                    if ret_code == 0:
                        log(f"{agent_type.upper()} Agent 完成 - Story: {story_id}")
                        CURRENT_AGENT_PROCESS = None
                        return True
                    else:
                        log(f"{agent_type.upper()} Agent 失败 (exit code: {ret_code})", "ERROR")
                        CURRENT_AGENT_PROCESS = None
                        return False

                # 读取输出
                try:
                    import select
                    ready, _, _ = select.select([process.stdout], [], [], 1.0)
                    if ready:
                        line = process.stdout.readline()
                        if line:
                            f.write(line)
                            f.flush()
                            print(line, end="")
                except:
                    time.sleep(1)

    except Exception as e:
        log(f"运行 Agent 时出错: {e}", "ERROR")
        CURRENT_AGENT_PROCESS = None
        return False


def run_designer(story_dir: Path) -> bool:
    """运行 Designer Agent"""
    return run_agent("designer", story_dir)


def run_reviewer(story_dir: Path) -> bool:
    """运行 Reviewer Agent"""
    return run_agent("reviewer", story_dir)


def run_coder(story_dir: Path) -> bool:
    """运行 Coder Agent"""
    return run_agent("coder", story_dir)


def run_validator(story_dir: Path) -> bool:
    """运行 Validator Agent"""
    return run_agent("validator", story_dir)


def process_story(story_dir: Path) -> bool:
    """
    处理一个 story
    根据当前阶段调用对应的 Agent
    """
    status = read_status(story_dir)
    if not status:
        log(f"无法读取 story 状态: {story_dir.name}", "ERROR")
        return False

    phase = normalize_status_phase(story_dir, status)
    story_id = story_dir.name

    log(f"处理 Story: {story_id}, 当前阶段: {phase}")

    # 根据阶段调用对应的 Agent
    if phase == "pending":
        return run_designer(story_dir)

    elif phase == "designed":
        return run_reviewer(story_dir)

    elif phase in ["design_reviewed", "coding"]:
        return run_coder(story_dir)

    elif phase == "coding_complete":
        return run_validator(story_dir)

    elif phase == "done":
        log(f"Story {story_id} 已完成")
        return True

    else:
        log(f"未知阶段: {phase}", "ERROR")
        return False


def signal_handler(signum, frame):
    """信号处理"""
    log("收到中断信号，正在退出...", "WARNING")
    terminate_agent_process()
    sys.exit(0)


def run_worker(max_iterations: int = MAX_ITERATIONS, start_ui: bool = False, open_browser: bool = False) -> int:
    """运行 Story 执行循环。返回进程退出码。"""
    try:
        acquire_runner_lock()
    except RuntimeError as e:
        log(str(e), "ERROR")
        return 1

    log("=" * 60)
    log("Worker 启动")
    log(f"项目目录: {PROJECT_ROOT}")
    log(f"工件目录: {ARTIFACT_ROOT}")
    log(f"Stories 目录: {STORIES_DIR}")
    log(f"最大迭代次数: {max_iterations}")
    log(f"Agent 超时: {AGENT_TIMEOUT}s")
    log("=" * 60)

    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if start_ui:
        try:
            # Prefer local import so the runner works both from VS Code cwd=new_code/worker
            # and from repository-root launches where `new_code` is importable.
            try:
                from ui import server as ui_server
            except ImportError:
                from new_code.worker.ui import server as ui_server
            ui_server.start(port=7332, open_browser=open_browser)
            log("UI 服务器启动: http://localhost:7332")
        except Exception as e:
            log(f"UI 服务器启动失败: {e}", "WARNING")

    # 主循环
    for iteration in range(1, max_iterations + 1):
        log(f"\n--- 迭代 {iteration}/{max_iterations} ---")

        session_step = read_session_step()
        if session_step not in {None, "running", "completed"}:
            log(f"当前会话步骤为 {session_step}，Runner 暂停执行，等待 UI 开始运行。")
            time.sleep(POLL_INTERVAL)
            continue

        # 检查是否全部完成
        if all_stories_done():
            log("\n" + "=" * 60)
            log("所有 Story 已完成！")
            log("=" * 60)
            return 0

        # 找到下一个需要处理的 story
        story_dir = find_next_story()

        if not story_dir:
            log("没有可处理的 Story（可能都在等待依赖）")
            log(f"等待 {POLL_INTERVAL} 秒后重试...")
            time.sleep(POLL_INTERVAL)
            continue

        # 处理 story
        try:
            success = process_story(story_dir)
            if not success:
                log(f"处理 Story 失败: {story_dir.name}", "WARNING")
        except Exception as e:
            log(f"处理 Story 时出错: {story_dir.name} - {e}", "ERROR")

        # 短暂暂停，避免 CPU 占用过高
        time.sleep(2)

    else:
        log(f"\n达到最大迭代次数 ({max_iterations})，退出", "WARNING")
        return 1

    return 0


def main() -> int:
    """CLI 入口。返回进程退出码。"""
    parser = argparse.ArgumentParser(description="AI Worker runner")
    parser.add_argument("--project-root", default=None, help="目标项目根目录")
    parser.add_argument("--artifact-root", default=None, help="工件目录，默认使用 <project-root>/.ai-worker 或旧 worker 目录")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    parser.add_argument("--ui", action="store_true", help="同时启动 Web UI")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve() if args.project_root else PROJECT_ROOT
    if args.artifact_root:
        artifact_root = Path(args.artifact_root).resolve()
    elif args.project_root and (project_root / ".ai-worker").exists():
        artifact_root = project_root / ".ai-worker"
    else:
        artifact_root = ARTIFACT_ROOT

    configure_paths(project_root=project_root, artifact_root=artifact_root)
    return run_worker(max_iterations=args.max_iterations, start_ui=args.ui, open_browser=args.open_browser)


if __name__ == "__main__":
    atexit.register(release_runner_lock)
    exit_code = main()
    if sys.gettrace() is None:
        sys.exit(exit_code)
    if exit_code:
        log(f"调试模式下返回非零退出码: {exit_code}", "WARNING")
