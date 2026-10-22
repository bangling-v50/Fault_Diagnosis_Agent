<script setup>
import DOMPurify from 'dompurify'
import { Marked } from 'marked'
import { computed } from 'vue'

/**
 * 把 Markdown 渲染成 HTML（诊断报告正文就是 Markdown）。
 *
 * **两道防线，缺一不可**：报告正文是**大模型写的**，而模型输出永远不可信——
 * 提示词里塞一句"输出 <script>…"，如果我们直接 v-html，注入就变成 XSS 了。
 *
 * 1. 解析器层面：`renderer.html = () => ''`，把 Markdown 里夹带的原始 HTML 直接丢掉；
 * 2. 输出层面：DOMPurify 再洗一遍，只留安全的 HTML 子集。
 *
 * 这份实现移植自参考前端 `apps/frontend1/src/components/MarkdownContent.vue`
 * （那边是 TS + CSS 变量，这里的仓库没有变量体系，色值改成硬编码）。
 */
const props = defineProps({
  content: { type: String, default: '' },
  // compact：列表/摘要里的简短渲染；report：报告正文（标题层级、表格、引用块都有样式）
  mode: { type: String, default: 'compact' },
})

const parser = new Marked({ breaks: true, gfm: true })
parser.use({ renderer: { html: () => '' } })

const renderedContent = computed(() =>
  DOMPurify.sanitize(parser.parse(props.content || ''), { USE_PROFILES: { html: true } }),
)
</script>

<template>
  <div :class="['markdown-content', `markdown-content--${props.mode}`]" v-html="renderedContent" />
</template>

<style scoped>
.markdown-content :deep(*:first-child) { margin-top: 0; }
.markdown-content :deep(*:last-child) { margin-bottom: 0; }
.markdown-content :deep(p),
.markdown-content :deep(ul),
.markdown-content :deep(ol),
.markdown-content :deep(pre) { margin: 0.7rem 0; }
.markdown-content :deep(h1),
.markdown-content :deep(h2),
.markdown-content :deep(h3) { font-size: 1rem; margin: 1rem 0 0.55rem; }
.markdown-content :deep(ul),
.markdown-content :deep(ol) { padding-left: 1.3rem; }
.markdown-content :deep(code) { background: #f4f4f5; color: #c7254e; font-size: 0.88em; padding: 0.1rem 0.25rem; }
.markdown-content :deep(pre) { background: #282c34; color: #e6e6e6; overflow-x: auto; padding: 0.9rem; }
.markdown-content :deep(pre code) { background: transparent; color: inherit; padding: 0; }
.markdown-content :deep(a) { color: #409eff; }

.markdown-content--report { color: #303133; font-size: 14px; line-height: 1.75; min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.markdown-content--report :deep(h1) { border-bottom: 1px solid #ebeef5; font-size: 20px; line-height: 1.35; margin: 0 0 1.4rem; padding-bottom: 0.7rem; }
.markdown-content--report :deep(h2) { color: #303133; font-size: 16px; line-height: 1.45; margin: 1.8rem 0 0.8rem; }
.markdown-content--report :deep(h3) { color: #606266; font-size: 14px; line-height: 1.5; margin: 1.2rem 0 0.5rem; }
.markdown-content--report :deep(p),
.markdown-content--report :deep(ul),
.markdown-content--report :deep(ol) { margin: 0.65rem 0; }
.markdown-content--report :deep(hr) { border: 0; border-top: 1px solid #ebeef5; margin: 1.6rem 0; }
.markdown-content--report :deep(strong) { color: #303133; font-weight: 700; }
.markdown-content--report :deep(table) { border-collapse: collapse; display: block; font-size: 13px; margin: 1rem 0 1.4rem; max-width: 100%; overflow-x: auto; width: 100%; }
.markdown-content--report :deep(th),
.markdown-content--report :deep(td) { border: 1px solid #ebeef5; min-width: 7rem; padding: 0.55rem 0.7rem; text-align: left; vertical-align: top; }
.markdown-content--report :deep(th) { background: #fafafa; color: #606266; font-weight: 700; white-space: nowrap; }
.markdown-content--report :deep(tr:nth-child(even) td) { background: #fafafa; }
.markdown-content--report :deep(blockquote) { border-left: 3px solid #e6a23c; color: #606266; margin: 1rem 0; padding: 0.2rem 0 0.2rem 0.9rem; }
</style>
