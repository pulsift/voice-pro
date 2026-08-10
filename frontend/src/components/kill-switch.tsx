"use client";

/**
 * The stop-dialling control, on every page.
 *
 * Built on 2026-08-10, the evening the agent rang a prospect Sami was handling
 * personally. The switch already existed — the reply router refuses to place a
 * call while it is on — but reaching it meant a terminal and a shared token. In
 * the moment you need it, a control you have to look up is not a control.
 *
 * Three deliberate choices:
 *
 *   - STOPPING IS ONE CLICK. No confirmation. Stopping is always the safe
 *     direction, and a confirm dialog between someone and an emergency brake is
 *     a design that has never helped anyone.
 *   - RESUMING ASKS FIRST. That is the direction that can cost money.
 *   - UNKNOWN IS LOUD, and never reads as "running". If we cannot see the
 *     switch, saying "calls are live" would be a guess, and a safety indicator
 *     that guesses is worse than none because it gets believed.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { AlertTriangle, Loader2, PhoneOff, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface KillSwitchState {
  state: "paused" | "running" | "unknown";
  changed_at: string | null;
  error: string | null;
}

export function KillSwitch({ collapsed }: { collapsed: boolean }) {
  const qc = useQueryClient();
  const [confirmingResume, setConfirmingResume] = useState(false);

  const { data, isLoading } = useQuery<KillSwitchState>({
    queryKey: ["killswitch"],
    queryFn: async () => (await api.get("/api/v1/killswitch")).data,
    // Someone else (or a script) can move this. Poll often enough that the page
    // is not confidently wrong for long.
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });

  const mutation = useMutation({
    mutationFn: async (paused: boolean) =>
      (await api.post("/api/v1/killswitch", { paused })).data as KillSwitchState,
    onSuccess: (result) => {
      qc.setQueryData(["killswitch"], result);
      void qc.invalidateQueries({ queryKey: ["killswitch"] });
      setConfirmingResume(false);
      toast.success(
        result.state === "paused" ? "Calls stopped. Nothing is dialling." : "Calls resumed.",
      );
    },
    onError: (e: unknown) => {
      // A failed STOP is the worst case on this screen, so it shouts.
      toast.error(
        e instanceof Error ? e.message : "Could not change the switch — check the reply router",
      );
    },
  });

  const state = data?.state ?? "unknown";
  const busy = mutation.isPending || isLoading;

  if (collapsed) {
    return (
      <button
        onClick={() => !busy && mutation.mutate(state !== "paused")}
        disabled={busy}
        title={
          state === "paused"
            ? "Calls are STOPPED. Click to resume."
            : state === "running"
              ? "Calls are running. Click to stop everything."
              : "Cannot reach the reply router — state unknown"
        }
        className={cn(
          "mx-auto mb-2 flex h-9 w-9 items-center justify-center rounded-md border transition-colors",
          state === "paused"
            ? "border-amber-500/50 bg-amber-500/15 text-amber-500 hover:bg-amber-500/25"
            : state === "running"
              ? "border-red-600/60 bg-red-600/15 text-red-500 hover:bg-red-600/25"
              : "border-muted-foreground/40 bg-muted/40 text-muted-foreground",
        )}
      >
        {busy ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : state === "paused" ? (
          <ShieldAlert className="h-4 w-4" />
        ) : state === "unknown" ? (
          <AlertTriangle className="h-4 w-4" />
        ) : (
          <PhoneOff className="h-4 w-4" />
        )}
      </button>
    );
  }

  return (
    <div className="mb-2 px-2">
      {state === "running" && (
        <button
          onClick={() => mutation.mutate(true)}
          disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-md border border-red-600/60 bg-red-600/15 px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-red-500 transition-colors hover:bg-red-600/30 disabled:opacity-60"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <PhoneOff className="h-4 w-4" />}
          Stop all calls
        </button>
      )}

      {state === "paused" && (
        <div className="rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2.5">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-amber-500">
            <ShieldAlert className="h-4 w-4 shrink-0" />
            Calls stopped
          </div>
          <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
            Nothing is dialling. Replies still arrive and queue.
          </p>
          {confirmingResume ? (
            <div className="mt-2 flex gap-1.5">
              <button
                onClick={() => mutation.mutate(false)}
                disabled={busy}
                className="flex-1 rounded border border-amber-500/60 bg-amber-500/20 px-2 py-1 text-[11px] font-medium text-amber-400 hover:bg-amber-500/30 disabled:opacity-60"
              >
                {busy ? "Resuming..." : "Yes, resume"}
              </button>
              <button
                onClick={() => setConfirmingResume(false)}
                className="rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmingResume(true)}
              className="mt-2 w-full rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent"
            >
              Resume calls
            </button>
          )}
        </div>
      )}

      {state === "unknown" && (
        <div className="rounded-md border border-muted-foreground/40 bg-muted/40 px-3 py-2.5">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            Switch unreadable
          </div>
          <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
            {data?.error ?? "Cannot reach the reply router."} Assume calls may be running.
          </p>
          <button
            onClick={() => mutation.mutate(true)}
            disabled={busy}
            className="mt-2 w-full rounded border border-red-600/60 bg-red-600/15 px-2 py-1 text-[11px] font-medium text-red-500 hover:bg-red-600/30 disabled:opacity-60"
          >
            Stop anyway
          </button>
        </div>
      )}
    </div>
  );
}
