import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronUp,
  Circle,
  ClipboardList,
  Database,
  Eye,
  EyeOff,
  FileText,
  Globe,
  Loader2,
  Search,
  Send,
  X,
} from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import Markdown from 'react-markdown';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

type Status = 'pending' | 'running' | 'completed' | 'failed';
type ToolType = 'command' | 'script' | 'file' | 'search' | 'database';
type AppState = 'idle' | 'planning' | 'reviewing' | 'executing' | 'completed' | 'error';

interface ToolCall {
  id: string;
  name: string;
  type: ToolType;
  status: 'running' | 'done' | 'error';
  argsPreview?: string;
  summary?: string;
  result?: string;
  toolResultId?: string;
}

interface ResearchStep {
  id: string;
  title: string;
  description: string;
  dependencies: string[];
  status: Status;
  toolCalls: ToolCall[];
  reasoning: string;
  isExpanded: boolean;
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

interface BackendPlanStep {
  id: string;
  description: string;
  depends_on: string[];
  status: string;
  result?: string | null;
  tool_result_ids?: string[];
}

interface CreateRunResponse {
  thread_id: string;
  plan: {
    plan_id: string;
    query: string;
    status: string;
    created_at: string;
    steps: BackendPlanStep[];
  };
}

function toolTypeFromName(name: string): ToolType {
  if (name === 'search_web' || name === 'fetch_url') return 'search';
  if (name === 'execute_python') return 'script';
  if (name === 'retrieve_context' || name === 'retrieve_tool_result') return 'database';
  if (name === 'calculator') return 'command';
  return 'file';
}

function toolKindLabel(type: ToolType): string {
  if (type === 'command') return 'Terminal';
  if (type === 'script') return 'Script';
  if (type === 'search') return 'Search';
  if (type === 'database') return 'Retrieval';
  return 'File';
}

function stepsFromPlan(steps: BackendPlanStep[]): ResearchStep[] {
  return steps.map((step) => ({
    id: step.id,
    title: step.description,
    description: step.description,
    dependencies: step.depends_on ?? [],
    status: 'pending',
    toolCalls: [],
    reasoning: '',
    isExpanded: true,
  }));
}

function artifactUrl(threadId: string | null, artifactPath: string): string {
  const filename = artifactPath.split('/').pop() ?? artifactPath;
  return `/artifacts/${threadId}/${filename}`;
}

function splitReportForArtifacts(content: string): [string, string] {
  const stripped = content.trim();
  if (!stripped) return ['', ''];

  for (const marker of ['[ARTIFACTS]', '[ARTIFACT]', '[CHART]']) {
    if (stripped.includes(marker)) {
      const [before, after] = stripped.split(marker, 2);
      return [before.trimEnd(), after.trimStart()];
    }
  }

  const lines = stripped.split('\n');
  for (let idx = 0; idx < lines.length; idx += 1) {
    const normalized = lines[idx].trim().replace(/^#+/, '').trim().toLowerCase().replace(/:$/, '');
    if (normalized.startsWith('limitations')) {
      return [lines.slice(0, idx).join('\n').trimEnd(), lines.slice(idx).join('\n').trimStart()];
    }
  }

  return [stripped, ''];
}

function removeArtifactMarkers(text: string): string {
  return text.replaceAll('[ARTIFACTS]', '').replaceAll('[ARTIFACT]', '').replaceAll('[CHART]', '').trim();
}

const ToolIcon = ({ type }: { type: ToolType }) => {
  switch (type) {
    case 'command':
      return (
        <div className="flex h-5 w-5 items-center justify-center rounded border border-gray-700 bg-gray-800">
          <span className="font-mono text-[10px] text-gray-400">$_</span>
        </div>
      );
    case 'script':
      return (
        <div className="flex h-5 w-5 items-center justify-center rounded border border-amber-500/20 bg-amber-500/10">
          <span className="text-[10px] font-bold text-amber-500">PY</span>
        </div>
      );
    case 'search':
      return <Globe size={14} className="text-blue-500" />;
    case 'database':
      return <Database size={14} className="text-emerald-500" />;
    default:
      return <FileText size={14} className="text-gray-500" />;
  }
};

const ToolCallItem = ({ tool, isLast }: { tool: ToolCall; isLast: boolean }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const detail = tool.result ?? tool.summary ?? '';

  return (
    <div className="relative ml-2 flex gap-4">
      {!isLast && <div className="absolute bottom-[-20px] left-[10px] top-6 w-[1px] bg-gray-800" />}

      <div className="relative z-10 mt-1">
        <ToolIcon type={tool.type} />
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1 pb-6">
        <button
          type="button"
          className="group flex flex-col items-start text-left"
          onClick={() => detail && setIsExpanded((value) => !value)}
        >
          <span className={cn(
            'truncate text-[14px] transition-colors',
            tool.status === 'running' ? 'text-blue-400' : 'text-gray-300 group-hover:text-white',
            tool.status === 'error' && 'text-red-400',
          )}>
            {tool.name}
          </span>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="rounded border border-gray-800 bg-gray-800/50 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">
              {toolKindLabel(tool.type)}
            </span>
            {tool.status === 'running' && (
              <span className="flex items-center gap-1 text-[10px] text-blue-400">
                <div className="h-1.5 w-1.5 rounded-full bg-blue-500 animate-pulse" />
                Running
              </span>
            )}
            {tool.status === 'done' && (
              <span className="flex items-center gap-1 text-[10px] text-emerald-500/80">
                <Check size={10} />
                Done
              </span>
            )}
            {tool.status === 'error' && (
              <span className="text-[10px] text-red-400">Error</span>
            )}
          </div>
        </button>

        {tool.summary && (
          <div className="mt-1 text-[12px] leading-relaxed text-gray-500">
            {tool.summary}
          </div>
        )}

        {detail && isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            className="mt-2 max-h-[300px] overflow-x-auto rounded-lg border border-gray-800 bg-[#1a1a1a] p-3 font-mono text-[12px] whitespace-pre-wrap text-gray-500 shadow-inner"
          >
            <div className="mb-2 flex items-center gap-2 border-b border-gray-800/50 pb-2 text-[10px] font-bold uppercase tracking-widest text-gray-600">
              <Database size={10} />
              Tool details
            </div>
            {tool.argsPreview && <div>Args: {tool.argsPreview}{'\n\n'}</div>}
            {tool.toolResultId && <div>tool_result_id: {tool.toolResultId}{'\n\n'}</div>}
            {detail}
          </motion.div>
        )}
      </div>
    </div>
  );
};

const StepCard = ({
  step,
  isActive,
  onToggle,
}: {
  step: ResearchStep;
  isActive: boolean;
  onToggle: () => void;
}) => (
  <div
    className={cn(
      'relative flex flex-col gap-3 rounded-xl border p-5 transition-all duration-500',
      isActive ? 'border-l-[4px] border-blue-500/50 bg-[#2a2a2a] shadow-lg shadow-blue-500/5' : 'border-gray-800 bg-[#252525]',
      step.status === 'pending' && 'grayscale-[0.5] opacity-50',
    )}
  >
    <button type="button" className="group flex items-start gap-4 text-left" onClick={onToggle}>
      <div className="mt-1 shrink-0">
        {step.status === 'completed' ? (
          <div className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/10">
            <Check size={12} className="text-emerald-500" strokeWidth={3} />
          </div>
        ) : step.status === 'running' ? (
          <Loader2 size={18} className="animate-spin text-blue-500" />
        ) : step.status === 'failed' ? (
          <div className="flex h-5 w-5 items-center justify-center rounded-full bg-red-500/10 text-red-400">!</div>
        ) : (
          <div className="h-5 w-5 rounded-full border-2 border-gray-700" />
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-3">
          <h3
            className={cn(
              'text-[16px] font-medium tracking-tight transition-colors',
              step.status === 'completed' ? 'text-gray-400' : 'text-white',
              isActive && 'text-blue-400',
            )}
          >
            {step.title}
          </h3>
          {step.dependencies.length > 0 && (
            <span className="rounded border border-gray-700 bg-gray-800 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-gray-500">
              dep: {step.dependencies.join(', ')}
            </span>
          )}
        </div>

        <AnimatePresence initial={false}>
          {step.isExpanded && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}>
              {!!step.reasoning && (
                <p className="mt-2 text-[13px] leading-relaxed text-gray-500">
                  {step.reasoning}
                </p>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className="text-gray-600 transition-colors group-hover:text-gray-400">
        {step.isExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </div>
    </button>

    {(step.status === 'running' || step.status === 'completed' || step.status === 'failed') && step.toolCalls.length > 0 && (
      <div className="mt-2 flex flex-col gap-0 border-t border-gray-800/50 pt-4">
        {step.toolCalls.map((tool, idx) => (
          <ToolCallItem key={tool.id} tool={tool} isLast={idx === step.toolCalls.length - 1} />
        ))}
      </div>
    )}
  </div>
);

export default function App() {
  const [appState, setAppState] = useState<AppState>('idle');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [steps, setSteps] = useState<ResearchStep[]>([]);
  const [hideReasoning, setHideReasoning] = useState(false);
  const [finalReport, setFinalReport] = useState('');
  const [artifactPaths, setArtifactPaths] = useState<string[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [planStepCount, setPlanStepCount] = useState(0);
  const [isReasoningCollapsed, setIsReasoningCollapsed] = useState(false);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);

  const scrollRef = useRef<HTMLDivElement>(null);
  const prevMessagesLength = useRef(0);
  const prevStepsLength = useRef(0);
  const eventSourceRef = useRef<EventSource | null>(null);

  const completedCount = useMemo(
    () => steps.filter((step) => step.status === 'completed').length,
    [steps],
  );
  const canSubmit = appState === 'idle' || appState === 'completed' || appState === 'error';

  const reportParts = useMemo(() => {
    const [before, after] = splitReportForArtifacts(finalReport);
    return {
      before: removeArtifactMarkers(before),
      after: removeArtifactMarkers(after),
    };
  }, [finalReport]);

  useEffect(() => () => {
    eventSourceRef.current?.close();
  }, []);

  useEffect(() => {
    if (!scrollRef.current) return;

    const isNewMessage = messages.length > prevMessagesLength.current;
    const isNewStep = steps.length > prevStepsLength.current;

    if (isNewMessage || isNewStep) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      setShouldAutoScroll(true);
    } else if (shouldAutoScroll) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }

    prevMessagesLength.current = messages.length;
    prevStepsLength.current = steps.length;
  }, [messages, steps, appState, finalReport, shouldAutoScroll]);

  const onScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    setShouldAutoScroll(scrollHeight - scrollTop - clientHeight < 100);
  };

  const appendAssistantMessage = (content: string) => {
    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: 'assistant', content }]);
  };

  const resetRunState = () => {
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    setSteps([]);
    setFinalReport('');
    setArtifactPaths([]);
    setThreadId(null);
    setPlanStepCount(0);
    setIsReasoningCollapsed(false);
    setHideReasoning(false);
  };

  const handleBackendEvent = (event: Record<string, unknown>) => {
    const eventType = String(event.type ?? '');

    if (eventType === 'step_start') {
      const stepId = String(event.step_id ?? '');
      setSteps((prev) =>
        prev.map((step) =>
          step.id === stepId ? { ...step, status: 'running', isExpanded: true } : step,
        ),
      );
      return;
    }

    if (eventType === 'step_reasoning') {
      const stepId = String(event.step_id ?? '');
      const text = String(event.text ?? '');
      setSteps((prev) =>
        prev.map((step) => (step.id === stepId ? { ...step, reasoning: text } : step)),
      );
      return;
    }

    if (eventType === 'tool_call_start') {
      const stepId = String(event.step_id ?? '');
      const toolName = String(event.tool_name ?? 'tool');
      const argsPreview = String(event.args_preview ?? '');
      setSteps((prev) =>
        prev.map((step) =>
          step.id === stepId
            ? {
                ...step,
                toolCalls: [
                  ...step.toolCalls,
                  {
                    id: crypto.randomUUID(),
                    name: toolName,
                    type: toolTypeFromName(toolName),
                    status: 'running',
                    argsPreview,
                  },
                ],
              }
            : step,
        ),
      );
      return;
    }

    if (eventType === 'tool_call_end' || eventType === 'tool_error') {
      const stepId = String(event.step_id ?? '');
      const toolName = String(event.tool_name ?? '');
      const summary = String(event.summary ?? event.error ?? '');
      const toolResultId = String(event.tool_result_id ?? '');
      setSteps((prev) =>
        prev.map((step) => {
          if (step.id !== stepId) return step;
          let updated = false;
          const toolCalls = [...step.toolCalls];
          for (let idx = toolCalls.length - 1; idx >= 0; idx -= 1) {
            const tool = toolCalls[idx];
            if (tool.name === toolName && tool.status === 'running') {
              toolCalls[idx] = {
                ...tool,
                status: eventType === 'tool_error' ? 'error' : 'done',
                summary,
                result: summary,
                toolResultId: toolResultId || tool.toolResultId,
              };
              updated = true;
              break;
            }
          }
          if (!updated) return step;
          return { ...step, toolCalls };
        }),
      );
      return;
    }

    if (eventType === 'step_complete') {
      const stepId = String(event.step_id ?? '');
      setSteps((prev) =>
        prev.map((step) =>
          step.id === stepId ? { ...step, status: 'completed', isExpanded: false } : step,
        ),
      );
      return;
    }

    if (eventType === 'synthesis_start') {
      setAppState('completed');
      setIsReasoningCollapsed(true);
      setFinalReport('');
      return;
    }

    if (eventType === 'synthesis_token') {
      setFinalReport((prev) => prev + String(event.token ?? ''));
      return;
    }

    if (eventType === 'synthesis_complete') {
      setAppState('completed');
      setArtifactPaths(Array.isArray(event.artifact_paths) ? (event.artifact_paths as string[]) : []);
      if (typeof event.content === 'string') {
        setFinalReport(event.content);
      }
      return;
    }

    if (eventType === 'run_error') {
      setAppState('error');
      appendAssistantMessage(String(event.error ?? 'Unknown execution error.'));
      eventSourceRef.current?.close();
      return;
    }

    if (eventType === 'run_complete') {
      eventSourceRef.current?.close();
    }
  };

  const handleSend = async () => {
    if (!input.trim() || !canSubmit) return;

    const userQuery = input.trim();
    resetRunState();
    setInput('');
    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: 'user', content: userQuery }]);
    setAppState('planning');

    try {
      const response = await fetch('/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: userQuery }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }

      const data = (await response.json()) as CreateRunResponse;
      setThreadId(data.thread_id);
      setSteps(stepsFromPlan(data.plan.steps));
      setPlanStepCount(data.plan.steps.length);
      setAppState('reviewing');
    } catch (error) {
      console.error('Planning error:', error);
      setAppState('error');
      appendAssistantMessage('Could not generate a plan. Check the backend server and try again.');
    }
  };

  const approvePlan = async () => {
    if (!threadId) return;

    setAppState('executing');
    setFinalReport('');
    setArtifactPaths([]);

    const source = new EventSource(`/runs/${threadId}/events`);
    eventSourceRef.current = source;
    source.onmessage = (message) => {
      try {
        handleBackendEvent(JSON.parse(message.data));
      } catch (error) {
        console.error('Event parse error:', error);
      }
    };
    source.onerror = () => {
      source.close();
    };

    try {
      const response = await fetch(`/runs/${threadId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'yes' }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
    } catch (error) {
      console.error('Resume error:', error);
      source.close();
      setAppState('error');
      appendAssistantMessage('Could not start execution. Check the backend server and try again.');
    }
  };

  const toggleStepExpansion = (id: string) => {
    setSteps((prev) =>
      prev.map((step) => (step.id === id ? { ...step, isExpanded: !step.isExpanded } : step)),
    );
  };

  return (
    <div className="flex h-screen flex-col bg-[#1a1a1a] font-sans text-gray-200 selection:bg-blue-500/30">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-gray-800 bg-[#1a1a1a]/80 px-6 py-4 backdrop-blur-md">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-600">
            <Search size={18} className="text-white" />
          </div>
          <h1 className="font-display text-lg font-bold tracking-tight text-white">Research Agent</h1>
        </div>

        {(appState === 'executing' || appState === 'completed') && (
          <button
            type="button"
            onClick={() => setHideReasoning((value) => !value)}
            className="flex items-center gap-2 rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-400 transition-all hover:bg-gray-800 hover:text-white"
          >
            {hideReasoning ? <Eye size={14} /> : <EyeOff size={14} />}
            {hideReasoning ? 'Show reasoning' : 'Hide reasoning'}
          </button>
        )}
      </header>

      <main
        ref={scrollRef}
        onScroll={onScroll}
        className="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-8 overflow-y-auto scroll-smooth px-6 py-8"
      >
        {messages.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center opacity-80">
            <div className="mb-2 flex h-16 w-16 items-center justify-center rounded-2xl bg-gray-800">
              <ClipboardList size={32} className="text-gray-400" />
            </div>
            <h2 className="font-display text-2xl font-bold text-white">Deep Research Assistant</h2>
            <p className="max-w-md text-gray-400">
              Send me a research question and I&apos;ll build an execution plan for your approval before running it.
            </p>
          </div>
        )}

        {messages.map((message) => (
          <div key={message.id} className={cn('flex flex-col gap-2', message.role === 'user' ? 'items-end' : 'items-start')}>
            <div
              className={cn(
                'max-w-[85%] rounded-2xl px-4 py-2.5 text-[15px] leading-relaxed',
                message.role === 'user'
                  ? 'rounded-tr-none bg-blue-600 text-white'
                  : 'rounded-tl-none border border-gray-800 bg-[#2a2a2a] text-gray-200',
              )}
            >
              {message.content}
            </div>
          </div>
        ))}

        {appState === 'reviewing' && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="w-full max-w-3xl self-start rounded-lg border border-gray-800 bg-[#262626] p-4 shadow-xl"
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm">📋</span>
                <h3 className="font-display text-sm font-semibold text-white">
                  Execution Plan · {planStepCount} steps
                </h3>
              </div>
              <span className="rounded border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-500">
                Draft
              </span>
            </div>

            <div className="mt-3 flex flex-col gap-2">
              {steps.map((step, idx) => (
                <div key={step.id} className="flex gap-3 rounded-md border border-gray-800 bg-[#1a1a1a] p-2.5">
                  <span className="mt-0.5 font-mono text-[12px] text-gray-600">{idx + 1}.</span>
                  <div className="flex flex-col gap-1">
                    <h4 className="text-[13px] font-medium text-gray-200">{step.title}</h4>
                    {step.dependencies.length > 0 && (
                      <div className="mt-0.5 flex items-center gap-1.5">
                        <ArrowRight size={10} className="text-gray-600" />
                        <span className="text-[9px] font-bold uppercase text-gray-600">
                          Depends on {step.dependencies.join(', ')}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-3">
              <button
                type="button"
                onClick={approvePlan}
                className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-blue-600 py-2 text-sm font-medium text-white transition-all hover:bg-blue-700"
              >
                <Check size={16} />
                Approve & Execute
              </button>
              <button
                type="button"
                onClick={() => {
                  resetRunState();
                  setAppState('idle');
                }}
                className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-300 transition-all hover:bg-gray-700"
              >
                <X size={16} />
              </button>
            </div>
          </motion.div>
        )}

        {(appState === 'executing' || appState === 'completed') && !hideReasoning && (
          <div className="flex flex-col gap-6">
            <button
              type="button"
              className="group flex items-center justify-between"
              onClick={() => setIsReasoningCollapsed((value) => !value)}
            >
              <div className="flex items-center gap-2 text-gray-400 transition-colors group-hover:text-gray-200">
                <Eye size={16} />
                <span className="text-sm font-medium">
                  {appState === 'completed' ? `Reasoning (${steps.length} steps completed)` : 'Executing research plan...'}
                </span>
              </div>
              <div className="flex items-center gap-4">
                {appState === 'executing' && steps.length > 0 && (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-32 overflow-hidden rounded-full bg-gray-800">
                      <motion.div
                        className="h-full bg-blue-500"
                        initial={{ width: 0 }}
                        animate={{ width: `${(completedCount / steps.length) * 100}%` }}
                      />
                    </div>
                    <span className="font-mono text-[12px] text-gray-500">
                      {completedCount}/{steps.length}
                    </span>
                  </div>
                )}
                {isReasoningCollapsed ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
              </div>
            </button>

            <AnimatePresence>
              {!isReasoningCollapsed && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  className="flex flex-col gap-6 overflow-hidden"
                >
                  {steps.map((step) => (
                    <StepCard
                      key={step.id}
                      step={step}
                      isActive={step.status === 'running'}
                      onToggle={() => toggleStepExpansion(step.id)}
                    />
                  ))}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {appState === 'completed' && finalReport && (
          <motion.div
            initial={{ opacity: 0, scale: 0.98 }}
            animate={{ opacity: 1, scale: 1 }}
            className="flex flex-col gap-6 rounded-xl border border-gray-800 bg-[#2a2a2a] p-8 shadow-2xl"
          >
            <div className="border-b border-gray-800 pb-4">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-600/20 text-emerald-500">
                  <FileText size={20} />
                </div>
                <h2 className="font-display text-xl font-bold text-white">Final Research Report</h2>
              </div>
            </div>

            {reportParts.before && (
              <div className="prose prose-invert prose-sm max-w-none leading-relaxed text-gray-300">
                <Markdown>{reportParts.before}</Markdown>
              </div>
            )}

            {threadId && artifactPaths.length > 0 && (
              <div className="flex flex-col gap-4">
                {artifactPaths.map((path) => (
                  <img
                    key={path}
                    src={artifactUrl(threadId, path)}
                    alt={path}
                    className="mx-auto max-h-[520px] w-auto max-w-full rounded-xl border border-gray-800 bg-[#1a1a1a] p-2"
                  />
                ))}
              </div>
            )}

            {reportParts.after && (
              <div className="prose prose-invert prose-sm max-w-none leading-relaxed text-gray-300">
                <Markdown>{reportParts.after}</Markdown>
              </div>
            )}
          </motion.div>
        )}

        {appState === 'planning' && (
          <div className="flex items-center gap-3 text-gray-500 animate-pulse">
            <Loader2 size={18} className="animate-spin" />
            <span className="text-sm font-medium">Generating research plan...</span>
          </div>
        )}
      </main>

      <footer className="border-t border-gray-800 bg-[#1a1a1a] p-6">
        <div className="relative mx-auto max-w-4xl">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask a research question..."
            disabled={!canSubmit}
            className="min-h-[56px] w-full resize-none rounded-xl border border-gray-800 bg-[#2a2a2a] px-4 py-3 pr-12 text-[15px] text-white placeholder-gray-500 transition-all focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50 disabled:opacity-80"
            rows={1}
          />
          <button
            type="button"
            onClick={handleSend}
            disabled={!input.trim() || !canSubmit}
            className="absolute bottom-3 right-3 rounded-lg bg-blue-600 p-2 text-white transition-all hover:bg-blue-700 disabled:opacity-50 disabled:hover:bg-blue-600"
          >
            <Send size={18} />
          </button>
        </div>
        <p className="mt-3 text-center text-[11px] text-gray-600">
          Research Agent can make mistakes. Verify important information.
        </p>
      </footer>
    </div>
  );
}
