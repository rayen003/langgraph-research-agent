import type { DocumentInfo } from '../types'

interface Props {
  doc: DocumentInfo
  onClose: () => void
}

const PDF_RE = /\.pdf$/i
const TEXT_RE = /\.(txt|md|csv)$/i
const OFFICE_RE = /\.(docx?|xlsx?|pptx?)$/i

export function DocumentPreview({ doc, onClose }: Props) {
  const fileUrl = `/documents/${doc.doc_id}/file`
  const isPdf = PDF_RE.test(doc.filename)
  const isText = TEXT_RE.test(doc.filename)
  const isOffice = OFFICE_RE.test(doc.filename)

  return (
    <div className="w-full h-full flex flex-col bg-[#0a0a0a] border-l border-[#141414]">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#141414] flex-shrink-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="flex-shrink-0 text-indigo-400">
            <path d="M2.5 1h6L11.5 4v8.5h-9V1z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            <path d="M8 1v3.5h3.5" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
          </svg>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-zinc-100 truncate">{doc.filename}</h3>
            <p className="text-[11px] text-zinc-600">
              {doc.page_count} page{doc.page_count !== 1 ? 's' : ''} · {doc.chunk_count} chunks
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <a
            href={fileUrl}
            download={doc.filename}
            title="Download"
            className="w-7 h-7 flex items-center justify-center rounded-md text-zinc-500 hover:text-zinc-200 hover:bg-[#1a1a22] transition-colors"
          >
            <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
              <path d="M2 9.5V11h9V9.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M6.5 9V2M4 6.5L6.5 9 9 6.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </a>
          <button
            onClick={onClose}
            title="Close"
            className="w-7 h-7 flex items-center justify-center rounded-md text-zinc-500 hover:text-zinc-200 hover:bg-[#1a1a22] transition-colors"
          >
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
              <path d="M2 2l7 7M9 2l-7 7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-hidden bg-[#080808]">
        {isPdf ? (
          <iframe
            src={`${fileUrl}#toolbar=0&navpanes=0`}
            title={doc.filename}
            className="w-full h-full bg-white"
          />
        ) : isText ? (
          <TextPreview url={fileUrl} />
        ) : isOffice ? (
          <NoPreview type="office" filename={doc.filename} downloadUrl={fileUrl} />
        ) : (
          <NoPreview type="generic" filename={doc.filename} downloadUrl={fileUrl} />
        )}
      </div>
    </div>
  )
}

function TextPreview({ url }: { url: string }) {
  return (
    <iframe
      src={url}
      title="Text preview"
      className="w-full h-full bg-[#0d0d10] text-zinc-200"
    />
  )
}

function NoPreview({
  type,
  filename,
  downloadUrl,
}: {
  type: 'office' | 'generic'
  filename: string
  downloadUrl: string
}) {
  const message = type === 'office'
    ? 'Inline preview is not available for Office documents. The agent can still search the contents — or download to view.'
    : 'No inline preview for this file type. The agent has indexed the contents — or download the original.'

  return (
    <div className="h-full flex flex-col items-center justify-center px-8 text-center space-y-4">
      <div className="w-12 h-12 rounded-xl bg-[#111118] border border-[#1e1e2a] flex items-center justify-center">
        <svg width="20" height="20" viewBox="0 0 14 14" fill="none" className="text-zinc-500">
          <path d="M2.5 1h6L11.5 4v8.5h-9V1z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
          <path d="M8 1v3.5h3.5" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
        </svg>
      </div>
      <p className="text-sm text-zinc-300 font-medium max-w-xs">{filename}</p>
      <p className="text-xs text-zinc-500 max-w-xs leading-relaxed">{message}</p>
      <a
        href={downloadUrl}
        download={filename}
        className="px-3 py-1.5 rounded-md border border-[#252535] text-xs text-zinc-300 hover:text-zinc-100 hover:border-[#33334a] transition-colors"
      >
        Download
      </a>
    </div>
  )
}
