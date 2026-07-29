"use client";

import { useMemo, type ReactNode } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ArrowLeft,
  AlertCircle,
  AudioLines,
  Bot,
  CalendarCheck,
  Clock,
  Download,
  FileText,
  FolderOpen,
  Phone,
  PhoneIncoming,
  PhoneOutgoing,
  Tags,
  User,
} from "lucide-react";
import { getCall, type CallRecord } from "@/lib/api/calls";

const ACCENT = "#ff5e00";

/** mm:ss */
function formatDuration(seconds: number): string {
  const safe = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function formatPhoneNumber(number: string): string {
  if (number.startsWith("+1") && number.length === 12) {
    return `(${number.slice(2, 5)}) ${number.slice(5, 8)}-${number.slice(8)}`;
  }
  return number;
}

function formatDateTime(dateString: string | null | undefined): string {
  if (!dateString) return "—";
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function getStatusBadgeVariant(
  status: string
): "default" | "destructive" | "secondary" | "outline" {
  switch (status) {
    case "completed":
      return "default";
    case "failed":
    case "busy":
    case "no_answer":
      return "destructive";
    case "in_progress":
      return "secondary";
    default:
      return "outline";
  }
}

type TranscriptTurn = { speaker: "user" | "assistant"; text: string };

/**
 * Parse the flat transcript ("[User]: ...", "[Assistant]: ...") into turns.
 * Returns an empty array when no markers are present — the caller then falls
 * back to rendering the raw text verbatim.
 */
function parseTranscript(raw: string): TranscriptTurn[] {
  const trimmed = raw.trim();
  if (!trimmed) return [];

  // Capturing group keeps the speaker label in the split output.
  const parts = trimmed.split(/\[(user|assistant)\]\s*:?\s*/i);
  const turns: TranscriptTurn[] = [];

  for (let i = 1; i < parts.length; i += 2) {
    const label = (parts[i] ?? "").toLowerCase();
    const text = (parts[i + 1] ?? "").trim();
    if (!text) continue;
    turns.push({ speaker: label === "user" ? "user" : "assistant", text });
  }

  return turns;
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Pull a primitive field off an unknown-shaped record, if present. */
function readField(source: unknown, key: string): string | null {
  if (typeof source !== "object" || source === null) return null;
  const value = (source as Record<string, unknown>)[key];
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function MetaItem({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="text-sm font-medium">{value}</div>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <Skeleton className="h-9 w-9 rounded-md" />
        <div className="space-y-2">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-4 w-64" />
        </div>
      </div>
      <Card>
        <CardContent className="grid gap-6 p-6 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 8 }).map((_, index) => (
            <div key={index} className="space-y-2">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-4 w-32" />
            </div>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardContent className="space-y-3 p-6">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-12 w-full" />
        </CardContent>
      </Card>
      <Card>
        <CardContent className="space-y-3 p-6">
          <Skeleton className="h-16 w-2/3" />
          <Skeleton className="ml-auto h-16 w-2/3" />
          <Skeleton className="h-16 w-2/3" />
        </CardContent>
      </Card>
    </div>
  );
}

export default function CallDetailPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const rawId: unknown = params?.id;
  const callId = Array.isArray(rawId) ? (rawId[0] ?? "") : typeof rawId === "string" ? rawId : "";

  const { data, isLoading, error } = useQuery<CallRecord>({
    queryKey: ["call", callId],
    queryFn: () => getCall(callId),
    enabled: callId.length > 0,
    retry: false,
  });

  const turns = useMemo(() => parseTranscript(data?.transcript ?? ""), [data?.transcript]);

  const variableEntries = useMemo(() => {
    const variables = data?.variables;
    if (!variables || typeof variables !== "object" || Array.isArray(variables)) return [];
    return Object.entries(variables);
  }, [data?.variables]);

  const bookingAttempts = useMemo(() => {
    const attempts = data?.booking_attempts;
    return Array.isArray(attempts) ? attempts : [];
  }, [data?.booking_attempts]);

  const handleDownloadTranscript = () => {
    if (!data?.transcript) return;
    const blob = new Blob([data.transcript], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `transcript-${data.id}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  if (isLoading) {
    return <DetailSkeleton />;
  }

  if (error || !data) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => router.back()} title="Go back">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <h1 className="text-xl font-semibold">Call not found</h1>
        </div>
        <Card className="border-destructive">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <AlertCircle className="mb-4 h-16 w-16 text-destructive" />
            <h3 className="mb-2 text-lg font-semibold">We couldn&apos;t load this call</h3>
            <p className="mb-4 max-w-sm text-center text-sm text-muted-foreground">
              {error instanceof Error
                ? error.message
                : "This call record doesn't exist, or you don't have access to it."}
            </p>
            <Button variant="outline" asChild>
              <Link href="/dashboard/calls">
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back to Call History
              </Link>
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const DirectionIcon = data.direction === "inbound" ? PhoneIncoming : PhoneOutgoing;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => router.back()}
            title="Go back"
            className="mt-0.5"
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">{data.agent_name ?? "Unknown Agent"}</h1>
              <Badge variant={data.direction === "inbound" ? "default" : "secondary"}>
                <DirectionIcon className="mr-1 h-3 w-3" />
                {data.direction}
              </Badge>
              <Badge variant={getStatusBadgeVariant(data.status)}>
                {data.status.replace("_", " ")}
              </Badge>
            </div>
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-sm text-muted-foreground">
              <Phone className="h-3.5 w-3.5" />
              {formatPhoneNumber(data.from_number)}
              <span aria-hidden="true">&rarr;</span>
              {formatPhoneNumber(data.to_number)}
              {data.contact_name && <span className="font-sans">&middot; {data.contact_name}</span>}
            </p>
          </div>
        </div>
      </div>

      {/* Call metadata */}
      <Card>
        <CardContent className="grid gap-6 p-6 sm:grid-cols-2 lg:grid-cols-4">
          <MetaItem
            icon={<Clock className="h-3.5 w-3.5" />}
            label="Duration"
            value={<span className="font-mono">{formatDuration(data.duration_seconds)}</span>}
          />
          <MetaItem
            icon={<FolderOpen className="h-3.5 w-3.5" />}
            label="Workspace"
            value={data.workspace_name ?? "—"}
          />
          <MetaItem
            icon={<Bot className="h-3.5 w-3.5" />}
            label="Provider"
            value={data.provider || "—"}
          />
          <MetaItem
            icon={<User className="h-3.5 w-3.5" />}
            label="Contact"
            value={data.contact_name ?? "—"}
          />
          <MetaItem
            icon={<Clock className="h-3.5 w-3.5" />}
            label="Started"
            value={formatDateTime(data.started_at)}
          />
          <MetaItem
            icon={<Clock className="h-3.5 w-3.5" />}
            label="Answered"
            value={formatDateTime(data.answered_at)}
          />
          <MetaItem
            icon={<Clock className="h-3.5 w-3.5" />}
            label="Ended"
            value={formatDateTime(data.ended_at)}
          />
          <MetaItem
            icon={<FileText className="h-3.5 w-3.5" />}
            label="Call ID"
            value={
              <span className="break-all font-mono text-xs">
                {data.provider_call_id || data.id}
              </span>
            }
          />
        </CardContent>
      </Card>

      {/* Recording */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <AudioLines className="h-4 w-4" />
            Recording
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data.recording_url ? (
            <div className="space-y-3">
              <audio controls preload="none" src={data.recording_url} className="w-full">
                Your browser does not support the audio element.
              </audio>
              <Button variant="outline" size="sm" asChild>
                <a
                  href={data.recording_url}
                  download={`recording-${data.id}.mp3`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <Download className="mr-2 h-4 w-4" />
                  Download recording
                </a>
              </Button>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No recording for this call</p>
          )}
        </CardContent>
      </Card>

      {/* Transcript */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4" />
            Transcript
          </CardTitle>
          {data.transcript && (
            <Button variant="outline" size="sm" onClick={handleDownloadTranscript}>
              <Download className="mr-2 h-4 w-4" />
              Download .txt
            </Button>
          )}
        </CardHeader>
        <CardContent>
          {!data.transcript ? (
            <p className="text-sm text-muted-foreground">No transcript for this call</p>
          ) : turns.length === 0 ? (
            <pre className="whitespace-pre-wrap rounded-lg border bg-muted/30 p-4 font-sans text-sm leading-relaxed">
              {data.transcript}
            </pre>
          ) : (
            <div className="space-y-3">
              {turns.map((turn, index) => {
                const isUser = turn.speaker === "user";
                return (
                  <div key={index} className={isUser ? "flex justify-start" : "flex justify-end"}>
                    <div
                      className={
                        isUser
                          ? "max-w-[80%] rounded-lg rounded-tl-sm border bg-muted/50 px-3 py-2"
                          : "max-w-[80%] rounded-lg rounded-tr-sm border px-3 py-2"
                      }
                      style={
                        isUser
                          ? undefined
                          : { borderColor: `${ACCENT}59`, backgroundColor: `${ACCENT}14` }
                      }
                    >
                      <p
                        className={
                          isUser
                            ? "mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground"
                            : "mb-1 text-[10px] font-semibold uppercase tracking-wide"
                        }
                        style={isUser ? undefined : { color: ACCENT }}
                      >
                        {isUser ? "Caller" : "Agent"}
                      </p>
                      <p className="whitespace-pre-wrap text-sm leading-relaxed">{turn.text}</p>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Lead variables */}
      {variableEntries.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Tags className="h-4 w-4" />
              Lead variables
            </CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="divide-y divide-border/60 rounded-lg border">
              {variableEntries.map(([key, value]) => (
                <div key={key} className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-3 p-2.5">
                  <dt className="truncate font-mono text-xs text-muted-foreground">{key}</dt>
                  <dd className="break-words text-sm">{stringifyValue(value)}</dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      )}

      {/* Booking attempts */}
      {bookingAttempts.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <CalendarCheck className="h-4 w-4" />
              Booking attempts
              <span className="text-xs font-normal text-muted-foreground">
                ({bookingAttempts.length})
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {bookingAttempts.map((attempt, index) => {
              const operation = readField(attempt, "operation");
              const category = readField(attempt, "category");
              const timestamp = readField(attempt, "timestamp");
              return (
                <div key={index} className="rounded-lg border p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">#{readField(attempt, "attempt") ?? index + 1}</Badge>
                    {operation && <span className="text-sm font-medium">{operation}</span>}
                    {category && (
                      <Badge variant="secondary" className="text-[10px]">
                        {category}
                      </Badge>
                    )}
                    {timestamp && (
                      <span className="ml-auto text-xs text-muted-foreground">
                        {formatDateTime(timestamp)}
                      </span>
                    )}
                  </div>
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-muted-foreground">
                      Raw details
                    </summary>
                    <pre className="mt-2 overflow-x-auto rounded-md bg-muted/40 p-2 text-xs">
                      {safeJson(attempt)}
                    </pre>
                  </details>
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
