"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { MessageSquare, Send, Plus, Info, Loader2, KeyRound, Copy } from "lucide-react";
import { api } from "@/lib/api";

interface Conversation {
  contact_number: string;
  our_number: string | null;
  name: string | null;
  last_text: string | null;
  last_direction: string | null;
  last_at: string;
  message_count: number;
  provider: string;
}

interface OurNumber {
  number: string;
  provider: string;
  message_count: number;
  last_at: string | null;
  can_send_to_us: boolean;
  note: string;
}

interface SmsMessage {
  id: string;
  direction: string;
  from_number: string | null;
  to_number: string | null;
  text: string | null;
  num_media: number;
  received_at: string | null;
  created_at: string;
  provider: string;
}

interface SendablePhoneNumber {
  id: string;
  phone_number: string;
  friendly_name: string | null;
  provider: string;
  can_send_sms: boolean;
  status: string;
}

const FROM_NUMBER_KEY = "sms-from-number";

function formatPhone(n: string | null): string {
  if (!n) return "—";
  if (n.startsWith("+1") && n.length === 12) {
    return `(${n.slice(2, 5)}) ${n.slice(5, 8)}-${n.slice(8)}`;
  }
  return n;
}

function initials(name: string | null, number: string): string {
  if (name) {
    const parts = name.trim().split(/\s+/);
    return (parts[0]?.[0] ?? "" + (parts[1]?.[0] ?? "")).toUpperCase().slice(0, 2) || "#";
  }
  return number.replace(/\D/g, "").slice(-2) || "#";
}

function extractCode(text: string | null): string | null {
  if (!text) return null;
  return text.match(/\b(\d{4,8})\b/)?.[1] ?? null;
}

function timeLabel(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function MessagesPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameName, setRenameName] = useState("");
  const [composeOpen, setComposeOpen] = useState(false);
  const [newTo, setNewTo] = useState("");
  const [newBody, setNewBody] = useState("");
  const [fromNumber, setFromNumber] = useState<string>("");
  const draftRef = useRef<HTMLTextAreaElement | null>(null);

  const { data: sendableNumbers = [] } = useQuery<SendablePhoneNumber[]>({
    queryKey: ["sms-from-numbers"],
    queryFn: async () => {
      const res = (await api.get("/api/v1/phone-numbers?page_size=100")).data;
      const nums: SendablePhoneNumber[] = res.phone_numbers ?? [];
      // The backend routes the send by the from-number's provider (Telnyx REST
      // or the Twilio SDK), so both are offerable here.
      return nums.filter(
        (n) =>
          n.can_send_sms &&
          n.status === "active" &&
          (n.provider === "telnyx" || n.provider === "twilio")
      );
    },
  });

  // Default the from-number: last used (localStorage), else first sendable.
  useEffect(() => {
    if (fromNumber || sendableNumbers.length === 0) return;
    const saved = localStorage.getItem(FROM_NUMBER_KEY);
    const match = sendableNumbers.find((n) => n.phone_number === saved);
    const fallback = sendableNumbers[0]?.phone_number ?? "";
    setFromNumber(match ? match.phone_number : fallback);
  }, [sendableNumbers, fromNumber]);

  const pickFromNumber = (value: string) => {
    setFromNumber(value);
    localStorage.setItem(FROM_NUMBER_KEY, value);
  };

  const numberLabel = (n: SendablePhoneNumber) =>
    n.friendly_name && n.friendly_name !== n.phone_number
      ? `${formatPhone(n.phone_number)} · ${n.friendly_name}`
      : `${formatPhone(n.phone_number)} · ${n.provider}`;

  const autoGrow = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  // Which of OUR numbers the inbox is being read through. null = all of them.
  const [lineFilter, setLineFilter] = useState<string | null>(null);

  const { data: ourNumbers = [] } = useQuery<OurNumber[]>({
    queryKey: ["sms-our-numbers"],
    queryFn: async () => (await api.get("/api/v1/sms/numbers")).data,
    refetchInterval: 30000,
  });

  const { data: conversations = [], isLoading: convLoading } = useQuery<Conversation[]>({
    queryKey: ["sms-conversations", lineFilter],
    queryFn: async () =>
      (
        await api.get("/api/v1/sms/conversations", {
          params: lineFilter ? { our_number: lineFilter } : undefined,
        })
      ).data,
    refetchInterval: 10000,
  });

  const activeLine = ourNumbers.find((n) => n.number === lineFilter) ?? null;

  const activeConvo = conversations.find((c) => c.contact_number === selected) ?? null;

  const { data: messages = [], isLoading: msgLoading } = useQuery<SmsMessage[]>({
    queryKey: ["sms-thread", selected],
    queryFn: async () =>
      (await api.get(`/api/v1/sms/conversations/${encodeURIComponent(selected ?? "")}/messages`))
        .data,
    enabled: !!selected,
    refetchInterval: 5000,
  });

  const sendMutation = useMutation({
    mutationFn: async (vars: { to: string; body: string; from_number?: string }) =>
      (await api.post("/api/v1/sms/send", vars)).data,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sms-thread", selected] });
      void qc.invalidateQueries({ queryKey: ["sms-conversations"] });
    },
    onError: (e: unknown) => {
      const msg = e instanceof Error ? e.message : "Failed to send";
      toast.error(msg);
    },
  });

  const renameMutation = useMutation({
    mutationFn: async (vars: { phone_number: string; name: string }) =>
      (await api.put("/api/v1/sms/contacts", vars)).data,
    onSuccess: () => {
      toast.success("Saved");
      setRenameOpen(false);
      void qc.invalidateQueries({ queryKey: ["sms-conversations"] });
    },
    onError: () => toast.error("Couldn't save name"),
  });

  const handleSend = () => {
    if (!selected || !draft.trim()) return;
    sendMutation.mutate({
      to: selected,
      body: draft.trim(),
      from_number: fromNumber || undefined,
    });
    setDraft("");
    autoGrow(draftRef.current);
  };

  const handleCompose = () => {
    const to = newTo.trim();
    if (!to || !newBody.trim()) return;
    sendMutation.mutate(
      { to, body: newBody.trim(), from_number: fromNumber || undefined },
      {
        onSuccess: () => {
          setComposeOpen(false);
          setNewBody("");
          setNewTo("");
          setSelected(to);
        },
      }
    );
  };

  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`${label} copied`);
    } catch {
      toast.error("Couldn't copy");
    }
  };

  const titleFor = (c: Conversation) => c.name ?? formatPhone(c.contact_number);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Messages</h1>
          <p className="text-sm text-muted-foreground">
            {convLoading
              ? "Loading..."
              : conversations.length === 0
                ? "No conversations yet"
                : `${conversations.length} conversation${conversations.length === 1 ? "" : "s"}`}
          </p>
        </div>
        <Button size="sm" onClick={() => setComposeOpen(true)}>
          <Plus className="mr-1 h-4 w-4" />
          New message
        </Button>
      </div>

      {/* Which of our lines you are reading through. Two providers run at once
          and they are not interchangeable, so the line is part of the question
          "will my reply actually arrive". */}
      {ourNumbers.length > 1 && (
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => setLineFilter(null)}
            className={`rounded-full border px-3 py-1 text-xs transition-colors ${
              lineFilter === null ? "bg-primary text-primary-foreground" : "hover:bg-accent"
            }`}
          >
            All numbers
          </button>
          {ourNumbers.map((n) => (
            <button
              key={n.number}
              onClick={() => setLineFilter(n.number)}
              title={n.note}
              className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                lineFilter === n.number
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-accent"
              }`}
            >
              {formatPhone(n.number)}
              <span className="ml-1.5 opacity-60">{n.provider}</span>
              {!n.can_send_to_us && <span className="ml-1.5">· receive only</span>}
            </button>
          ))}
        </div>
      )}

      {activeLine && !activeLine.can_send_to_us && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
          <span>{activeLine.note}</span>
        </div>
      )}

      <Card className="grid h-[calc(100vh-12rem)] grid-cols-[300px_1fr] overflow-hidden p-0">
        {/* Conversation list */}
        <div className="flex flex-col overflow-y-auto border-r">
          {conversations.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center p-6 text-center">
              <MessageSquare className="mb-3 h-10 w-10 text-muted-foreground/40" />
              <p className="text-sm text-muted-foreground">
                Inbound texts and your replies will thread here.
              </p>
            </div>
          ) : (
            conversations.map((c) => (
              <button
                key={c.contact_number}
                onClick={() => setSelected(c.contact_number)}
                className={`flex items-center gap-3 border-b px-3 py-3 text-left transition-colors hover:bg-accent/50 ${
                  selected === c.contact_number ? "bg-accent" : ""
                }`}
              >
                <Avatar className="h-9 w-9 shrink-0">
                  <AvatarFallback className="text-xs">
                    {initials(c.name, c.contact_number)}
                  </AvatarFallback>
                </Avatar>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{titleFor(c)}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {timeLabel(c.last_at)}
                    </span>
                  </div>
                  <p className="truncate text-xs text-muted-foreground">
                    {c.last_direction === "outbound" && "You: "}
                    {c.last_text ?? "—"}
                  </p>
                </div>
              </button>
            ))
          )}
        </div>

        {/* Thread */}
        <div className="flex flex-col overflow-hidden">
          {!selected || !activeConvo ? (
            <div className="flex flex-1 flex-col items-center justify-center text-center">
              <MessageSquare className="mb-3 h-12 w-12 text-muted-foreground/30" />
              <p className="text-sm text-muted-foreground">Select a conversation</p>
            </div>
          ) : (
            <>
              {/* Header */}
              <div className="flex items-center justify-between border-b px-4 py-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{titleFor(activeConvo)}</div>
                  <div className="text-xs text-muted-foreground">
                    {formatPhone(activeConvo.contact_number)}
                    {activeConvo.our_number && (
                      <span> · via {formatPhone(activeConvo.our_number)}</span>
                    )}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  title="Edit name"
                  onClick={() => {
                    setRenameName(activeConvo.name ?? "");
                    setRenameOpen(true);
                  }}
                >
                  <Info className="h-4 w-4" />
                </Button>
              </div>

              {/* Messages */}
              <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-4">
                {msgLoading ? (
                  <div className="flex flex-1 items-center justify-center">
                    <Loader2 className="h-6 w-6 animate-spin text-muted-foreground/50" />
                  </div>
                ) : (
                  messages.map((m) => {
                    const outbound = m.direction === "outbound";
                    const code = !outbound ? extractCode(m.text) : null;
                    return (
                      <div
                        key={m.id}
                        className={`flex flex-col ${outbound ? "items-end" : "items-start"}`}
                      >
                        <div
                          className={`max-w-[75%] rounded-2xl px-3 py-2 text-sm ${
                            outbound
                              ? "bg-primary text-primary-foreground"
                              : "bg-muted text-foreground"
                          }`}
                        >
                          <span className="whitespace-pre-wrap break-words">
                            {m.text ?? (m.num_media > 0 ? `[${m.num_media} attachment(s)]` : "—")}
                          </span>
                          {code && (
                            <button
                              onClick={() => void copy(code, "Code")}
                              className="ml-2 inline-flex items-center gap-1 rounded bg-background/20 px-1.5 py-0.5 align-middle text-xs"
                              title="Copy code"
                            >
                              <KeyRound className="h-3 w-3" />
                              {code}
                            </button>
                          )}
                        </div>
                        <span className="mt-0.5 px-1 text-[10px] text-muted-foreground">
                          {new Date(m.received_at ?? m.created_at).toLocaleString()}
                        </span>
                      </div>
                    );
                  })
                )}
              </div>

              {/* Composer */}
              <div className="space-y-2 border-t p-3">
                <div className="flex items-center gap-2">
                  <span className="shrink-0 text-xs text-muted-foreground">From</span>
                  <Select value={fromNumber} onValueChange={pickFromNumber}>
                    <SelectTrigger className="h-8 w-auto min-w-[220px] text-xs">
                      <SelectValue placeholder="Pick a number…" />
                    </SelectTrigger>
                    <SelectContent>
                      {sendableNumbers.map((n) => (
                        <SelectItem key={n.id} value={n.phone_number} className="text-xs">
                          {numberLabel(n)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-end gap-2">
                  <Textarea
                    ref={draftRef}
                    rows={1}
                    placeholder="Type a message… (Shift+Enter for a new line)"
                    className="max-h-40 min-h-9 resize-none overflow-y-auto"
                    value={draft}
                    onChange={(e) => {
                      setDraft(e.target.value);
                      autoGrow(e.currentTarget);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleSend();
                      }
                    }}
                  />
                  <Button
                    size="icon"
                    className="shrink-0"
                    onClick={handleSend}
                    disabled={!draft.trim() || sendMutation.isPending}
                  >
                    {sendMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Send className="h-4 w-4" />
                    )}
                  </Button>
                </div>
              </div>
            </>
          )}
        </div>
      </Card>

      {/* Rename / contact dialog */}
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent className="sm:max-w-[400px]">
          <DialogHeader>
            <DialogTitle>Contact</DialogTitle>
            <DialogDescription>Give this number a name. The number is kept.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="space-y-1">
              <Label className="text-muted-foreground">Number</Label>
              <div className="flex items-center gap-2">
                <p className="font-mono text-sm">{activeConvo?.contact_number}</p>
                {activeConvo && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6"
                    onClick={() => void copy(activeConvo.contact_number, "Number")}
                  >
                    <Copy className="h-3 w-3" />
                  </Button>
                )}
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="cname">Name</Label>
              <Input
                id="cname"
                value={renameName}
                onChange={(e) => setRenameName(e.target.value)}
                placeholder="e.g. Acme Solar"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenameOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() =>
                activeConvo &&
                renameName.trim() &&
                renameMutation.mutate({
                  phone_number: activeConvo.contact_number,
                  name: renameName.trim(),
                })
              }
              disabled={!renameName.trim() || renameMutation.isPending}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* New message dialog */}
      <Dialog open={composeOpen} onOpenChange={setComposeOpen}>
        <DialogContent className="sm:max-w-[440px]">
          <DialogHeader>
            <DialogTitle>New message</DialogTitle>
            <DialogDescription>Send a text from your Voice Pro number.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="space-y-1">
              <Label>From</Label>
              <Select value={fromNumber} onValueChange={pickFromNumber}>
                <SelectTrigger>
                  <SelectValue placeholder="Pick a number…" />
                </SelectTrigger>
                <SelectContent>
                  {sendableNumbers.map((n) => (
                    <SelectItem key={n.id} value={n.phone_number}>
                      {numberLabel(n)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="to">To (E.164)</Label>
              <Input
                id="to"
                value={newTo}
                onChange={(e) => setNewTo(e.target.value)}
                placeholder="+14155551234"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="body">Message</Label>
              <Textarea
                id="body"
                rows={4}
                className="max-h-60 resize-none overflow-y-auto"
                value={newBody}
                onChange={(e) => {
                  setNewBody(e.target.value);
                  autoGrow(e.currentTarget);
                }}
                placeholder="Type a message… (Shift+Enter for a new line)"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setComposeOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleCompose}
              disabled={!newTo.trim() || !newBody.trim() || sendMutation.isPending}
            >
              {sendMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Send
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
