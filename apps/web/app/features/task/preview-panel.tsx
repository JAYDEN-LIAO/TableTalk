import { useState, useEffect, useMemo, useRef } from 'react'
import { FileSpreadsheet, ArrowRight, Loader2, Download, ChevronDown, Check } from 'lucide-react'

import { cn } from '~/lib/utils'
import ExcelPreview from '~/components/excel-preview'
import ExcelIcon from '~/assets/iconify/vscode-icons/file-type-excel.svg?react'

import type { UserMessageAttachment, OutputFileInfo } from '~/components/llm-chat/message-list/types'

export type PreviewTab = 'input' | 'output'

export interface PreviewPanelProps {
  /** 输入文件列表 */
  inputFiles?: UserMessageAttachment[]
  /** 可处理文件列表（历史产出文件，供用户手动选择） */
  processedFiles?: UserMessageAttachment[]
  /** 输出文件列表（仅最终处理结果） */
  outputFiles?: OutputFileInfo[]
  /** 是否正在处理中 */
  isProcessing?: boolean
  /** 当前激活的 Tab */
  activeTab?: PreviewTab
  /** Tab 切换回调 */
  onTabChange?: (tab: PreviewTab) => void
  /** 当前选中的可处理文件 ID 列表（来自下拉多选） */
  selectedProcessingFileIds?: string[]
  /** 可处理文件选中变更回调（多选） */
  onProcessingFilesChange?: (files: UserMessageAttachment[]) => void
}

/** 左侧预览面板组件 */
const PreviewPanel = ({
  inputFiles = [],
  processedFiles = [],
  outputFiles = [],
  isProcessing = false,
  activeTab: controlledTab,
  onTabChange,
  selectedProcessingFileIds = [],
  onProcessingFilesChange,
}: PreviewPanelProps) => {
  // 下拉菜单展开状态
  const [inputDropdownOpen, setInputDropdownOpen] = useState(false)
  const inputDropRef = useRef<HTMLDivElement>(null)

  // 内部 Tab 状态（支持受控和非受控模式）
  const [internalTab, setInternalTab] = useState<PreviewTab>('input')
  const activeTab = controlledTab ?? internalTab
  const handleTabChange = (tab: PreviewTab) => {
    if (onTabChange) {
      onTabChange(tab)
    } else {
      setInternalTab(tab)
    }
    // 切换 Tab 时关闭下拉
    setInputDropdownOpen(false)
  }

  // 关闭下拉当点击外部
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (inputDropRef.current && !inputDropRef.current.contains(e.target as Node)) {
        setInputDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // 选中的输出文件
  const [internalSelectedOutputFileId, setInternalSelectedOutputFileId] = useState<string>()
  // 当前正在预览的文件 ID（用于多选时切换预览）
  const [previewingFileId, setPreviewingFileId] = useState<string | null>(null)

  // 受控/非受控 selectedProcessingFileIds
  const effectiveSelectedIds: string[] =
    selectedProcessingFileIds.length > 0 ? selectedProcessingFileIds : []

  // 合并所有可选项（去重）
  const allSelectableFiles = useMemo(() => {
    const map = new Map<string, UserMessageAttachment>()
    inputFiles.forEach(f => map.set(f.id, f))
    processedFiles.filter(pf => !inputFiles.some(if_ => if_.id === pf.id))
      .forEach(f => map.set(f.id, f))
    return Array.from(map.values())
  }, [inputFiles, processedFiles])

  // 当前选中的可处理文件（多选，用于预览 / 发送）
  const selectedFiles = useMemo(() => {
    return effectiveSelectedIds
      .map(id => allSelectableFiles.find(f => f.id === id))
      .filter((f): f is UserMessageAttachment => f != null)
  }, [effectiveSelectedIds, allSelectableFiles])

  // 当前预览的输入文件（跟随 previewingFileId，无则 fallback 到第一个选中项）
  const currentInputFile = useMemo(() => {
    if (previewingFileId) {
      return allSelectableFiles.find(f => f.id === previewingFileId) ?? selectedFiles[0] ?? null
    }
    return selectedFiles[0] ?? inputFiles[0] ?? null
  }, [previewingFileId, selectedFiles, inputFiles, allSelectableFiles])

  // 当前选中的输出文件
  const effectiveSelectedOutputId = outputFiles.length > 0 && !internalSelectedOutputFileId
    ? outputFiles[0].file_id
    : internalSelectedOutputFileId
  const currentOutputFile = useMemo(() => {
    if (effectiveSelectedOutputId) {
      return outputFiles.find(f => f.file_id === effectiveSelectedOutputId)
    }
    return outputFiles[0]
  }, [effectiveSelectedOutputId, outputFiles])

  // 自动选中第一个输入文件（非受控时）
  useEffect(() => {
    if (inputFiles.length > 0 && effectiveSelectedIds.length === 0) {
      onProcessingFilesChange?.([inputFiles[0]])
    }
  }, [inputFiles, effectiveSelectedIds.length])

  // 当前预览文件不在选中列表时，自动切换到第一个选中项
  useEffect(() => {
    if (previewingFileId && !selectedFiles.some(f => f.id === previewingFileId)) {
      setPreviewingFileId(selectedFiles[0]?.id ?? null)
    }
  }, [previewingFileId, selectedFiles])

  // 自动选择第一个输出文件
  useEffect(() => {
    if (outputFiles.length > 0 && !internalSelectedOutputFileId) {
      setInternalSelectedOutputFileId(outputFiles[0].file_id)
    }
  }, [outputFiles, internalSelectedOutputFileId])

  // 输出文件列表变化时重置选中
  useEffect(() => {
    if (outputFiles.length === 0) {
      setInternalSelectedOutputFileId(undefined)
    } else if (internalSelectedOutputFileId) {
      const exists = outputFiles.some(f => f.file_id === internalSelectedOutputFileId)
      if (!exists) {
        setInternalSelectedOutputFileId(outputFiles[0].file_id)
      }
    }
  }, [outputFiles, internalSelectedOutputFileId])

  // 切换单个文件选中状态
  const toggleFile = (file: UserMessageAttachment) => {
    const isSelected = effectiveSelectedIds.includes(file.id)
    if (isSelected) {
      const nextIds = effectiveSelectedIds.filter(id => id !== file.id)
      const nextFiles = nextIds.map(id => allSelectableFiles.find(f => f.id === id)).filter((f): f is UserMessageAttachment => f != null)
      onProcessingFilesChange?.(nextFiles)
    } else {
      const nextFiles = [...effectiveSelectedIds, file.id].map(id =>
        allSelectableFiles.find(f => f.id === id)
      ).filter((f): f is UserMessageAttachment => f != null)
      onProcessingFilesChange?.(nextFiles)
    }
  }

  const hasInputFiles = inputFiles.length > 0
  const hasOutputFiles = outputFiles.length > 0
  const selectableFileCount = allSelectableFiles.length

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Tab 切换栏 */}
      <div className="flex items-center border-b border-gray-200 bg-linear-to-r from-white to-brand-muted/20">
        {/* 可处理文件 Tab */}
        <div className="relative" ref={inputDropRef}>
          <button
            onClick={() => handleTabChange('input')}
            className={cn(
              "flex items-center gap-2 px-5 py-3.5 text-sm font-medium border-b-2 transition-all",
              activeTab === 'input'
                ? "border-brand text-brand-dark bg-brand-muted/30"
                : "border-transparent text-gray-500 hover:text-gray-700 hover:bg-gray-50/50"
            )}
          >
            <FileSpreadsheet className="w-4 h-4" />
            <span>可处理文件</span>
            {/* 矩形数字徽章（可点击展开下拉） */}
            {selectableFileCount > 0 && (
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  setInputDropdownOpen(v => !v)
                }}
                className={cn(
                  "flex items-center justify-center h-5 px-2 text-xs font-semibold rounded-md cursor-pointer select-none min-w-[2rem]",
                  activeTab === 'input'
                    ? "bg-brand/20 text-brand-dark hover:bg-brand/30"
                    : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                )}
              >
                {effectiveSelectedIds.length > 0 ? `${effectiveSelectedIds.length}/${selectableFileCount}` : selectableFileCount}
                <ChevronDown className="w-3 h-3 ml-0.5" />
              </button>
            )}
          </button>

          {/* 可处理文件下拉（多选） */}
          {inputDropdownOpen && (
            <div className="absolute top-full left-0 z-50 mt-1 w-72 bg-white rounded-xl border border-gray-200 shadow-lg shadow-gray-200/50 overflow-hidden">
              <div className="px-3 py-2 text-xs font-medium text-gray-400 border-b border-gray-100">
                选择处理文件（可多选）
              </div>
              <div className="max-h-60 overflow-y-auto py-1">
                {allSelectableFiles.map((file) => {
                  const isSelected = effectiveSelectedIds.includes(file.id)
                  return (
                    <button
                      key={file.id}
                      onClick={() => toggleFile(file)}
                      className={cn(
                        "w-full flex items-center gap-2.5 px-3 py-2 text-sm text-left hover:bg-gray-50 transition-colors",
                        isSelected && "bg-brand-muted/40"
                      )}
                    >
                      {/* Checkbox 风格选择框 */}
                      <div className={cn(
                        "w-4 h-4 rounded border-2 flex items-center justify-center shrink-0 transition-colors",
                        isSelected
                          ? "bg-brand border-brand"
                          : "border-gray-300 bg-white"
                      )}>
                        {isSelected && <Check className="w-2.5 h-2.5 text-white" />}
                      </div>
                      <ExcelIcon className="w-4 h-4 text-brand shrink-0" />
                      <span className="flex-1 truncate text-gray-700">{file.filename}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        {/* 处理箭头 */}
        <div className="flex items-center px-3">
          <ArrowRight className={cn(
            "w-4 h-4 transition-colors",
            isProcessing ? "text-brand animate-pulse" : "text-gray-300"
          )} />
        </div>

        {/* 处理结果 Tab */}
        <button
          onClick={() => hasOutputFiles && handleTabChange('output')}
          disabled={!hasOutputFiles}
          className={cn(
            "flex items-center gap-2 px-5 py-3.5 text-sm font-medium border-b-2 transition-all",
            activeTab === 'output'
              ? "border-brand text-brand-dark bg-brand-muted/30"
              : "border-transparent text-gray-500 hover:text-gray-700 hover:bg-gray-50/50",
            !hasOutputFiles && "opacity-40 cursor-not-allowed"
          )}
        >
          <FileSpreadsheet className="w-4 h-4" />
          <span>处理结果</span>
          {isProcessing && (
            <Loader2 className="w-3.5 h-3.5 text-brand animate-spin" />
          )}
          {/* 矩形数字徽章（无下拉，仅展示数量） */}
          {hasOutputFiles && !isProcessing && (
            <span className={cn(
              "flex items-center justify-center h-5 px-2 text-xs font-semibold rounded-md",
              activeTab === 'output'
                ? "bg-brand/20 text-brand-dark"
                : "bg-gray-100 text-gray-500"
            )}>
              {outputFiles.length}
            </span>
          )}
        </button>
      </div>

      {/* 输入文件 Tab 内容 */}
      {activeTab === 'input' && (
        <>
          {/* 当前选中的文件标签（点击可切换预览） */}
          {selectedFiles.length > 0 && (
            <div className="flex items-center gap-1.5 px-3 py-2 border-b border-gray-100 bg-gray-50/50 overflow-x-auto shrink-0">
              {selectedFiles.map((file) => {
                const isPreviewing = previewingFileId === file.id || (!previewingFileId && file.id === selectedFiles[0]?.id)
                return (
                  <button
                    key={file.id}
                    onClick={() => setPreviewingFileId(file.id)}
                    className={cn(
                      "inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border whitespace-nowrap transition-all",
                      isPreviewing
                        ? "bg-brand-muted text-brand-dark border-brand/30 shadow-sm"
                        : "bg-white text-gray-500 border-gray-200 hover:border-brand/30 hover:text-brand-dark"
                    )}
                  >
                    <ExcelIcon className="w-3.5 h-3.5 shrink-0" />
                    <span className="max-w-[120px] truncate">{file.filename}</span>
                  </button>
                )
              })}
            </div>
          )}

          {/* 预览区域 */}
          <div className="flex-1 overflow-hidden bg-linear-to-br from-white to-gray-50/50">
            {currentInputFile ? (
              <ExcelPreview
                className="w-full h-full"
                fileUrl={currentInputFile.path}
              />
            ) : (
              <EmptyState
                icon={<FileSpreadsheet className="w-12 h-12" />}
                title="暂无输入文件"
                description="请上传或从下拉选择 Excel 文件"
              />
            )}
          </div>
        </>
      )}

      {/* 输出结果 Tab 内容 */}
      {activeTab === 'output' && (
        <>
          {/* 输出文件选择器（多文件时显示） */}
          {outputFiles.length > 1 && (
            <div className="flex items-center gap-1 px-3 py-2 border-b border-gray-100 bg-gray-50/50 overflow-x-auto">
              {outputFiles.map((file) => (
                <button
                  key={file.file_id}
                  onClick={() => setInternalSelectedOutputFileId(file.file_id)}
                  className={cn(
                    "flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-all whitespace-nowrap",
                    effectiveSelectedOutputId === file.file_id
                      ? "bg-brand-muted text-brand-dark"
                      : "text-gray-600 hover:bg-gray-100"
                  )}
                >
                  <FileSpreadsheet className="w-3.5 h-3.5" />
                  <span className="max-w-30 truncate">{file.filename}</span>
                </button>
              ))}
            </div>
          )}

          {/* 预览区域 */}
          <div className="flex-1 overflow-hidden bg-linear-to-br from-white to-brand-muted/10">
            {currentOutputFile ? (
              <ExcelPreview
                className="w-full h-full"
                fileUrl={currentOutputFile.url}
              />
            ) : (
              <EmptyState
                icon={<FileSpreadsheet className="w-12 h-12" />}
                title="暂无处理结果"
                description={isProcessing ? "正在处理中..." : "提交任务后将显示处理结果"}
              />
            )}
          </div>
        </>
      )}
    </div>
  )
}

/** 空状态组件 */
const EmptyState = ({
  icon,
  title,
  description
}: {
  icon: React.ReactNode
  title: string
  description: string
}) => (
  <div className="flex flex-col items-center justify-center h-full text-gray-400">
    <div className="mb-3 opacity-40">{icon}</div>
    <p className="text-sm font-medium text-gray-500">{title}</p>
    <p className="text-xs text-gray-400 mt-1">{description}</p>
  </div>
)

export default PreviewPanel
