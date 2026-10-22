import { extractError } from './index'
import { getToken } from '../utils/token'

// 与 api/index.js 里的 axios 实例保持一致：后端直连、无 /api 前缀
const BASE_URL = 'http://127.0.0.1:8000'

/**
 * 流式发一条聊天消息（SSE）。
 *
 * **为什么不用 `EventSource`**：它**不能带请求头**，而我们的鉴权是 `Authorization: Bearer`。
 * 所以只能用 `fetch` + `ReadableStream` 自己读流、自己按 SSE 的帧格式拆。
 * （axios 也不行：它的响应拦截器与 30 秒超时都是为"一次请求一次响应"设计的。）
 *
 * @param {number} sessionId
 * @param {string} content
 * @param {{ onEvent?: (event: object) => void, signal?: AbortSignal }} options
 *        `signal` 用于中断生成（页面上那个「停止」按钮）。
 */
export async function streamChatMessage(sessionId, content, { onEvent, signal } = {}) {
  const token = getToken()
  const response = await fetch(`${BASE_URL}/chat/sessions/${sessionId}/messages:stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ content }),
    signal,
  })

  if (!response.ok) {
    // 出错时后端返回的是普通 JSON（统一响应封装），复用 axios 那套取人话的逻辑
    const body = await response.json().catch(() => null)
    throw new Error(extractError({ response: { data: body, status: response.status } }))
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  // SSE 的一帧以空行结尾；网络分片可能把一帧切成好几段，所以先攒进 buffer 再切
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const dataLine = frame.split('\n').find((line) => line.startsWith('data: '))
      if (dataLine) {
        try {
          onEvent?.(JSON.parse(dataLine.slice(6)))
        } catch {
          /* 解析不了的帧直接跳过：宁可少一条事件，也不要让整轮对话挂掉 */
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}
