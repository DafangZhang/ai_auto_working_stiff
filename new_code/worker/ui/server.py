#!/usr/bin/env python3
"""
Worker UI Server - 引导式项目接入、PRD 评审、Story 预览和运行监控 API。
"""

import cgi
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKER_DIR = Path(__file__).parent.parent.resolve()
PROJECT_ROOT = WORKER_DIR.parent
ACTIVE_SESSION_FILE = WORKER_DIR / "active_session.json"

SESSION_STEPS = {
    "select_project",
    "upload_prd",
    "analyzing_project",
    "awaiting_answers",
    "reviewing_prd",
    "awaiting_prd_confirmation",
    "generating_stories",
    "awaiting_start",
    "running",
    "completed",
    "error",
}

PHASE_LABELS = {
    "pending": "待设计",
    "designed": "设计审核",
    "design_reviewed": "开发中",
    "coding": "修复中",
    "coding_complete": "验证中",
    "done": "已完成",
}

STORY_SPLITTER_TIMEOUT = 30 * 60
TASK_HEARTBEAT_INTERVAL = 5


def now_iso() -> str:
    return datetime.now().isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def clear_active_session_file():
    if ACTIVE_SESSION_FILE.exists():
        ACTIVE_SESSION_FILE.unlink()


def tail_file(path: Path, max_lines: int = 160) -> List[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception:
        return []


def run_git(project_root: Path, args: List[str]) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def story_sort_key(story: Dict[str, Any]) -> tuple:
    story_id = (
        story.get("storyId")
        or story.get("status", {}).get("storyId")
        or story.get("id")
        or story.get("directory")
        or ""
    )
    match = re.search(r"US-(\d+)", str(story_id), flags=re.IGNORECASE)
    if match:
        return (0, int(match.group(1)), str(story_id))
    return (1, 0, str(story_id))


def sort_stories(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=story_sort_key)


def active_session_path() -> Optional[Path]:
    active = read_json(ACTIVE_SESSION_FILE, {})
    artifact_root = active.get("artifactRoot")
    if not artifact_root:
        return None
    path = Path(artifact_root) / "session.json"
    return path if path.exists() else None


def default_session() -> Dict[str, Any]:
    return {
        "projectRoot": "",
        "artifactRoot": "",
        "step": "select_project",
        "rawPrdPath": "",
        "reviewedPrdPath": "",
        "projectSummaryPath": "",
        "questions": [],
        "answers": {},
        "storiesPreview": [],
        "runStatus": {},
        "activity": {},
        "currentAgentLog": "",
        "runnerPid": None,
        "lastError": "",
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }


def load_session() -> Dict[str, Any]:
    session_path = active_session_path()
    if not session_path:
        return default_session()
    session = read_json(session_path, default_session())
    return augment_session(session)


def save_session(session: Dict[str, Any]) -> Dict[str, Any]:
    step = session.get("step", "select_project")
    if step not in SESSION_STEPS:
        session["step"] = "error"
        session["lastError"] = f"未知会话步骤: {step}"
    session["updatedAt"] = now_iso()
    artifact_root = Path(session["artifactRoot"])
    write_json(artifact_root / "session.json", session)
    write_json(ACTIVE_SESSION_FILE, {
        "projectRoot": session["projectRoot"],
        "artifactRoot": session["artifactRoot"],
        "updatedAt": session["updatedAt"],
    })
    return augment_session(session)


def load_saved_session(artifact_root: Path) -> Dict[str, Any]:
    session = read_json(artifact_root / "session.json", default_session())
    if not session.get("artifactRoot"):
        session["artifactRoot"] = str(artifact_root)
    return session


def set_activity(session: Dict[str, Any], kind: str, status: str, message: str, progress: Optional[int] = None, detail: str = ""):
    activity = {
        "kind": kind,
        "status": status,
        "message": message,
        "detail": detail,
        "updatedAt": now_iso(),
        "startedAt": session.get("activity", {}).get("startedAt") or now_iso(),
    }
    if progress is not None:
        activity["progress"] = max(0, min(100, int(progress)))
    if status in {"completed", "failed"}:
        activity["finishedAt"] = now_iso()
    session["activity"] = activity


def clear_activity(session: Dict[str, Any]):
    session["activity"] = {}


def artifact_paths(session: Dict[str, Any]) -> Dict[str, Path]:
    artifact_root = Path(session["artifactRoot"])
    return {
        "artifact": artifact_root,
        "context": artifact_root / "context",
        "shared": artifact_root / "shared",
        "stories": artifact_root / "stories",
        "logs": artifact_root / "logs",
        "screenshots": artifact_root / "screenshots",
        "worker_state": artifact_root / "worker_state.json",
    }


def ensure_artifact_tree(artifact_root: Path):
    for rel in ["context", "shared", "stories", "logs", "screenshots"]:
        (artifact_root / rel).mkdir(parents=True, exist_ok=True)
    (artifact_root / "shared" / "mvp").mkdir(parents=True, exist_ok=True)


def stop_runner(session: Dict[str, Any]):
    artifact_root = str(Path(session.get("artifactRoot") or "").resolve()) if session.get("artifactRoot") else ""
    candidate_pids: List[int] = []
    pid = session.get("runnerPid")
    if pid:
        candidate_pids.append(int(pid))
    candidate_pids.extend(find_runner_pids(artifact_root))

    seen = set()
    for candidate_pid in candidate_pids:
        if not candidate_pid or candidate_pid in seen or candidate_pid == os.getpid():
            continue
        seen.add(candidate_pid)
        terminate_pid(candidate_pid)


def find_runner_pids(artifact_root: str) -> List[int]:
    """查找绑定到当前工件目录的 worker 进程，兼容手动启动和 IDE 调试启动。"""
    if not artifact_root:
        return []
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []

    pids: List[int] = []
    for line in result.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = raw.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        if "worker.py" not in command:
            continue
        if artifact_root not in command:
            continue
        try:
            pids.append(int(pid_text))
        except ValueError:
            continue
    return pids


def terminate_pid(pid: int):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            return

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)

    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def get_all_stories(session: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if session is None:
        session = load_session()
    stories_dir = Path(session.get("artifactRoot") or WORKER_DIR) / "stories"
    stories = []
    if not stories_dir.exists():
        return stories

    for story_dir in stories_dir.iterdir():
        if not story_dir.is_dir():
            continue
        status = read_json(story_dir / "status.json", {})
        story_text = ""
        if (story_dir / "story.md").exists():
            story_text = (story_dir / "story.md").read_text(encoding="utf-8", errors="replace")
        stories.append({
            "id": story_dir.name,
            "status": status,
            "story": story_text[:500] + ("..." if len(story_text) > 500 else ""),
            "hasDesignV1": (story_dir / "design-v1.md").exists(),
            "hasDesignV2": (story_dir / "design-v2.md").exists(),
            "hasImplementation": (story_dir / "implementation.md").exists(),
            "hasTestReport": (story_dir / "test-report.md").exists(),
        })
    return sort_stories(stories)


def get_story_detail(story_id: str, session: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if session is None:
        session = load_session()
    story_dir = Path(session.get("artifactRoot") or WORKER_DIR) / "stories" / story_id
    if not story_dir.exists():
        return None

    detail = {"id": story_id, "status": read_json(story_dir / "status.json", {})}
    for key, filename in {
        "story": "story.md",
        "designV1": "design-v1.md",
        "designV2": "design-v2.md",
        "implementation": "implementation.md",
        "testReport": "test-report.md",
    }.items():
        path = story_dir / filename
        detail[key] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return detail


def summarize_tree(project_root: Path) -> List[str]:
    ignored = {".git", ".ai-worker", "node_modules", "vendor", "dist", "build", "__pycache__"}
    lines: List[str] = []
    for root, dirs, files in os.walk(project_root):
        rel_root = Path(root).relative_to(project_root)
        depth = 0 if str(rel_root) == "." else len(rel_root.parts)
        if depth > 2:
            dirs[:] = []
            continue
        dirs[:] = sorted([d for d in dirs if d not in ignored])[:12]
        for name in sorted(files)[:16]:
            rel = (rel_root / name) if str(rel_root) != "." else Path(name)
            lines.append(str(rel))
            if len(lines) >= 120:
                return lines
    return lines


def read_key_files(project_root: Path) -> Dict[str, str]:
    patterns = [
        "README.md", "README", "package.json", "go.mod", "pyproject.toml",
        "Cargo.toml", "Makefile", "AGENTS.md", "CONTRIBUTING.md",
    ]
    docs = list((project_root / "docs").glob("*.md"))[:5] if (project_root / "docs").exists() else []
    result: Dict[str, str] = {}
    for rel in patterns:
        path = project_root / rel
        if path.exists() and path.is_file():
            result[rel] = path.read_text(encoding="utf-8", errors="replace")[:4000]
    for path in docs:
        result[str(path.relative_to(project_root))] = path.read_text(encoding="utf-8", errors="replace")[:3000]
    return result


def detect_commands(key_files: Dict[str, str]) -> List[str]:
    commands = []
    if "package.json" in key_files:
        commands.extend(["npm run typecheck", "npm run lint", "npm run test", "npm run build"])
    if "go.mod" in key_files:
        commands.append("go test ./...")
    if "pyproject.toml" in key_files:
        commands.append("python -m pytest")
    if "Cargo.toml" in key_files:
        commands.extend(["cargo test", "cargo build"])
    return commands


def question(question_id: str, title: str, text: str, why: str, placeholder: str, required: bool = True) -> Dict[str, Any]:
    return {
        "id": question_id,
        "title": title,
        "question": text,
        "whyItMatters": why,
        "answerType": "text",
        "placeholder": placeholder,
        "required": required,
    }


def analyze_project(session: Dict[str, Any]) -> Dict[str, Any]:
    project_root = Path(session["projectRoot"])
    paths = artifact_paths(session)
    raw_prd = Path(session["rawPrdPath"]).read_text(encoding="utf-8", errors="replace") if session.get("rawPrdPath") else ""
    set_activity(session, "analyze_project", "running", "正在扫描项目结构和关键文件...", 15, "会检查目录结构、关键清单文件和 Git 状态。")
    save_session(session)

    tree = summarize_tree(project_root)
    key_files = read_key_files(project_root)
    git_branch = run_git(project_root, ["branch", "--show-current"]) or "unavailable"
    git_status = run_git(project_root, ["status", "--short"]) or ""
    git_log = run_git(project_root, ["log", "-5", "--oneline"]) or ""
    set_activity(session, "analyze_project", "running", "正在提炼项目约束和待确认问题...", 60, "正在根据项目上下文和 PRD 生成后续问题。")
    save_session(session)
    commands = detect_commands(key_files)
    risks = []
    questions = []

    if git_status:
        risks.append("项目存在未提交变更，执行开发前需要确认这些变更是否属于当前上下文。")
        questions.append(question(
            "git_uncommitted_scope",
            "未提交变更范围",
            "当前未提交变更是否应被视为本次开发的基础？",
            "Coder 和 Validator 会在现有工作树上继续修改，错误理解未提交变更会影响实现边界。",
            "例如：全部纳入 / 只纳入某几个文件 / 与本次无关",
        ))
    if "AGENTS.md" not in key_files:
        questions.append(question(
            "architecture_rules",
            "项目规则",
            "这个项目有哪些必须遵守的架构、命名、测试或提交规则？",
            "没有 AGENTS.md 时，Agent 需要用户补充关键约束，避免按错误模式开发。",
            "例如：后端分层规则、前端组件目录、必须运行的测试命令",
        ))
    if len(raw_prd.strip()) < 300:
        questions.append(question(
            "prd_scope",
            "需求范围",
            "PRD 内容较短，请补充本次开发明确包含和不包含的范围。",
            "Story 拆分依赖清晰的验收边界，范围不足会导致 Story 粒度不稳定。",
            "例如：本次只做 UI / 需要后端 API / 不做鉴权",
        ))

    analysis = {
        "summary": {
            "projectRoot": str(project_root),
            "tree": tree,
            "keyFiles": sorted(key_files.keys()),
            "git": {
                "branch": git_branch,
                "status": git_status,
                "recentCommits": git_log,
            },
        },
        "commands": commands,
        "risks": risks,
        "questions": questions,
    }

    summary_md = render_project_summary(analysis, key_files)
    paths["context"].mkdir(parents=True, exist_ok=True)
    write_json(paths["context"] / "project-analysis.json", analysis)
    write_json(paths["context"] / "questions.json", questions)
    (paths["context"] / "project-summary.md").write_text(summary_md, encoding="utf-8")

    session["projectSummaryPath"] = str(paths["context"] / "project-summary.md")
    session["questions"] = questions
    session["analysis"] = analysis
    if questions:
        session["step"] = "awaiting_answers"
        set_activity(session, "analyze_project", "completed", "项目分析完成，已生成待确认问题。", 100)
        return save_session(session)
    session["step"] = "reviewing_prd"
    set_activity(session, "analyze_project", "completed", "项目分析完成，正在进入 PRD 评审。", 100)
    save_session(session)
    return review_prd(session)


def render_project_summary(analysis: Dict[str, Any], key_files: Dict[str, str]) -> str:
    summary = analysis["summary"]
    git_info = summary["git"]
    lines = [
        "# Project Summary",
        "",
        f"- Project root: `{summary['projectRoot']}`",
        f"- Git branch: `{git_info['branch']}`",
        f"- Key files: {', '.join(summary['keyFiles']) or 'None detected'}",
        "",
        "## Suggested Commands",
    ]
    lines.extend([f"- `{cmd}`" for cmd in analysis["commands"]] or ["- No standard commands detected"])
    lines.extend(["", "## Git Status", "```text", git_info["status"] or "clean or unavailable", "```"])
    lines.extend(["", "## Recent Commits", "```text", git_info["recentCommits"] or "unavailable", "```"])
    lines.extend(["", "## File Tree Snapshot", "```text"])
    lines.extend(summary["tree"][:120])
    lines.extend(["```", "", "## Key Context Files"])
    for name, content in key_files.items():
        lines.extend(["", f"### {name}", "```text", content[:1600], "```"])
    if analysis["risks"]:
        lines.extend(["", "## Risks"])
        lines.extend([f"- {risk}" for risk in analysis["risks"]])
    return "\n".join(lines) + "\n"


def review_prd(session: Dict[str, Any]) -> Dict[str, Any]:
    paths = artifact_paths(session)
    answers = session.get("answers", {})
    remaining = [
        q for q in session.get("questions", [])
        if q.get("required") and not str(answers.get(q["id"], "")).strip()
    ]
    if remaining:
        session["questions"] = remaining
        session["step"] = "awaiting_answers"
        return save_session(session)

    raw_prd = Path(session["rawPrdPath"]).read_text(encoding="utf-8", errors="replace")
    project_summary = Path(session["projectSummaryPath"]).read_text(encoding="utf-8", errors="replace") if session.get("projectSummaryPath") else ""
    reviewed = [
        "# Reviewed PRD",
        "",
        "## Source PRD",
        "",
        raw_prd.strip(),
        "",
        "## Project Context Decisions",
        "",
    ]
    if answers:
        reviewed.extend([f"- **{key}**: {value}" for key, value in answers.items()])
    else:
        reviewed.append("- No extra user answers were required.")
    reviewed.extend([
        "",
        "## Implementation Context",
        "",
        "The following project summary was generated before Story planning and should guide implementation decisions.",
        "",
        project_summary[:5000],
    ])

    reviewed_path = paths["context"] / "reviewed-prd.md"
    reviewed_path.write_text("\n".join(reviewed).strip() + "\n", encoding="utf-8")
    session["reviewedPrdPath"] = str(reviewed_path)
    session["questions"] = []
    session["step"] = "awaiting_prd_confirmation"
    clear_activity(session)
    return save_session(session)


def slugify(value: str) -> str:
    value = re.sub(r"[^\w\s-]", "", value.lower(), flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "-", value).strip("-")
    return value or "story"


def extract_story_sections(markdown: str) -> List[Dict[str, Any]]:
    pattern = re.compile(r"(?m)^#{2,4}\s*(US-\d{3})\s*[:：-]?\s*(.+)$")
    matches = list(pattern.finditer(markdown))
    stories = []
    if matches:
        for idx, match in enumerate(matches):
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
            body = markdown[start:end].strip()
            acceptance = re.findall(r"(?im)^\s*[-*]\s*(?:\[[ xX]\]\s*)?(.+)$", body)
            stories.append({
                "storyId": match.group(1),
                "title": match.group(2).strip(),
                "description": body[:600] or match.group(2).strip(),
                "acceptanceCriteria": acceptance[:8] or ["实现内容符合 Reviewed PRD", "本地验证命令通过"],
                "dependencies": [],
            })
    if not stories:
        title = "实现 Reviewed PRD"
        first_heading = re.search(r"(?m)^#\s+(.+)$", markdown)
        if first_heading:
            title = first_heading.group(1).strip()
        stories.append({
            "storyId": "US-001",
            "title": title,
            "description": markdown[:900].strip(),
            "acceptanceCriteria": ["实现内容符合 Reviewed PRD", "本地验证命令通过"],
            "dependencies": [],
        })
    return stories


def ensure_clean_path(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def materialize_shared_assets(session: Dict[str, Any]):
    project_root = Path(session["projectRoot"])
    paths = artifact_paths(session)
    shared_dir = paths["shared"]
    shared_dir.mkdir(parents=True, exist_ok=True)

    reviewed_path = Path(session["reviewedPrdPath"])
    if reviewed_path.exists():
        shutil.copyfile(reviewed_path, shared_dir / "prd.md")

    mvp_target = project_root / "mvp"
    shared_mvp = shared_dir / "mvp"
    ensure_clean_path(shared_mvp)
    if mvp_target.exists() and mvp_target.is_dir():
        shared_mvp.symlink_to(mvp_target, target_is_directory=True)
    else:
        shared_mvp.mkdir(parents=True, exist_ok=True)

    project_agents = project_root / "AGENTS.md"
    shared_agents = shared_dir / "AGENTS.md"
    ensure_clean_path(shared_agents)
    if project_agents.exists() and project_agents.is_file():
        shared_agents.symlink_to(project_agents)
    else:
        summary_text = ""
        summary_path = Path(session.get("projectSummaryPath", ""))
        if summary_path.exists():
            summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
        shared_agents.write_text(render_generated_agents(session, summary_text), encoding="utf-8")


def render_generated_agents(session: Dict[str, Any], summary_text: str) -> str:
    commands = session.get("analysis", {}).get("commands") if isinstance(session.get("analysis"), dict) else None
    command_lines = "\n".join([f"- `{cmd}`" for cmd in (commands or [])]) or "- 参考项目原有命令"
    return f"""# AGENTS.md

该文件由 AI Worker 基于项目扫描结果自动生成，供执行阶段的 Designer / Reviewer / Coder / Validator 使用。

## 项目概览

- 项目根目录：`{session.get('projectRoot', '')}`
- 工件目录：`{session.get('artifactRoot', '')}`

## 命令

{command_lines}

## 项目补充摘要

{summary_text[:6000]}
"""


def parse_claude_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("story-splitter 没有返回内容。")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\})", text)
        if not match:
            raise
        return json.loads(match.group(1))


def parse_section(markdown: str, heading: str) -> str:
    pattern = rf"(?ims)^##\s*{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    match = re.search(pattern, markdown)
    return match.group(1).strip() if match else ""


def parse_bullet_lines(text: str) -> List[str]:
    items = []
    for line in text.splitlines():
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if match:
            items.append(match.group(1).strip())
    return items


def story_preview_from_dir(story_dir: Path) -> Dict[str, Any]:
    status = read_json(story_dir / "status.json", {})
    markdown = (story_dir / "story.md").read_text(encoding="utf-8", errors="replace") if (story_dir / "story.md").exists() else ""
    title_line = re.search(r"(?m)^#\s+([^:]+):\s*(.+)$", markdown)
    story_id = status.get("storyId") or (title_line.group(1).strip() if title_line else story_dir.name)
    title = status.get("title") or (title_line.group(2).strip() if title_line else story_dir.name)
    description = parse_section(markdown, "描述") or parse_section(markdown, "Description")
    implementation_tasks = parse_bullet_lines(parse_section(markdown, "实现任务") or parse_section(markdown, "Implementation Tasks"))
    acceptance_criteria = parse_bullet_lines(parse_section(markdown, "验收标准") or parse_section(markdown, "Acceptance Criteria"))
    dependencies = status.get("dependencies", [])
    if not dependencies:
        dep_text = parse_section(markdown, "依赖") or parse_section(markdown, "Dependencies")
        if dep_text and dep_text not in {"[]", "无", "none", "None"}:
            dependencies = [item.strip() for item in re.split(r"[,，\n]+", dep_text) if item.strip()]
    priority = status.get("priority", 1)
    priority_text = parse_section(markdown, "优先级") or parse_section(markdown, "Priority")
    if priority_text:
        match = re.search(r"\d+", priority_text)
        if match:
            priority = int(match.group(0))
    return {
        "storyId": story_id,
        "title": title,
        "description": description or title,
        "implementationTasks": implementation_tasks,
        "acceptanceCriteria": acceptance_criteria,
        "dependencies": dependencies,
        "priority": priority,
        "directory": story_dir.name,
    }


def build_story_previews_from_disk(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    paths = artifact_paths(session)
    if not paths["stories"].exists():
        return []

    previews: List[Dict[str, Any]] = []
    for story_dir in [item for item in paths["stories"].iterdir() if item.is_dir()]:
        status_file = story_dir / "status.json"
        if not status_file.exists():
            continue
        try:
            previews.append(story_preview_from_dir(story_dir))
        except Exception:
            status = read_json(status_file, {})
            previews.append({
                "storyId": status.get("storyId", story_dir.name),
                "title": status.get("title", story_dir.name),
                "description": status.get("title", story_dir.name),
                "implementationTasks": [],
                "acceptanceCriteria": [],
                "dependencies": status.get("dependencies", []),
                "priority": status.get("priority", 999),
                "directory": story_dir.name,
            })
    return sort_stories(previews)


def story_splitter_completed(paths: Dict[str, Path]) -> bool:
    log_path = paths["logs"] / "story-splitter.log"
    if not log_path.exists():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return "[EXIT] code=0" in text


def prepare_story_splitter_workspace(session: Dict[str, Any]) -> Path:
    paths = artifact_paths(session)
    compat_root = paths["artifact"] / "compat-root"
    compat_root.mkdir(parents=True, exist_ok=True)

    worker_link = compat_root / "worker"
    ensure_clean_path(worker_link)
    worker_link.symlink_to(paths["artifact"], target_is_directory=True)

    skill_dir = compat_root / ".claude" / "skills" / "story-splitter"
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROJECT_ROOT / ".claude" / "skills" / "story-splitter" / "SKILL.md", skill_dir / "SKILL.md")
    return compat_root


def valid_story_dirs(paths: Dict[str, Path]) -> List[Path]:
    if not paths["stories"].exists():
        return []
    dirs: List[Path] = []
    for item in sorted([entry for entry in paths["stories"].iterdir() if entry.is_dir()]):
        if (item / "story.md").exists() and (item / "status.json").exists():
            dirs.append(item)
    return dirs


def generate_structured_stories(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    reviewed_path = Path(session.get("reviewedPrdPath", ""))
    if not reviewed_path.exists():
        raise ValueError("Reviewed PRD 不存在，请先确认 PRD。")

    prd_text = reviewed_path.read_text(encoding="utf-8", errors="replace")
    project_summary = ""
    summary_path = Path(session.get("projectSummaryPath", ""))
    if summary_path.exists():
        project_summary = summary_path.read_text(encoding="utf-8", errors="replace")

    schema = json.dumps({
        "type": "object",
        "properties": {
            "stories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "storyId": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "implementationTasks": {"type": "array", "items": {"type": "string"}},
                        "acceptanceCriteria": {"type": "array", "items": {"type": "string"}},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "priority": {"type": "integer"}
                    },
                    "required": ["storyId", "title", "description", "implementationTasks", "acceptanceCriteria", "dependencies", "priority"]
                }
            }
        },
        "required": ["stories"]
    }, ensure_ascii=False)

    prompt = f"""
你是 AI Worker 的 Story Planner。请基于下面的 Reviewed PRD 和项目摘要，拆分出可独立执行的 Stories。

要求：
1. Story 粒度适中，可单独设计、开发、验证。
2. 明确依赖关系。
3. 每个 Story 必须包含：storyId、title、description、implementationTasks、acceptanceCriteria、dependencies、priority。
4. 只输出符合 JSON Schema 的 JSON。

目标项目：{session["projectRoot"]}

Reviewed PRD:
{prd_text}

项目摘要:
{project_summary[:12000]}
""".strip()

    result = subprocess.run(
        [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--json-schema",
            schema,
            "--",
            prompt,
        ],
        cwd=str(Path(session["projectRoot"])),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"结构化 Story 生成失败: {result.stderr.strip() or result.stdout.strip() or 'unknown error'}")
    data = parse_claude_json(result.stdout)
    stories = data.get("stories")
    if not isinstance(stories, list) or not stories:
        raise ValueError("结构化 Story 生成没有返回任何 Story。")
    return stories


def write_story_files_from_structured(session: Dict[str, Any], stories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    paths = artifact_paths(session)
    paths["stories"].mkdir(parents=True, exist_ok=True)
    for existing in paths["stories"].iterdir():
        if existing.is_dir():
            shutil.rmtree(existing)

    previews: List[Dict[str, Any]] = []
    for index, story in enumerate(stories, start=1):
        story_id = story["storyId"] or f"US-{index:03d}"
        dirname = f"{story_id}-{slugify(story['title'])}"
        story_dir = paths["stories"] / dirname
        story_dir.mkdir(parents=True, exist_ok=True)
        (story_dir / "story.md").write_text(render_story_markdown(story, session), encoding="utf-8")
        status = {
            "storyId": story_id,
            "title": story["title"],
            "phase": "pending",
            "phases": {
                "design": {"status": "pending", "version": 0},
                "design_review": {"status": "pending", "version": 0},
                "coding": {"status": "pending", "iteration": 0},
                "validation": {"status": "pending", "iteration": 0},
            },
            "retryCount": 0,
            "maxRetries": 20,
            "priority": story.get("priority", index),
            "dependencies": story.get("dependencies", []),
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "history": [],
        }
        write_json(story_dir / "status.json", status)
        previews.append({**story, "directory": dirname, "priority": story.get("priority", index)})
    return sort_stories(previews)


def generate_stories_with_skill(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    reviewed_path = Path(session.get("reviewedPrdPath", ""))
    if not reviewed_path.exists():
        raise ValueError("Reviewed PRD 不存在，请先确认 PRD。")

    paths = artifact_paths(session)
    paths["stories"].mkdir(parents=True, exist_ok=True)
    for existing in paths["stories"].iterdir():
        if existing.is_dir():
            shutil.rmtree(existing)

    compat_root = prepare_story_splitter_workspace(session)
    prompt = "\n".join([
        "/story-splitter",
        "请按 skill 说明拆分 Story，并直接创建文件，不要只给建议。",
        "PRD 文件路径：worker/shared/prd.md",
        "MVP 文件路径：worker/shared/mvp",
        "补充要求：Story 必须可独立执行，依赖关系明确，验收标准可测试。",
        "完成后请简单汇报创建了多少个 stories。",
    ])
    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(Path(session["projectRoot"])),
        "--add-dir",
        str(paths["artifact"]),
        "--",
        prompt,
    ]
    log_path = paths["logs"] / "story-splitter.log"
    start_time = time.time()
    last_update = 0.0
    output_chunks: List[str] = []

    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write(f"Started at: {datetime.now().isoformat()}\n")
        log_file.write(f"Working dir: {compat_root}\n")
        log_file.write(f"Command: {' '.join(cmd[:-1])} <prompt>\n")
        log_file.write("=" * 60 + "\n\n")
        log_file.flush()

        process = subprocess.Popen(
            cmd,
            cwd=str(compat_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

        while True:
            if process.stdout:
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if ready:
                    line = process.stdout.readline()
                    if line:
                        output_chunks.append(line)
                        log_file.write(line)
                        log_file.flush()

            ret_code = process.poll()
            elapsed = int(time.time() - start_time)

            if time.time() - last_update >= TASK_HEARTBEAT_INTERVAL:
                heartbeat_progress = min(72, 46 + max(1, elapsed // 15))
                set_activity(
                    session,
                    "generate_stories",
                    "running",
                    "正在调用 story-splitter 拆分 Stories...",
                    heartbeat_progress,
                    f"story-splitter 已运行 {elapsed} 秒。若项目较大，这一步可能持续几分钟。",
                )
                save_session(session)
                last_update = time.time()

            if elapsed > STORY_SPLITTER_TIMEOUT:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                log_file.write(f"\n[TIMEOUT] timed out after {STORY_SPLITTER_TIMEOUT} seconds\n")
                log_file.flush()
                raise TimeoutError(
                    f"story-splitter 执行超过 {STORY_SPLITTER_TIMEOUT // 60} 分钟仍未完成。"
                    f" 请查看日志：{log_path}"
                )

            if ret_code is not None:
                remaining = process.stdout.read() if process.stdout else ""
                if remaining:
                    output_chunks.append(remaining)
                    log_file.write(remaining)
                log_file.write(f"\n[EXIT] code={ret_code}\n")
                log_file.flush()
                if ret_code != 0:
                    output = "".join(output_chunks).strip()
                    raise RuntimeError(f"story-splitter 调用失败: {output[:2000] or 'unknown error'}")
                break

    story_dirs = valid_story_dirs(paths)
    if not story_dirs:
        return []

    return [story_preview_from_dir(story_dir) for story_dir in story_dirs]


def generate_stories(session: Dict[str, Any]) -> Dict[str, Any]:
    paths = artifact_paths(session)
    reviewed_path = Path(session.get("reviewedPrdPath", ""))
    if not reviewed_path.exists():
        raise ValueError("Reviewed PRD 不存在，请先确认 PRD。")

    set_activity(session, "generate_stories", "running", "正在准备 shared 上下文...", 12, "会生成 PRD、MVP、AGENTS 等共享输入。")
    save_session(session)
    materialize_shared_assets(session)
    set_activity(session, "generate_stories", "running", "正在调用 story-splitter 拆分 Stories...", 46, "这一步可能需要 10-60 秒，请不要重复点击。")
    save_session(session)
    stories = generate_stories_with_skill(session)
    previews = []

    if not stories:
        set_activity(session, "generate_stories", "running", "story-splitter 未写出完整文件，正在自动补齐 Story 工件...", 70, "会保留原有 skill 结果，并用结构化规划结果补写 story.md 和 status.json。")
        save_session(session)
        stories = generate_structured_stories(session)
        previews = write_story_files_from_structured(session, stories)

    set_activity(session, "generate_stories", "running", "正在读取并整理 Story 结果...", 78, "准备把 skill 生成的 story 文件展示到预览界面。")
    save_session(session)
    if not previews:
        for story in stories:
            previews.append(story)

    session["storiesPreview"] = sort_stories(previews)
    session["lastError"] = ""
    set_activity(session, "generate_stories", "completed", f"Story 拆分完成，共生成 {len(previews)} 个 Story。", 100)
    session["step"] = "awaiting_start"
    return save_session(session)


def render_story_markdown(story: Dict[str, Any], session: Dict[str, Any]) -> str:
    tasks = "\n".join([f"- {item}" for item in story.get("implementationTasks", [])]) or "- 待实现"
    criteria = "\n".join([f"- {item}" for item in story.get("acceptanceCriteria", [])]) or "- 待补充"
    deps = ", ".join(story.get("dependencies", [])) or "[]"
    return f"""# {story['storyId']}: {story['title']}

## 描述
{story.get('description', '').strip()}

## 实现任务
{tasks}

## 验收标准
{criteria}

## 优先级
{story.get('priority', 1)}

## 依赖
{deps}

## 参考
- PRD: .ai-worker/shared/prd.md
- MVP: .ai-worker/shared/mvp
"""


def start_runner(session: Dict[str, Any]) -> Dict[str, Any]:
    paths = artifact_paths(session)
    if not session.get("storiesPreview"):
        session["storiesPreview"] = build_story_previews_from_disk(session)
    if not session.get("storiesPreview") and story_splitter_completed(paths):
        stories = generate_structured_stories(session)
        session["storiesPreview"] = write_story_files_from_structured(session, stories)
        session["step"] = "awaiting_start"
    if not session.get("storiesPreview"):
        raise ValueError("还没有生成 Stories，不能开始执行。")
    session["storiesPreview"] = sort_stories(session["storiesPreview"])
    project_root = Path(session["projectRoot"])
    artifact_root = Path(session["artifactRoot"])
    cmd = [
        sys.executable,
        str(WORKER_DIR / "worker.py"),
        "--project-root",
        str(project_root),
        "--artifact-root",
        str(artifact_root),
    ]
    process = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    session["runnerPid"] = process.pid
    session["step"] = "running"
    session["runStatus"] = {"state": "running", "startedAt": now_iso()}
    return save_session(session)


def recover_session(session: Dict[str, Any]) -> Dict[str, Any]:
    if not session.get("artifactRoot"):
        return default_session()

    paths = artifact_paths(session)
    has_stories = paths["stories"].exists() and any(item.is_dir() for item in paths["stories"].iterdir())
    reviewed_exists = bool(session.get("reviewedPrdPath")) and Path(session["reviewedPrdPath"]).exists()
    raw_exists = bool(session.get("rawPrdPath")) and Path(session["rawPrdPath"]).exists()

    if has_stories:
        session["step"] = "awaiting_start"
    elif reviewed_exists:
        session["step"] = "awaiting_prd_confirmation"
    elif session.get("questions"):
        session["step"] = "awaiting_answers"
    elif raw_exists:
        session["step"] = "upload_prd"
    elif session.get("projectRoot"):
        session["step"] = "upload_prd"
    else:
        session = default_session()

    session["lastError"] = ""
    return save_session(session)


def restart_session(session: Dict[str, Any]) -> Dict[str, Any]:
    artifact_root_raw = session.get("artifactRoot")
    if not artifact_root_raw:
        clear_active_session_file()
        return default_session()

    artifact_root = Path(artifact_root_raw).resolve()
    if artifact_root.name != ".ai-worker":
        raise ValueError(f"拒绝重置非 .ai-worker 目录: {artifact_root}")

    stop_runner(session)

    if artifact_root.exists():
        shutil.rmtree(artifact_root)

    clear_active_session_file()
    return default_session()


def start_background_task(artifact_root: Path, target):
    thread = threading.Thread(target=target, name=f"session-task-{artifact_root.name}", daemon=True)
    thread.start()


def start_analyze_task(session: Dict[str, Any]) -> Dict[str, Any]:
    session["step"] = "analyzing_project"
    session["lastError"] = ""
    set_activity(session, "analyze_project", "running", "已开始分析项目与 PRD...", 5, "页面会自动刷新，你可以留在当前页面等待结果。")
    saved = save_session(session)
    artifact_root = Path(saved["artifactRoot"])

    def task():
        current = load_saved_session(artifact_root)
        try:
            analyze_project(current)
        except Exception as exc:
            failed = load_saved_session(artifact_root)
            failed["step"] = "error"
            failed["lastError"] = str(exc)
            set_activity(failed, "analyze_project", "failed", "项目分析失败。", 100, str(exc))
            save_session(failed)

    start_background_task(artifact_root, task)
    return saved


def start_story_generation_task(session: Dict[str, Any]) -> Dict[str, Any]:
    session["step"] = "generating_stories"
    session["lastError"] = ""
    set_activity(session, "generate_stories", "running", "已开始拆分 Stories...", 5, "这一步通常需要几十秒，完成后会自动跳转到 Story 预览。")
    saved = save_session(session)
    artifact_root = Path(saved["artifactRoot"])

    def task():
        current = load_saved_session(artifact_root)
        try:
            generate_stories(current)
        except Exception as exc:
            failed = load_saved_session(artifact_root)
            failed["step"] = "error"
            failed["lastError"] = str(exc)
            set_activity(failed, "generate_stories", "failed", "Story 拆分失败。", 100, str(exc))
            save_session(failed)

    start_background_task(artifact_root, task)
    return saved


def current_agent_log(session: Dict[str, Any]) -> Dict[str, Any]:
    paths = artifact_paths(session)
    worker_state = read_json(paths["worker_state"], {})
    agent = worker_state.get("currentAgent")
    story = worker_state.get("currentStoryId")
    log_path = None
    if agent and story:
        matches = sorted(paths["logs"].glob(f"*-{agent}-{story}.log"))
        if matches:
            log_path = matches[-1]
    if not log_path:
        if session.get("activity", {}).get("kind") == "generate_stories":
            splitter_log = paths["logs"] / "story-splitter.log"
            if splitter_log.exists():
                log_path = splitter_log
                agent = "story-splitter"
                story = "story-planning"
        worker_logs = sorted(paths["logs"].glob("worker-*.log"))
        if not log_path:
            log_path = worker_logs[-1] if worker_logs else None
    return {
        "agent": agent,
        "storyId": story,
        "phase": worker_state.get("currentPhase"),
        "path": str(log_path) if log_path else "",
        "lines": tail_file(log_path) if log_path else [],
        "lastUpdate": worker_state.get("lastUpdate") or session.get("updatedAt"),
    }


def augment_session(session: Dict[str, Any]) -> Dict[str, Any]:
    if not session.get("artifactRoot"):
        return session
    paths = artifact_paths(session)
    if not session.get("storiesPreview"):
        session["storiesPreview"] = build_story_previews_from_disk(session)
    else:
        session["storiesPreview"] = sort_stories(session["storiesPreview"])
    if (
        session.get("step") == "generating_stories"
        and session.get("storiesPreview")
        and story_splitter_completed(paths)
    ):
        session["step"] = "awaiting_start"
        session["lastError"] = ""
        set_activity(
            session,
            "generate_stories",
            "completed",
            f"Story 拆分完成，共生成 {len(session['storiesPreview'])} 个 Story。",
            100,
            "story-splitter 已完成，当前可以开始执行。",
        )
    worker_state = read_json(paths["worker_state"], {})
    stories = get_all_stories(session)
    total = len(stories)
    done = sum(1 for s in stories if s["status"].get("phase") == "done")
    pending = sum(1 for s in stories if s["status"].get("phase") == "pending")
    in_progress = total - done - pending
    session["runStatus"] = {
        **session.get("runStatus", {}),
        "total": total,
        "done": done,
        "pending": pending,
        "inProgress": in_progress,
        "progress": round(done / total * 100, 1) if total else 0,
        "workerState": worker_state,
    }
    if total and done == total and session.get("step") == "running":
        session["step"] = "completed"
        save_session(session)
    session["currentAgentLog"] = current_agent_log(session)
    if session.get("reviewedPrdPath") and Path(session["reviewedPrdPath"]).exists():
        session["reviewedPrd"] = Path(session["reviewedPrdPath"]).read_text(encoding="utf-8", errors="replace")
    if session.get("rawPrdPath") and Path(session["rawPrdPath"]).exists():
        session["rawPrd"] = Path(session["rawPrdPath"]).read_text(encoding="utf-8", errors="replace")
    return session


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/session":
            self._send_json(load_session())
        elif path == "/api/status":
            self._handle_api_status()
        elif path == "/api/stories":
            self._send_json(get_all_stories())
        elif path.startswith("/api/story/"):
            detail = get_story_detail(path.split("/")[-1])
            self._send_json(detail) if detail else self._send_404()
        elif path == "/api/logs/current-agent":
            self._send_json(current_agent_log(load_session()))
        elif path == "/api/logs/worker" or path == "/api/logs":
            self._send_json(self._worker_logs())
        elif path == "/" or path == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._send_static("style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
        else:
            self._send_404()

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/session/project":
                self._handle_project()
            elif path == "/api/session/prd":
                self._handle_prd_upload()
            elif path == "/api/session/analyze":
                self._send_json(start_analyze_task(load_session()))
            elif path == "/api/session/answers":
                self._handle_answers()
            elif path == "/api/session/prd/confirm":
                self._handle_prd_confirm()
            elif path == "/api/session/stories/generate":
                self._send_json(start_story_generation_task(load_session()))
            elif path == "/api/session/recover":
                self._send_json(recover_session(load_session()))
            elif path == "/api/session/restart":
                self._send_json(restart_session(load_session()))
            elif path == "/api/session/start":
                self._send_json(start_runner(load_session()))
            else:
                self._send_404()
        except Exception as exc:
            session = load_session()
            if session.get("artifactRoot"):
                session["step"] = "error"
                session["lastError"] = str(exc)
                save_session(session)
            self._send_json({"error": str(exc)}, status=400)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _handle_project(self):
        data = self._read_json()
        project_root = Path(data.get("projectRoot", "")).expanduser()
        if not project_root.is_absolute():
            raise ValueError("项目根目录必须是绝对路径。")
        project_root = project_root.resolve()
        if not project_root.exists() or not project_root.is_dir():
            raise ValueError("项目根目录不存在或不是目录。")
        artifact_root = project_root / ".ai-worker"
        ensure_artifact_tree(artifact_root)
        session = default_session()
        session.update({
            "projectRoot": str(project_root),
            "artifactRoot": str(artifact_root),
            "step": "upload_prd",
        })
        self._send_json(save_session(session))

    def _handle_prd_upload(self):
        session = load_session()
        if not session.get("artifactRoot"):
            raise ValueError("请先选择项目根目录。")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": self.headers.get("Content-Type"),
        })
        field = form["prd"] if "prd" in form else None
        if field is None or not field.filename:
            raise ValueError("请上传 PRD Markdown 文件。")
        suffix = Path(field.filename).suffix.lower()
        if suffix not in {".md", ".markdown"}:
            raise ValueError("第一版仅支持 .md / .markdown PRD。")
        content = field.file.read().decode("utf-8")
        paths = artifact_paths(session)
        raw_path = paths["context"] / "raw-prd.md"
        raw_path.write_text(content, encoding="utf-8")
        session["rawPrdPath"] = str(raw_path)
        session["step"] = "analyzing_project"
        self._send_json(save_session(session))

    def _handle_answers(self):
        data = self._read_json()
        session = load_session()
        answers = data.get("answers", {})
        if not isinstance(answers, dict):
            raise ValueError("answers 必须是对象。")
        session["answers"] = answers
        write_json(artifact_paths(session)["context"] / "answers.json", answers)
        session["step"] = "reviewing_prd"
        self._send_json(review_prd(session))

    def _handle_prd_confirm(self):
        data = self._read_json()
        markdown = data.get("markdown", "")
        if not markdown.strip():
            raise ValueError("确认后的 PRD 不能为空。")
        session = load_session()
        path = artifact_paths(session)["context"] / "reviewed-prd.md"
        path.write_text(markdown, encoding="utf-8")
        session["reviewedPrdPath"] = str(path)
        session["reviewedPrd"] = markdown
        session["step"] = "generating_stories"
        self._send_json(save_session(session))

    def _handle_api_status(self):
        session = load_session()
        self._send_json(session.get("runStatus", {}))

    def _worker_logs(self) -> Dict[str, Any]:
        session = load_session()
        artifact_root = Path(session.get("artifactRoot") or WORKER_DIR)
        logs_dir = artifact_root / "logs"
        worker_logs = sorted(logs_dir.glob("worker-*.log"))
        return {
            "workerLogs": tail_file(worker_logs[-1]) if worker_logs else [],
            "agentLogs": [
                {"name": p.name, "size": p.stat().st_size, "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
                for p in sorted(logs_dir.glob("*.log")) if not p.name.startswith("worker-")
            ][-20:],
        }

    def _send_json(self, data: Any, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _send_static(self, filename: str, content_type: str):
        path = Path(__file__).parent / filename
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def _send_404(self):
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")


def start(port=7332, open_browser=False):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"UI Server: http://localhost:{port}")
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")
    return server


if __name__ == "__main__":
    start(port=7332, open_browser=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
