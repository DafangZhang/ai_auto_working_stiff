# Worker - 自主 AI Agent 工作流系统

一个严格顺序执行的 AI Agent 工作流系统：Designer → Reviewer → Coder → Validator

> 当前推荐入口是根目录 README 中描述的 Web 工作台流程：先通过 `python3 worker/ui/server.py` 启动 UI，选择目标项目、上传 PRD、确认 Stories 后再启动执行。本文保留旧版手工创建 `worker/stories/` 的说明，用于理解底层 Runner 和兼容旧工件目录。

## 特性

- **严格顺序执行**: 每个 Story 必须按顺序完成设计、审核、开发、验证
- **状态驱动**: 通过 status.json 强制流程，防止跳过环节
- **详细反馈**: Validator 提供错误日志和截图，帮助开发者定位问题
- **Web UI**: 实时监控工作进度和日志

## 快速开始

### 1. 拆分 PRD 为 Stories

使用 skill 拆分 PRD：

```bash
# 在项目根目录下
claude skill:story-splitter
# 然后指定 PRD 文件路径
```

或者手动创建 story：

```bash
mkdir -p worker/stories/US-001-xxx
cat > worker/stories/US-001-xxx/story.md << 'EOF'
# US-001: Story 标题

## 描述
作为...我想要...以便...

## 实现任务
- 任务1
- 任务2

## 验收标准
- 标准1
- 标准2
- Typecheck 通过
EOF

cat > worker/stories/US-001-xxx/status.json << 'EOF'
{
  "storyId": "US-001",
  "title": "Story 标题",
  "phase": "pending",
  "phases": {
    "design": {"status": "pending", "version": 0},
    "design_review": {"status": "pending", "version": 0},
    "coding": {"status": "pending", "iteration": 0},
    "validation": {"status": "pending", "iteration": 0}
  },
  "retryCount": 0,
  "maxRetries": 20,
  "priority": 1,
  "dependencies": [],
  "createdAt": "2024-01-15T10:00:00Z",
  "updatedAt": "2024-01-15T10:00:00Z",
  "history": []
}
EOF
```

### 2. 创建共享链接

```bash
# 链接 PRD
ln -sf $(pwd)/PRD.md worker/shared/prd.md

# 链接 MVP
ln -sf $(pwd)/mvp worker/shared/mvp

# 链接 AGENTS.md
ln -sf $(pwd)/AGENTS.md worker/shared/AGENTS.md
```

### 3. 启动 Worker

```bash
cd worker
python3 worker.py
```

Worker 会自动：
1. 启动 Web UI（http://localhost:7332）
2. 按顺序处理所有 Stories
3. 记录日志到 `logs/` 目录

## 文件结构

```
worker/
├── stories/                    # Story 文件夹
│   └── {story-id}/
│       ├── story.md           # 需求描述
│       ├── design-v1.md       # Designer 输出
│       ├── design-v2.md       # Reviewer 修改
│       ├── implementation.md  # Coder 输出
│       ├── test-report.md     # Validator 输出
│       └── status.json        # 状态管理
├── agents/
│   ├── designer.md            # Designer Agent 指令
│   ├── reviewer.md            # Reviewer Agent 指令
│   ├── coder.md               # Coder Agent 指令
│   └── validator.md           # Validator Agent 指令
├── shared/
│   ├── prd.md -> 链接到项目 PRD
│   └── mvp/ -> 链接到项目 MVP
├── logs/                       # 日志目录
├── ui/                         # Web UI
│   ├── server.py
│   ├── index.html
│   ├── style.css
│   └── app.js
├── worker.py                   # 主控脚本
├── worker_state.json           # 全局状态文件（运行时自动更新）
└── config.json                 # 配置文件
```

## 工作流程

```
Story 创建 (pending)
    ↓
Designer Agent
- 读取 story.md, prd.md, mvp/
- 输出 design-v1.md
- 更新 status: designed
    ↓
Reviewer Agent
- 读取 design-v1.md
- 修改并输出 design-v2.md
- 更新 status: design_reviewed
    ↓
Coder Agent
- 读取 design-v2.md
- 实现代码
- 本地测试 (typecheck, lint, test)
- 提交代码
- 输出 implementation.md
- 更新 status: coding_complete
    ↓
Validator Agent
- 读取 implementation.md, design-v2.md
- 代码审查
- 运行测试
- 浏览器测试 (如果需要)
- 输出 test-report.md
- 更新 status:
  - 通过: done
  - 失败: coding (Coder 重新修复)
```

## Web UI

访问 http://localhost:7332 查看：

- **总进度**: 完成百分比
- **Story 列表**: 所有 stories 的状态
- **工作日志**: 实时日志输出
- **详情查看**: 点击 Story 查看设计文档、实现、测试报告

## Agent 指令

每个 Agent 的指令文件位于 `agents/` 目录：

- **designer.md**: 创建技术设计方案
- **reviewer.md**: 审核并修改设计方案
- **coder.md**: 实现代码并进行本地测试
- **validator.md**: 验证实现是否符合设计

## 状态文件

### Story 状态: `status.json`

每个 story 有自己的状态文件，控制该 story 的工作流：

```json
{
  "storyId": "US-001",
  "phase": "design_reviewed",
  "phases": {
    "design": {"status": "completed", "version": 1},
    "design_review": {"status": "completed", "version": 2},
    "coding": {"status": "pending", "iteration": 0},
    "validation": {"status": "pending", "iteration": 0}
  },
  "retryCount": 0,
  "maxRetries": 20
}
```

### 全局状态: `worker_state.json`

Worker 运行时会自动更新此文件，Agent 通过它获取当前任务：

```json
{
  "currentStoryId": "US-001-websocket-server",
  "currentPhase": "pending",
  "currentAgent": "designer",
  "startedAt": "2024-01-15T10:00:00Z",
  "iteration": 5,
  "totalStories": 10,
  "completedStories": 2,
  "lastUpdate": "2024-01-15T10:30:00Z"
}
```

**Agent 使用方式：**
每个 Agent 启动时必须先读取 `worker/worker_state.json`，确认：
1. `currentAgent` 是否是自己（designer/reviewer/coder/validator）
2. `currentStoryId` 是要处理的 story
3. 然后读取对应 story 的 `status.json` 验证状态

`status.json.phase` 的规范值只有：
- `pending`
- `designed`
- `design_reviewed`
- `coding`
- `coding_complete`
- `done`

## 配置

编辑 `config.json`：

```json
{
  "timeouts": {
    "agent": 1800,  // Agent 超时时间（秒）
    "poll": 5       // 轮询间隔（秒）
  },
  "limits": {
    "maxIterations": 100,  // 最大迭代次数
    "maxRetries": 20       // 最大重试次数
  },
  "ui": {
    "port": 7332,          // UI 端口
    "pollInterval": 5000   // UI 轮询间隔（毫秒）
  }
}
```

## 日志

日志保存在 `logs/` 目录：

- `worker-YYYYMMDD.log`: Worker 主日志
- `YYYYMMDD-HHMMSS-{agent}-{story-id}.log`: Agent 执行日志

## 故障排除

### Agent 跳过环节

检查 `status.json` 中的 `phase` 是否正确。

### Agent 超时

检查 `config.json` 中的 `timeouts.agent` 设置。

### UI 无法访问

检查端口是否被占用：
```bash
lsof -i :7332
```

## 注意事项

1. **不要手动修改** `design-v1.md`, `design-v2.md`, `implementation.md`, `test-report.md`
2. **可以手动修改** `status.json` 来重置状态（谨慎操作）
3. **Coder 必须使用真实测试**，不允许使用 mock
4. **Validator 必须提供详细反馈**，包括日志和截图
