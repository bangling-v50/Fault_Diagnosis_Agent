<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  createChatSession,
  deleteChatSession,
  extractChatMemories,
  getChatSession,
  getChatSessions,
  getMemories,
  deleteMemory,
} from '../api/chat'
import { streamChatMessage } from '../api/chatStream'
import { getDevices } from '../api/devices'
import { extractError } from '../api/index'
import MarkdownContent from '../components/MarkdownContent.vue'

const route = useRoute()
const router = useRouter()

const KIND_LABELS = { preference: '偏好', fact: '事实', constraint: '约束' }

// ---------- 状态 ----------
const sessions = ref([])
const current = ref(null) // { session, messages, memory }
const memories = ref([])
const devices = ref([])
const loading = ref(false)
const sending = ref(false)
const streamingText = ref('')
const liveTools = ref([])
const input = ref('')
const listEl = ref(null)
const memoryVisible = ref(false)
const createVisible = ref(false)
const createForm = reactive({ device_id: null })
let controller = null

const messages = computed(() => current.value?.messages || [])
const usagePercent = computed(() => current.value?.memory?.usagePercent ?? 0)

// ---------- 加载 ----------
onMounted(async () => {
  await loadSessions()
  try {
    devices.value = (await getDevices({ page: 1, page_size: 100 })).data.data.items
  } catch {
    /* 设备列表拿不到不影响聊天 */
  }
  if (route.params.id) await openSession(Number(route.params.id))
})

onBeforeUnmount(() => controller?.abort())

watch(
  () => route.params.id,
  (id) => {
    if (id && Number(id) !== current.value?.session?.id) openSession(Number(id))
  },
)

async function loadSessions() {
  try {
    sessions.value = (await getChatSessions()).data.data
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

async function openSession(id) {
  loading.value = true
  streamingText.value = ''
  liveTools.value = []
  try {
    current.value = (await getChatSession(id)).data.data
    await scrollToBottom()
  } catch (e) {
    ElMessage.error(extractError(e))
    router.push('/chat')
  } finally {
    loading.value = false
  }
}

// ---------- 建/删会话 ----------
function openCreate() {
  createForm.device_id = null
  createVisible.value = true
}

async function handleCreate() {
  try {
    const res = await createChatSession({
      device_id: createForm.device_id || undefined,
    })
    createVisible.value = false
    await loadSessions()
    router.push(`/chat/${res.data.data.id}`)
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

async function handleDelete(session) {
  try {
    await ElMessageBox.confirm(`删除会话「${session.title || '未命名'}」？消息一并删除。`, '删除会话', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await deleteChatSession(session.id)
    ElMessage.success('会话已删除')
    if (current.value?.session?.id === session.id) {
      current.value = null
      router.push('/chat')
    }
    await loadSessions()
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

// ---------- 发消息（流式） ----------
async function send() {
  const text = input.value.trim()
  if (!text || sending.value || !current.value) return
  sending.value = true
  streamingText.value = ''
  liveTools.value = []
  input.value = ''

  // 本地先挂上用户消息与一个空的助手消息，事件到了往里填
  current.value.messages.push({ id: `local-user-${Date.now()}`, role: 'user', content: text })
  await scrollToBottom()

  controller = new AbortController()
  let failed = false
  try {
    await streamChatMessage(current.value.session.id, text, {
      signal: controller.signal,
      onEvent: (event) => {
        if (event.type === 'content.delta') {
          streamingText.value += event.delta || ''
          scrollToBottom()
        } else if (event.type === 'tool.call') {
          const call = event.toolCall || {}
          const existing = liveTools.value.findIndex((item) => item.id === call.id)
          if (existing >= 0) liveTools.value[existing] = call
          else liveTools.value.push(call)
          scrollToBottom()
        } else if (event.type === 'error') {
          failed = true
          ElMessage.error(event.message || '这一轮出错了')
        } else if (event.type === 'complete') {
          if (event.result?.memory && current.value) current.value.memory = event.result.memory
        }
      },
    })
  } catch (e) {
    // 用户点了「停止」不算错误
    if (e?.name !== 'AbortError') {
      failed = true
      ElMessage.error(e.message || '连接中断')
    }
  } finally {
    sending.value = false
    controller = null
    // 流式结束后从服务端取权威数据（消息 id、metadata、上下文占用都以库里为准）
    if (!failed || streamingText.value) await openSession(current.value.session.id)
    await loadSessions()
  }
}

function stop() {
  controller?.abort()
}

function onKeydown(event) {
  // Enter 发送，Shift+Enter 换行
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
  }
}

async function scrollToBottom() {
  await nextTick()
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
}

// ---------- 长期记忆 ----------
async function openMemories() {
  try {
    memories.value = (await getMemories()).data.data
    memoryVisible.value = true
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

async function handleDeleteMemory(memory) {
  try {
    await deleteMemory(memory.id)
    ElMessage.success('已删除')
    memories.value = (await getMemories()).data.data
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

async function handleExtract() {
  if (!current.value) return
  try {
    const written = (await extractChatMemories(current.value.session.id)).data.data.written
    ElMessage.success(written > 0 ? `记住了 ${written} 条新信息` : '这次没有新的值得长期记住的信息')
    memories.value = (await getMemories()).data.data
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

function formatTime(t) {
  return t ? new Date(t).toLocaleString() : ''
}

function toolLabel(name) {
  return {
    knowledge_retrieval: '检索知识库',
    alert_history: '查历史告警',
    work_order_history: '查历史工单',
    diagnosis_history: '查历史诊断',
  }[name] || name
}
</script>

<template>
  <div class="chat-page">
    <!-- 左：会话列表 -->
    <aside class="sessions">
      <div class="sessions-head">
        <span>会话</span>
        <el-button size="small" type="primary" @click="openCreate">新建</el-button>
      </div>
      <div class="sessions-list">
        <div
          v-for="item in sessions"
          :key="item.id"
          :class="['session-item', { active: item.id === current?.session?.id }]"
          @click="router.push(`/chat/${item.id}`)"
        >
          <div class="session-title">{{ item.title || '新会话' }}</div>
          <div class="session-meta">
            <span>{{ formatTime(item.updated_at) }}</span>
            <el-button text size="small" type="danger" @click.stop="handleDelete(item)">删除</el-button>
          </div>
        </div>
        <el-empty v-if="!sessions.length" description="还没有会话" :image-size="60" />
      </div>
      <div class="sessions-foot">
        <el-button size="small" text @click="openMemories">我的长期记忆</el-button>
      </div>
    </aside>

    <!-- 右：对话 -->
    <section class="conversation" v-loading="loading">
      <template v-if="current">
        <div class="conversation-head">
          <div>
            <strong>{{ current.session.title || '新会话' }}</strong>
            <span class="muted" v-if="current.session.device_id">
              ｜ 设备 #{{
                devices.find((d) => d.id === current.session.device_id)?.device_code ||
                current.session.device_id
              }}
            </span>
          </div>
          <div class="head-actions">
            <el-tooltip content="当前上下文占用（到 70% 会压缩成摘要）">
              <span class="muted">上下文 {{ usagePercent }}%</span>
            </el-tooltip>
            <el-button size="small" @click="handleExtract">结束会话并记住重点</el-button>
          </div>
        </div>

        <div class="messages" ref="listEl">
          <div v-for="message in messages" :key="message.id" :class="['msg', message.role]">
            <div class="msg-role">{{ message.role === 'user' ? '我' : '助手' }}</div>
            <div class="msg-body">
              <MarkdownContent v-if="message.role === 'assistant'" :content="message.content" mode="report" />
              <div v-else class="msg-text">{{ message.content }}</div>
            </div>
          </div>

          <!-- 正在流式生成的那条 -->
          <div v-if="sending || streamingText" class="msg assistant">
            <div class="msg-role">助手</div>
            <div class="msg-body">
              <div v-for="call in liveTools" :key="call.id" class="tool-chip">
                <el-icon v-if="call.status === 'started'" class="is-loading"><Loading /></el-icon>
                <el-icon v-else><Select /></el-icon>
                {{ toolLabel(call.name) }}{{ call.status === 'started' ? '…' : ' 完成' }}
              </div>
              <MarkdownContent v-if="streamingText" :content="streamingText" mode="report" />
              <span v-else-if="sending" class="muted">思考中…</span>
            </div>
          </div>

          <el-empty
            v-if="!messages.length && !sending"
            description="问点什么吧，比如「这台设备以前修过吗」「手册里对主轴过载怎么说」"
          />
        </div>

        <div class="composer">
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            resize="none"
            placeholder="Enter 发送，Shift+Enter 换行"
            @keydown="onKeydown"
          />
          <div class="composer-actions">
            <el-button v-if="sending" @click="stop">停止</el-button>
            <el-button type="primary" :disabled="sending || !input.trim()" @click="send">发送</el-button>
          </div>
        </div>
      </template>

      <el-empty v-else description="从左边选一个会话，或新建一个">
        <el-button type="primary" @click="openCreate">新建会话</el-button>
      </el-empty>
    </section>

    <!-- 新建会话 -->
    <el-dialog v-model="createVisible" title="新建会话" width="460px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="关联设备">
          <el-select v-model="createForm.device_id" placeholder="可不选" clearable filterable style="width: 100%">
            <el-option
              v-for="item in devices"
              :key="item.id"
              :label="item.device_name ? `${item.device_code}（${item.device_name}）` : item.device_code"
              :value="item.id"
            />
          </el-select>
          <p class="dialog-hint">
            选设备后助手能查这台设备的历史诊断与工单；<strong>售后/管理员账号必须选设备</strong>
            （他们不属于任何租户）。
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="handleCreate">创建并开始</el-button>
      </template>
    </el-dialog>

    <!-- 长期记忆 -->
    <el-drawer v-model="memoryVisible" title="我的长期记忆" size="440px">
      <p class="drawer-hint">
        这些是助手从历史对话里提炼出的、关于你的稳定信息（身份、偏好、常用设备），
        每轮对话都会带上它们。<strong>你可以随时删除</strong>。
      </p>
      <div v-for="memory in memories" :key="memory.id" class="memory-item">
        <div class="memory-head">
          <el-tag size="small" :type="memory.status === 'active' ? 'success' : 'info'">
            {{ KIND_LABELS[memory.kind] || memory.kind }}
          </el-tag>
          <el-tag v-if="memory.status !== 'active'" size="small" type="info">已作废</el-tag>
          <el-button text size="small" type="danger" @click="handleDeleteMemory(memory)">删除</el-button>
        </div>
        <div :class="['memory-content', { muted: memory.status !== 'active' }]">
          {{ memory.content }}
        </div>
      </div>
      <el-empty v-if="!memories.length" description="还没有记住任何信息" :image-size="60" />
    </el-drawer>
  </div>
</template>

<style scoped>
.chat-page { display: flex; gap: 16px; height: calc(100vh - 96px); max-width: 1250px; }
.sessions { width: 260px; flex: none; background: #fff; border: 1px solid #ebeef5; border-radius: 4px; display: flex; flex-direction: column; }
.sessions-head { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #ebeef5; font-weight: 600; }
.sessions-list { flex: 1; overflow-y: auto; padding: 8px; }
.session-item { padding: 8px 10px; border-radius: 4px; cursor: pointer; margin-bottom: 4px; }
.session-item:hover { background: #f5f7fa; }
.session-item.active { background: #ecf5ff; }
.session-title { font-size: 14px; color: #303133; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-meta { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #909399; margin-top: 2px; }
.sessions-foot { border-top: 1px solid #ebeef5; padding: 8px; text-align: center; }

.conversation { flex: 1; display: flex; flex-direction: column; background: #fff; border: 1px solid #ebeef5; border-radius: 4px; min-width: 0; }
.conversation-head { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid #ebeef5; }
.head-actions { display: flex; align-items: center; gap: 12px; }
.messages { flex: 1; overflow-y: auto; padding: 16px; }
.msg { display: flex; gap: 10px; margin-bottom: 18px; }
.msg-role { width: 40px; flex: none; font-size: 12px; color: #909399; padding-top: 4px; }
.msg-body { flex: 1; min-width: 0; }
.msg.user .msg-text { background: #ecf5ff; border-radius: 6px; padding: 8px 12px; white-space: pre-wrap; font-size: 14px; color: #303133; line-height: 1.7; }
.tool-chip { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: #909399; background: #f5f7fa; border-radius: 10px; padding: 2px 10px; margin-bottom: 6px; margin-right: 6px; }

.composer { border-top: 1px solid #ebeef5; padding: 12px 16px; }
.composer-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }

.muted { color: #909399; font-size: 13px; }
.dialog-hint { color: #909399; font-size: 12px; line-height: 1.7; margin: 6px 0 0; }
.drawer-hint { color: #909399; font-size: 13px; line-height: 1.8; margin: 0 0 16px; }
.memory-item { border-bottom: 1px solid #f2f6fc; padding: 10px 0; }
.memory-head { display: flex; align-items: center; gap: 8px; }
.memory-content { font-size: 13px; color: #303133; line-height: 1.7; margin-top: 6px; }
</style>
