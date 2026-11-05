'use client';

import { useEffect, useRef, useState } from 'react';

type Message = {
  role: 'user' | 'assistant';
  text: string;
  status?: 'success' | 'blocked' | 'ambiguous' | 'error' | 'guidance';
  sql?: string;
  results?: Record<string, unknown>[];
  retryText?: string;
};

type RecorderState = 'idle' | 'recording' | 'recorded';

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
const starterQuestions = ['Total revenue by region', 'Which products sell the most?', 'How many completed orders this month?'];

function isClearlyOffTopic(text: string) {
  const normalized = text.trim().toLowerCase();
  return [/^(hi|hello|hey|yo|sup)[!. ]*$/, /\b(weather|joke|poem|recipe|movie|song|politics|cricket score|football score)\b/, /\b(who are you|what can you do|help me with homework)\b/]
    .some((pattern) => pattern.test(normalized));
}

function MicrophoneIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="size-5"><rect x="8.5" y="3" width="7" height="11" rx="3.5" /><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M8.5 21h7" strokeLinecap="round" /></svg>;
}

function SendIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="size-4"><path d="m21 3-7.25 18-3.1-7.65L3 10.25 21 3Z" strokeLinejoin="round" /></svg>;
}

export default function Home() {
  const [input, setInput] = useState('');
  const [sessionId] = useState(() => crypto.randomUUID());
  const [messages, setMessages] = useState<Message[]>([{ role: 'assistant', text: 'Ask a question about your orders, customers, products, or revenue. I’ll turn it into a safe data query.' }]);
  const [loading, setLoading] = useState(false);
  const [recorderState, setRecorderState] = useState<RecorderState>('idle');
  const [recordedAudio, setRecordedAudio] = useState<Blob | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const discardRecordingRef = useRef(false);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, loading, recorderState]);
  useEffect(() => () => mediaStreamRef.current?.getTracks().forEach((track) => track.stop()), []);

  const addMessage = (message: Message) => setMessages((previous) => [...previous, message]);

  const describeResponse = (data: Record<string, unknown>) => {
    if (data.status === 'success') {
      const results = Array.isArray(data.results) ? data.results : [];
      return results.length ? `Done — I found ${results.length} result${results.length === 1 ? '' : 's'}.` : 'That query ran successfully, but there are no matching records.';
    }
    if (data.status === 'blocked') return 'I can only help with safe, read-only analytics questions about this dataset.';
    if (data.status === 'ambiguous') return typeof data.question === 'string' ? data.question : 'Which metric or time range should I use?';
    return 'The analytics service is taking a breather. Please try that again in a moment.';
  };

  const runRequest = async (text: string, audioBlob?: Blob) => {
    if (loading) return;
    setLoading(true);
    try {
      const response = audioBlob
        ? await fetch(`${API_URL}/voice-query`, { method: 'POST', body: (() => { const formData = new FormData(); formData.append('file', audioBlob, 'recording.webm'); formData.append('session_id', sessionId); return formData; })() })
        : await fetch(`${API_URL}/query`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, session_id: sessionId }) });
      const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
      const transcript = typeof data.transcript === 'string' ? data.transcript : text;
      if (audioBlob) addMessage({ role: 'user', text: transcript || 'Voice query' });
      if (!response.ok || data.status === 'error') {
        addMessage({ role: 'assistant', status: 'error', text: 'I couldn’t complete that query right now. Your question is still here — try again in a few seconds.', retryText: transcript || text });
        return;
      }
      addMessage({ role: 'assistant', text: describeResponse(data), status: data.status as Message['status'], sql: typeof data.sql === 'string' ? data.sql : undefined, results: Array.isArray(data.results) ? (data.results as Record<string, unknown>[]) : undefined });
    } catch {
      if (audioBlob) addMessage({ role: 'user', text: text || 'Voice query' });
      addMessage({ role: 'assistant', status: 'error', text: 'I can’t reach the analytics service right now. Please check the connection and try again.', retryText: text });
    } finally { setLoading(false); }
  };

  const sendText = async (text = input.trim()) => {
    if (!text || loading) return;
    setInput('');
    addMessage({ role: 'user', text });
    if (isClearlyOffTopic(text)) {
      addMessage({ role: 'assistant', status: 'guidance', text: 'I’m here for your sales data — orders, customers, products, regions, and revenue. Try something like “Which region generated the most revenue?”' });
      return;
    }
    await runRequest(text);
  };

  const stopTracks = () => { mediaStreamRef.current?.getTracks().forEach((track) => track.stop()); mediaStreamRef.current = null; };

  const startRecording = async () => {
    if (loading) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      discardRecordingRef.current = false;
      mediaStreamRef.current = stream;
      recorder.ondataavailable = (event) => { if (event.data.size > 0) chunksRef.current.push(event.data); };
      recorder.onstop = () => {
        const shouldDiscard = discardRecordingRef.current;
        const audioBlob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
        stopTracks();
        mediaRecorderRef.current = null;
        if (shouldDiscard || audioBlob.size === 0) { setRecordedAudio(null); setRecorderState('idle'); return; }
        setRecordedAudio(audioBlob);
        setRecorderState('recorded');
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      setRecorderState('recording');
    } catch {
      addMessage({ role: 'assistant', status: 'error', text: 'Microphone access is off. Allow it in your browser settings, then try voice input again.' });
    }
  };

  const finishRecording = () => { if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop(); };
  const cancelRecording = () => {
    discardRecordingRef.current = true;
    if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop();
    else { setRecordedAudio(null); setRecorderState('idle'); stopTracks(); }
  };
  const sendRecording = async () => {
    if (!recordedAudio || loading) return;
    const audioBlob = recordedAudio;
    setRecordedAudio(null);
    setRecorderState('idle');
    await runRequest('', audioBlob);
  };

  return (
    <main className="relative min-h-screen overflow-hidden bg-[#070812] text-slate-100 selection:bg-cyan-300 selection:text-slate-950">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_10%_0%,rgba(34,211,238,0.14),transparent_30%),radial-gradient(circle_at_90%_15%,rgba(168,85,247,0.15),transparent_28%),radial-gradient(circle_at_50%_100%,rgba(59,130,246,0.10),transparent_35%)]" />
      <div className="relative mx-auto flex min-h-screen max-w-5xl flex-col px-4 py-4 sm:px-6 sm:py-6">
        <header className="flex items-center justify-between rounded-2xl border border-white/10 bg-white/[0.035] px-4 py-3 backdrop-blur-xl sm:px-5">
          <div className="flex items-center gap-3"><div className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-cyan-300 to-violet-500 font-black text-slate-950 shadow-[0_0_28px_rgba(34,211,238,0.25)]">V</div><div><h1 className="text-sm font-bold tracking-[0.18em] text-white">VOXQL</h1><p className="text-xs text-slate-400">Your data, in plain language.</p></div></div>
          <div className="hidden items-center gap-2 rounded-full border border-emerald-300/15 bg-emerald-300/10 px-3 py-1.5 text-xs font-medium text-emerald-200 sm:flex"><span className="size-1.5 rounded-full bg-emerald-300 shadow-[0_0_10px_rgba(110,231,183,0.9)]" />Analytics workspace</div>
        </header>
        <section className="flex flex-1 flex-col py-6 sm:py-10">
          <div className="mb-6 text-center sm:mb-8"><p className="mb-2 text-xs font-semibold uppercase tracking-[0.22em] text-cyan-300">Query without SQL</p><h2 className="text-2xl font-semibold tracking-tight text-white sm:text-4xl">What do you want to know?</h2></div>
          <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col">
            <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6">
              {messages.map((message, index) => (
                <article key={`${message.role}-${index}`} className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}><div className={`max-w-[92%] rounded-2xl px-4 py-3.5 shadow-lg sm:max-w-[82%] ${message.role === 'user' ? 'rounded-br-md bg-gradient-to-br from-cyan-300 to-blue-500 text-slate-950 shadow-cyan-500/10' : 'rounded-bl-md border border-white/10 bg-white/[0.055] text-slate-200 backdrop-blur-sm'}`}>
                  <p className="text-sm leading-6">{message.text}</p>
                  {message.status === 'guidance' && <p className="mt-3 rounded-lg border border-cyan-300/15 bg-cyan-300/10 px-3 py-2 text-xs text-cyan-100">Tip: ask about a metric, time period, customer group, product, or region.</p>}
                  {message.retryText && <button type="button" onClick={() => void sendText(message.retryText)} disabled={loading} className="mt-3 rounded-lg border border-white/15 bg-white/10 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-white/15 disabled:cursor-not-allowed disabled:opacity-50">Try again</button>}
                  {message.sql && <details className="mt-3"><summary className="cursor-pointer text-xs font-medium text-cyan-200 marker:text-cyan-300">View generated SQL</summary><pre className="mt-2 overflow-x-auto rounded-xl border border-white/10 bg-[#05060c] p-3 text-xs leading-5 text-slate-300">{message.sql}</pre></details>}
                  {message.results && message.results.length > 0 && <div className="mt-3 overflow-x-auto rounded-xl border border-white/10"><table className="w-full min-w-max text-left text-xs"><thead className="bg-white/[0.07] text-slate-400"><tr>{Object.keys(message.results[0]).map((column) => <th key={column} className="px-3 py-2 font-medium">{column}</th>)}</tr></thead><tbody className="divide-y divide-white/5">{message.results.slice(0, 10).map((row, rowIndex) => <tr key={rowIndex} className="text-slate-200">{Object.values(row).map((value, cellIndex) => <td key={cellIndex} className="px-3 py-2">{String(value ?? '—')}</td>)}</tr>)}</tbody></table></div>}
                </div></article>
              ))}
              {loading && <div className="flex justify-start"><div className="flex items-center gap-2 rounded-2xl rounded-bl-md border border-white/10 bg-white/[0.055] px-4 py-3 text-sm text-slate-300"><span className="flex gap-1"><i className="size-1.5 animate-bounce rounded-full bg-cyan-300 [animation-delay:-0.3s]" /><i className="size-1.5 animate-bounce rounded-full bg-cyan-300 [animation-delay:-0.15s]" /><i className="size-1.5 animate-bounce rounded-full bg-cyan-300" /></span>Thinking through your data…</div></div>}
              <div ref={bottomRef} />
            </div>
            {!messages.some((message) => message.role === 'user') && <div className="mb-4 flex flex-wrap justify-center gap-2">{starterQuestions.map((question) => <button key={question} type="button" onClick={() => void sendText(question)} className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-slate-300 transition hover:border-cyan-300/35 hover:bg-cyan-300/10 hover:text-cyan-100">{question}</button>)}</div>}
            <div className="rounded-2xl border border-white/10 bg-[#111322]/80 p-2 shadow-2xl shadow-black/30 backdrop-blur-xl">
              {recorderState === 'recording' ? <div className="flex items-center justify-between gap-3 px-3 py-2"><div className="flex items-center gap-3 text-sm text-white"><span className="relative flex size-3"><span className="absolute inline-flex size-full animate-ping rounded-full bg-rose-400 opacity-70" /><span className="relative inline-flex size-3 rounded-full bg-rose-400" /></span>Recording…</div><div className="flex gap-2"><button type="button" onClick={cancelRecording} className="rounded-xl px-3 py-2 text-xs font-semibold text-slate-300 transition hover:bg-white/10 hover:text-white">Cancel</button><button type="button" onClick={finishRecording} className="rounded-xl bg-white px-3 py-2 text-xs font-bold text-slate-950 transition hover:bg-cyan-100">Done</button></div></div> : recorderState === 'recorded' ? <div className="flex items-center justify-between gap-3 px-3 py-2"><div className="flex items-center gap-2 text-sm text-cyan-100"><span className="grid size-7 place-items-center rounded-lg bg-cyan-300/15 text-cyan-300"><MicrophoneIcon /></span>Voice note ready to send</div><div className="flex gap-2"><button type="button" onClick={cancelRecording} className="rounded-xl px-3 py-2 text-xs font-semibold text-slate-300 transition hover:bg-white/10 hover:text-white">Cancel</button><button type="button" onClick={() => void sendRecording()} disabled={loading} className="inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-cyan-300 to-blue-400 px-3 py-2 text-xs font-bold text-slate-950 transition hover:brightness-110 disabled:opacity-50"><SendIcon />Send</button></div></div> : <div className="flex items-center gap-2"><button type="button" onClick={() => void startRecording()} disabled={loading} aria-label="Record a voice query" className="grid size-11 shrink-0 place-items-center rounded-xl text-slate-300 transition hover:bg-white/10 hover:text-cyan-200 disabled:cursor-not-allowed disabled:opacity-40"><MicrophoneIcon /></button><input value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void sendText(); } }} placeholder="Ask about your sales data…" className="min-w-0 flex-1 bg-transparent px-1 py-3 text-sm text-white outline-none placeholder:text-slate-500" /><button type="button" onClick={() => void sendText()} disabled={!input.trim() || loading} aria-label="Send query" className="grid size-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-cyan-300 to-blue-500 text-slate-950 shadow-lg shadow-cyan-500/10 transition hover:scale-[1.03] hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-35"><SendIcon /></button></div>}
            </div>
            <p className="mt-3 text-center text-xs text-slate-500">VoxQL answers from your connected sales database.</p>
          </div>
        </section>
      </div>
    </main>
  );
}
