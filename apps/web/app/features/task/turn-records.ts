import type { ThreadTurn, ThreadTurnStep } from '~/lib/api'
import type { StepError, StepName, StepRecord } from '~/components/llm-chat/message-list/types'

export interface RecordBlock {
  type: 'text' | 'reasoning' | 'tool_call'
  text?: string
  toolName?: string
  arguments?: Record<string, unknown>
  createdAt?: string
}

function getRecordText(step: ThreadTurnStep): string | undefined {
  if (typeof step.text === 'string') return step.text
  if (typeof step.output === 'string') return step.output
  return undefined
}

export function extractReplaySteps(steps: ThreadTurnStep[]): StepRecord[] {
  return steps
    .filter((step) => !step.type || step.type === 'tool_result')
    .map((step) => {
      const baseStep = {
        step: (step.step || 'execute') as StepName,
        started_at: step.started_at,
        completed_at: step.completed_at,
      }

      if (step.status === 'done' && step.output !== undefined) {
        return { ...baseStep, status: 'done' as const, output: step.output }
      }
      if (step.status === 'error' && step.error) {
        return { ...baseStep, status: 'error' as const, error: step.error as StepError }
      }
      if (step.status === 'streaming') {
        return { ...baseStep, status: 'streaming' as const }
      }
      return { ...baseStep, status: 'running' as const }
    }) as StepRecord[]
}

export function extractRecordBlocks(steps: ThreadTurnStep[]): RecordBlock[] {
  const blocks: RecordBlock[] = []

  for (const step of steps) {
    if (step.type === 'text') {
      const text = getRecordText(step)
      if (text) blocks.push({ type: 'text', text, createdAt: step.created_at })
      continue
    }

    if (step.type === 'reasoning') {
      const text = getRecordText(step)
      if (text) blocks.push({ type: 'reasoning', text, createdAt: step.created_at })
      continue
    }

    if (step.type === 'tool_call') {
      blocks.push({
        type: 'tool_call',
        toolName: step.tool_name,
        arguments: step.arguments,
        createdAt: step.created_at,
      })
    }
  }

  return blocks
}

export function hasChatStep(steps: ThreadTurnStep[]): boolean {
  return steps.some((step) => step.step === 'chat')
}

export function extractResponseText(turn: ThreadTurn): string | undefined {
  return hasChatStep(turn.steps) ? undefined : (turn.response_text ?? undefined)
}
