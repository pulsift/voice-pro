"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { MessageSquare, Copy, RefreshCw, Loader2, AlertCircle, KeyRound } from "lucide-react";
import { api } from "@/lib/api";

interface SmsMessage {
  id: string;
  provider: string;
  direction: string;
  from_number: string | null;
  to_number: string | null;
  text: string | null;
  num_media: number;
  received_at: string | null;
  created_at: string;
}

function formatPhoneNumber(number: string | null): string {
  if (!number) return "—";
  if (number.startsWith("+1") && number.length === 12) {
    return `(${number.slice(2, 5)}) ${number.slice(5, 8)}-${number.slice(8)}`;
  }
  return number;
}

// Surface a likely one-time code (4–8 digits) so it can be copied in one tap.
function extractCode(text: string | null): string | null {
  if (!text) return null;
  const match = text.match(/\b(\d{4,8})\b/);
  return match?.[1] ?? null;
}

async function copyToClipboard(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value);
    toast.success(`${label} copied`);
  } catch {
    toast.error("Couldn't copy to clipboard");
  }
}

export default function MessagesPage() {
  const [searchQuery, setSearchQuery] = useState("");

  const { data, isLoading, error, refetch, isFetching } = useQuery<SmsMessage[]>({
    queryKey: ["sms-inbox"],
    queryFn: async () => {
      const response = await api.get("/api/v1/sms/inbox", { params: { limit: 100 } });
      return response.data;
    },
    refetchInterval: 10000, // poll so verification codes appear within ~10s
  });

  const messages = data ?? [];
  const filtered = searchQuery
    ? messages.filter((m) => {
        const q = searchQuery.toLowerCase();
        return (
          (m.text?.toLowerCase().includes(q) ?? false) ||
          (m.from_number?.includes(searchQuery) ?? false) ||
          (m.to_number?.includes(searchQuery) ?? false)
        );
      })
    : messages;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Messages</h1>
          <p className="text-sm text-muted-foreground">
            {isLoading
              ? "Loading..."
              : messages.length === 0
                ? "No messages yet"
                : `${messages.length} received message${messages.length === 1 ? "" : "s"}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Input
            placeholder="Search messages..."
            className="w-[200px]"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refetch()}
            disabled={isFetching}
            title="Refresh"
          >
            <RefreshCw className={`mr-1 h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      <Card>
        <CardContent>
          {isLoading ? (
            <div className="flex flex-col items-center justify-center py-16">
              <Loader2 className="mb-4 h-16 w-16 animate-spin text-muted-foreground/50" />
              <p className="text-muted-foreground">Loading messages...</p>
            </div>
          ) : error instanceof Error ? (
            <div className="flex flex-col items-center justify-center py-16">
              <AlertCircle className="mb-4 h-16 w-16 text-destructive" />
              <h3 className="mb-2 text-lg font-semibold">Failed to load messages</h3>
              <p className="max-w-sm text-center text-sm text-muted-foreground">{error.message}</p>
            </div>
          ) : messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16">
              <MessageSquare className="mb-4 h-16 w-16 text-muted-foreground/50" />
              <h3 className="mb-2 text-lg font-semibold">No messages yet</h3>
              <p className="max-w-sm text-center text-sm text-muted-foreground">
                Inbound texts to your numbers (e.g. verification codes) will appear here within
                seconds of arriving.
              </p>
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16">
              <MessageSquare className="mb-4 h-16 w-16 text-muted-foreground/50" />
              <h3 className="mb-2 text-lg font-semibold">No matching messages</h3>
              <p className="max-w-sm text-center text-sm text-muted-foreground">
                Try a different search.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[170px]">Received</TableHead>
                  <TableHead className="w-[150px]">From</TableHead>
                  <TableHead className="w-[150px]">To</TableHead>
                  <TableHead>Message</TableHead>
                  <TableHead className="w-[150px]">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((m) => {
                  const code = extractCode(m.text);
                  return (
                    <TableRow key={m.id}>
                      <TableCell className="text-sm">
                        {new Date(m.received_at ?? m.created_at).toLocaleString()}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {formatPhoneNumber(m.from_number)}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {formatPhoneNumber(m.to_number)}
                      </TableCell>
                      <TableCell className="max-w-[420px] whitespace-pre-wrap break-words text-sm">
                        {m.text ?? (m.num_media > 0 ? `[${m.num_media} media attachment(s)]` : "—")}
                        {code && (
                          <Badge variant="secondary" className="ml-2 font-mono">
                            {code}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex gap-1">
                          {code && (
                            <Button
                              variant="ghost"
                              size="icon"
                              title="Copy code"
                              onClick={() => void copyToClipboard(code, "Code")}
                            >
                              <KeyRound className="h-4 w-4" />
                            </Button>
                          )}
                          {m.text && (
                            <Button
                              variant="ghost"
                              size="icon"
                              title="Copy message"
                              onClick={() => void copyToClipboard(m.text ?? "", "Message")}
                            >
                              <Copy className="h-4 w-4" />
                            </Button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
