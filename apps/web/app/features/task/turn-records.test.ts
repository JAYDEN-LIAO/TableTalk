import test from 'node:test'
import assert from 'node:assert/strict'

import { extractRecordBlocks, extractReplaySteps, extractResponseText } from './turn-records'

import type { ThreadTurn } from '~/lib/api'

test('extractReplaySteps keeps only tool_result records as replay steps', () => {
  const turn = {
    id: 'turn-1',
    turn_number: 1,
    user_query: '处理文件',
    status: 'completed',
    response_text: '处理完成',
    steps: [
      { type: 'tool_call', tool_name: 'processing_workflow', status: 'done' },
      { type: 'reasoning', text: '先处理再总结', status: 'done' },
      { type: 'tool_result', step: 'execute', status: 'done', output: { success: true } },
    ],
    created_at: '2026-04-12T00:00:00Z',
    completed_at: '2026-04-12T00:00:01Z',
  } satisfies ThreadTurn

  const steps = extractReplaySteps(turn.steps)
  assert.equal(steps.length, 1)
  assert.equal(steps[0].step, 'execute')
})

test('extractRecordBlocks returns text, reasoning and tool_call blocks', () => {
  const steps = [
    { type: 'text', text: '最终总结', status: 'done' },
    { type: 'reasoning', text: '我先分析字段', status: 'done' },
    { type: 'tool_call', tool_name: 'analysis_workflow', arguments: { intent: 'analysis' }, status: 'done' },
    { type: 'tool_result', step: 'execute', status: 'done', output: { success: true } },
  ]

  const blocks = extractRecordBlocks(steps as any)
  assert.deepEqual(
    blocks.map((block) => block.type),
    ['text', 'reasoning', 'tool_call'],
  )
  assert.equal(blocks[2].toolName, 'analysis_workflow')
})

test('extractResponseText prefers standalone response_text when there is no chat step', () => {
  const turn = {
    id: 'turn-1',
    turn_number: 1,
    user_query: '你好',
    status: 'completed',
    response_text: '最终回复',
    steps: [
      { type: 'tool_result', step: 'execute', status: 'done', output: { success: true } },
    ],
    created_at: '2026-04-12T00:00:00Z',
    completed_at: '2026-04-12T00:00:01Z',
  } satisfies ThreadTurn

  assert.equal(extractResponseText(turn), '最终回复')
})
