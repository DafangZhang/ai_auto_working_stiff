---
name: story-splitter
description: "将 PRD 拆分为独立的 Story 文件夹，供 Worker 系统使用。触发词：拆分 prd 成 stories"
---

# Story Splitter

将产品需求文档 (PRD) 拆分为独立的、可执行的 Story 文件夹。

---

## 工作流程

1. 接收用户指定的 PRD 文件路径
2. 分析 PRD 中的 User Stories
3. 在 `worker/stories/` 下为每个 story 创建独立文件夹
4. 生成 `story.md` 和初始 `status.json`

---

## 输入

用户需要提供：
- PRD 文件路径（相对于项目根目录）
- 可选：MVP 参考文件路径

---

## 输出格式

每个 story 文件夹结构：
```
worker/stories/{story-id}/
├── story.md       # 需求描述
└── status.json    # 初始状态
```

### story.md 格式
```markdown
# {story-id}: {title}

## 描述
{description}

## 实现任务
{implementationTasks}

## 验收标准
{acceptanceCriteria}

## 优先级
{priority}

## 依赖
{dependencies}

## 参考
- PRD: {prd-path}
- MVP: {mvp-path}
```

### status.json 格式
```json
{
  "storyId": "US-001",
  "title": "WebSocket Server 实现",
  "phase": "pending",
  "phases": {
    "design": {"status": "pending", "version": 0},
    "design_review": {"status": "pending", "version": 0},
    "coding": {"status": "pending", "iteration": 0},
    "validation": {"status": "pending", "iteration": 0}
  },
  "retryCount": 0,
  "maxRetries": 20,
  "createdAt": "2024-01-15T10:00:00Z",
  "updatedAt": "2024-01-15T10:00:00Z",
  "history": []
}
```

---

## Story 拆分原则

### 1. 大小适中
- 每个 story 应该能在一次完整的开发周期内完成
- 经验法则：实现时间不超过 2-4 小时

### 2. 依赖优先
- 先创建基础设施 story（数据库、核心服务）
- 后创建依赖基础设施的 story（UI、业务逻辑）

### 3. 可独立验证
- 每个 story 必须有明确的验收标准
- 可以独立测试，不依赖其他未完成的 story

---

## Story ID 格式

- 格式：`US-XXX`（User Story）
- 示例：`US-001`, `US-002`
- 不足 3 位补零

---

## 执行步骤

1. **读取 PRD**
   ```bash
   cat {prd-path}
   ```

2. **分析 Stories**
   - 提取所有 User Stories
   - 确定优先级和依赖关系
   - 检查是否需要进一步拆分

3. **创建文件夹结构**
   ```bash
   mkdir -p worker/stories/US-001-{kebab-case-title}
   ```

4. **生成 story.md**
   - 从 PRD 提取内容
   - 格式化输出

5. **生成 status.json**
   - 初始状态为 `pending`
   - 记录创建时间

6. **创建软链接**
   ```bash
   ln -sf {prd-path} worker/shared/prd.md
   ln -sf {mvp-path} worker/shared/mvp 2>/dev/null || true
   ```

---

## 示例

**输入 PRD:**
```markdown
# AI 狼人杀游戏

## US-001: WebSocket 服务器
实现 WebSocket 服务器支持多客户端连接。

**验收标准:**
- WebSocket 服务器启动
- 支持多客户端连接
- Typecheck 通过
```

**输出:**
```
worker/stories/
└── US-001-websocket-server/
    ├── story.md
    └── status.json
```

---

## 检查清单

拆分完成后验证：
- [ ] 所有 stories 都有唯一 ID
- [ ] 所有 stories 都有明确的验收标准
- [ ] 依赖关系清晰（如果有）
- [ ] status.json 格式正确
- [ ] 软链接创建成功
