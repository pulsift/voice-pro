"use client";

/* eslint-disable no-console -- Debug logging is intentional for WebRTC/audio troubleshooting */

import { useState, useRef, useEffect, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { updateAgent } from "@/lib/api/agents";
import { api } from "@/lib/api";
import { getWhisperCode, getLanguagesForTier } from "@/lib/languages";
import { Button } from "@/components/ui/button";
import { Play, Square, Loader2, Save, FolderOpen } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import Link from "next/link";

interface Workspace {
  id: string;
  name: string;
  description: string | null;
  is_default: boolean;
}

interface WorkspaceAgent {
  agent_id: string;
  agent_name: string;
  is_default: boolean;
}

type TranscriptItem = {
  id: string;
  speaker: "user" | "assistant" | "system";
  text: string;
  timestamp: Date;
};

// Transcript entry for saving to backend
type TranscriptEntry = {
  role: "user" | "assistant";
  content: string;
};

// Connection status type
type ConnectionStatus = "idle" | "connecting" | "connected";

// WebRTC resources for proper cleanup
type WebRTCResources = {
  peerConnection: RTCPeerConnection | null;
  dataChannel: RTCDataChannel | null;
  audioStream: MediaStream | null;
  audioElement: HTMLAudioElement | null;
};

// Audio visualizer resources
type AudioVisualizerResources = {
  audioContext: AudioContext | null;
  analyser: AnalyserNode | null;
  source: MediaStreamAudioSourceNode | null;
};

// Real-time audio visualizer component using Web Audio API
function AudioVisualizer({
  stream,
  isActive,
  barCount = 12,
}: {
  stream: MediaStream | null;
  isActive: boolean;
  barCount?: number;
}) {
  const [frequencies, setFrequencies] = useState<number[]>(new Array(barCount).fill(0));
  const audioRef = useRef<AudioVisualizerResources>({
    audioContext: null,
    analyser: null,
    source: null,
  });
  const animationRef = useRef<number | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!stream || !isActive) {
      // Clean up and reset
      if (animationRef.current) {
        cancelAnimationFrame(animationRef.current);
        animationRef.current = null;
      }
      if (audioRef.current.audioContext && audioRef.current.audioContext.state !== "closed") {
        void audioRef.current.audioContext.close();
        audioRef.current = { audioContext: null, analyser: null, source: null };
      }
      setFrequencies(new Array(barCount).fill(0));
      return;
    }

    // Set up Web Audio API
    const audioContext = new AudioContext();
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.7;

    const source = audioContext.createMediaStreamSource(stream);
    source.connect(analyser);

    audioRef.current = { audioContext, analyser, source };

    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    const updateFrequencies = () => {
      if (!audioRef.current.analyser) return;

      audioRef.current.analyser.getByteFrequencyData(dataArray);

      // Map frequency data to bar count with logarithmic scaling for better low-frequency visibility
      const bands: number[] = [];

      for (let i = 0; i < barCount; i++) {
        // Use logarithmic distribution to favor lower frequencies
        const startIndex = Math.floor(Math.pow(i / barCount, 1.5) * bufferLength);
        const endIndex = Math.floor(Math.pow((i + 1) / barCount, 1.5) * bufferLength);

        let sum = 0;
        const count = Math.max(1, endIndex - startIndex);
        for (let j = startIndex; j < endIndex && j < bufferLength; j++) {
          sum += dataArray[j] ?? 0;
        }

        // Normalize to 0-1 range with slight boost
        const avg = sum / count / 255;
        bands.push(Math.min(1, avg * 1.5));
      }

      setFrequencies(bands);
      animationRef.current = requestAnimationFrame(updateFrequencies);
    };

    updateFrequencies();

    return () => {
      if (animationRef.current) {
        cancelAnimationFrame(animationRef.current);
      }
      if (audioContext.state !== "closed") {
        void audioContext.close();
      }
    };
  }, [stream, isActive, barCount]);

  // Idle animation when not active
  const idleAnimation = mounted && !isActive;

  return (
    <div className="flex h-6 items-center gap-0.5">
      {frequencies.map((freq, i) => {
        // Create mirrored effect from center
        const centerDistance = Math.abs(i - (barCount - 1) / 2) / ((barCount - 1) / 2);
        const idleHeight = idleAnimation ? 4 + Math.sin(Date.now() / 300 + i * 0.5) * 3 : 4;
        const height = isActive ? Math.max(4, freq * 20) : idleHeight;
        const opacity = isActive ? 0.4 + freq * 0.6 : 0.3 + (1 - centerDistance) * 0.2;

        return (
          <div
            key={i}
            className="w-0.5 rounded-full bg-foreground transition-all duration-75 ease-out"
            style={{
              height: `${height}px`,
              opacity,
            }}
          />
        );
      })}
    </div>
  );
}

// Toggle button group component
function ToggleGroup({
  options,
  value,
  onChange,
}: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex rounded-lg border bg-muted/50 p-1">
      {options.map((option) => (
        <button
          key={option.value}
          onClick={() => onChange(option.value)}
          className={`flex-1 rounded-md px-3 py-1.5 text-sm transition-colors ${
            value === option.value
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export default function TestAgentPage() {
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string>("all");
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("idle");
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
  const [callDuration, setCallDuration] = useState(0);
  const [audioStream, setAudioStream] = useState<MediaStream | null>(null);

  // Ref to track selected agent ID for use in callbacks (avoids stale closures)
  const selectedAgentIdRef = useRef<string>("");

  // Settings state
  const [voice, setVoice] = useState("marin");
  const [language, setLanguage] = useState("en-US");
  const [turnDetection, setTurnDetection] = useState("normal");
  const [threshold, setThreshold] = useState(0.5);
  const [prefixPadding, setPrefixPadding] = useState(300);
  const [silenceDuration, setSilenceDuration] = useState(200);
  const [idleTimeout, setIdleTimeout] = useState(true);
  const [editedSystemPrompt, setEditedSystemPrompt] = useState("");

  const queryClient = useQueryClient();

  // Fetch workspaces
  const { data: workspaces = [] } = useQuery<Workspace[]>({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const response = await api.get("/api/v1/workspaces");
      return response.data;
    },
  });

  // Fetch all agents (for "All Workspaces" / admin mode)
  const { data: allAgents = [] } = useQuery<{ id: string; name: string; pricing_tier: string }[]>({
    queryKey: ["agents"],
    queryFn: async () => {
      const response = await api.get("/api/v1/agents");
      return response.data;
    },
    enabled: selectedWorkspaceId === "all",
  });

  // Fetch agents for selected workspace
  const { data: workspaceAgents = [] } = useQuery<WorkspaceAgent[]>({
    queryKey: ["workspace-agents", selectedWorkspaceId],
    queryFn: async () => {
      if (!selectedWorkspaceId || selectedWorkspaceId === "all") return [];
      const response = await api.get(`/api/v1/workspaces/${selectedWorkspaceId}/agents`);
      return response.data;
    },
    enabled: !!selectedWorkspaceId && selectedWorkspaceId !== "all",
  });

  // Get the list of agents based on selection mode
  const availableAgents =
    selectedWorkspaceId === "all"
      ? allAgents
          .filter((a) => a.pricing_tier === "premium" || a.pricing_tier === "premium-mini")
          .map((a) => ({ agent_id: a.id, agent_name: a.name, is_default: false }))
      : workspaceAgents;

  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const callTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // WebRTC resource refs for proper cleanup
  const webrtcRef = useRef<WebRTCResources>({
    peerConnection: null,
    dataChannel: null,
    audioStream: null,
    audioElement: null,
  });

  // Batch transcript updates to reduce re-renders
  const pendingTranscriptsRef = useRef<TranscriptItem[]>([]);
  const flushTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Transcript tracking for saving to backend
  const transcriptEntriesRef = useRef<TranscriptEntry[]>([]);
  const sessionIdRef = useRef<string>("");
  const sessionStartTimeRef = useRef<number>(0);

  // Auto-scroll to bottom when transcript updates
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript]);

  // Fetch selected agent details
  const { data: selectedAgent } = useQuery({
    queryKey: ["agent", selectedAgentId],
    queryFn: async () => {
      if (!selectedAgentId) return null;
      const response = await api.get(`/api/v1/agents/${selectedAgentId}`);
      return response.data;
    },
    enabled: !!selectedAgentId,
  });

  // Clear agent selection when workspace changes
  useEffect(() => {
    setSelectedAgentId("");
  }, [selectedWorkspaceId]);

  // Sync agent ID ref whenever it changes
  useEffect(() => {
    selectedAgentIdRef.current = selectedAgentId;
  }, [selectedAgentId]);

  // Update all settings when agent changes
  useEffect(() => {
    if (selectedAgent) {
      setEditedSystemPrompt(selectedAgent.system_prompt ?? "");
      setVoice(selectedAgent.voice ?? "alloy");
      setLanguage(selectedAgent.language ?? "en-US");
      setTurnDetection(selectedAgent.turn_detection_mode ?? "normal");
      setThreshold(selectedAgent.turn_detection_threshold ?? 0.5);
      setPrefixPadding(selectedAgent.turn_detection_prefix_padding_ms ?? 300);
      setSilenceDuration(selectedAgent.turn_detection_silence_duration_ms ?? 200);
    } else {
      setEditedSystemPrompt("");
      setVoice("marin");
      setLanguage("en-US");
      setTurnDetection("normal");
      setThreshold(0.5);
      setPrefixPadding(300);
      setSilenceDuration(200);
    }
  }, [selectedAgent]);

  // Mutation to save all settings
  const saveSettingsMutation = useMutation({
    mutationFn: async () => {
      if (!selectedAgentId) throw new Error("No agent selected");
      return updateAgent(selectedAgentId, {
        system_prompt: editedSystemPrompt,
        voice: voice,
        language: language,
        turn_detection_mode: turnDetection as "normal" | "semantic" | "disabled",
        turn_detection_threshold: threshold,
        turn_detection_prefix_padding_ms: prefixPadding,
        turn_detection_silence_duration_ms: silenceDuration,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["agent", selectedAgentId] });
      toast.success("Settings saved");
    },
    onError: (error: Error) => {
      toast.error(`Failed to save: ${error.message}`);
    },
  });

  const hasUnsavedChanges = selectedAgent
    ? selectedAgent.system_prompt !== editedSystemPrompt ||
      selectedAgent.voice !== voice ||
      selectedAgent.language !== language ||
      selectedAgent.turn_detection_mode !== turnDetection ||
      selectedAgent.turn_detection_threshold !== threshold ||
      selectedAgent.turn_detection_prefix_padding_ms !== prefixPadding ||
      selectedAgent.turn_detection_silence_duration_ms !== silenceDuration
    : false;

  // Get available languages for the agent's pricing tier (premium = GPT Realtime)
  const availableLanguages = selectedAgent
    ? getLanguagesForTier(selectedAgent.pricing_tier as "budget" | "balanced" | "premium")
    : getLanguagesForTier("premium");

  const handleSaveSettings = () => {
    saveSettingsMutation.mutate();
  };

  // Flush pending transcript updates
  const flushTranscripts = useCallback(() => {
    if (pendingTranscriptsRef.current.length > 0) {
      const pending = [...pendingTranscriptsRef.current];
      pendingTranscriptsRef.current = [];
      setTranscript((prev) => {
        // Deduplicate by checking existing texts
        const existingTexts = new Set(prev.map((t) => `${t.speaker}:${t.text}`));
        const newItems = pending.filter(
          (item) => !existingTexts.has(`${item.speaker}:${item.text}`)
        );
        return [...prev, ...newItems];
      });
    }
  }, []);

  // Batched addTranscript that groups updates (used only for history_updated bulk processing)
  const addTranscriptBatched = useCallback(
    (speaker: "user" | "assistant" | "system", text: string) => {
      pendingTranscriptsRef.current.push({
        id: crypto.randomUUID(),
        speaker,
        text,
        timestamp: new Date(),
      });

      // Shorter debounce for faster updates
      if (flushTimeoutRef.current) {
        clearTimeout(flushTimeoutRef.current);
      }
      flushTimeoutRef.current = setTimeout(flushTranscripts, 16); // ~1 frame
    },
    [flushTranscripts]
  );

  const cleanup = useCallback(() => {
    // Clear call timer
    if (callTimerRef.current) {
      clearInterval(callTimerRef.current);
      callTimerRef.current = null;
    }

    // Clear any pending flush timeout
    if (flushTimeoutRef.current) {
      clearTimeout(flushTimeoutRef.current);
      flushTimeoutRef.current = null;
    }

    // Flush any remaining transcripts
    flushTranscripts();

    // Clean up WebRTC resources
    const { peerConnection, dataChannel, audioStream, audioElement } = webrtcRef.current;

    if (dataChannel) {
      try {
        dataChannel.close();
      } catch (e) {
        console.error("[Cleanup] Error closing data channel:", e);
      }
    }

    if (peerConnection) {
      try {
        peerConnection.close();
      } catch (e) {
        console.error("[Cleanup] Error closing peer connection:", e);
      }
    }

    if (audioStream) {
      try {
        audioStream.getTracks().forEach((track) => track.stop());
      } catch (e) {
        console.error("[Cleanup] Error stopping audio tracks:", e);
      }
    }

    if (audioElement) {
      try {
        audioElement.srcObject = null;
        audioElement.remove();
      } catch (e) {
        console.error("[Cleanup] Error cleaning up audio element:", e);
      }
    }

    // Reset refs
    webrtcRef.current = {
      peerConnection: null,
      dataChannel: null,
      audioStream: null,
      audioElement: null,
    };
  }, [flushTranscripts]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      cleanup();
    };
  }, [cleanup]);

  // Save transcript to backend
  const saveTranscript = useCallback(async () => {
    const agentId = selectedAgentIdRef.current;

    // Skip if no transcript entries or no session
    if (transcriptEntriesRef.current.length === 0 || !sessionIdRef.current || !agentId) {
      console.log("[Transcript] Skipping save - no transcript entries or session", {
        entriesCount: transcriptEntriesRef.current.length,
        sessionId: sessionIdRef.current,
        agentId,
      });
      return;
    }

    // Format transcript
    const transcriptText = transcriptEntriesRef.current
      .map((entry) => `[${entry.role === "user" ? "User" : "Assistant"}]: ${entry.content}`)
      .join("\n\n");

    // Calculate duration
    const durationSeconds = sessionStartTimeRef.current
      ? Math.floor((Date.now() - sessionStartTimeRef.current) / 1000)
      : 0;

    console.log("[Transcript] Saving transcript", {
      agentId,
      sessionId: sessionIdRef.current,
      entriesCount: transcriptEntriesRef.current.length,
      duration: durationSeconds,
    });

    try {
      const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
      const authToken = localStorage.getItem("access_token");

      const response = await fetch(`${apiBase}/api/v1/realtime/transcript/${agentId}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken && { Authorization: `Bearer ${authToken}` }),
        },
        body: JSON.stringify({
          session_id: sessionIdRef.current,
          transcript: transcriptText,
          duration_seconds: durationSeconds,
        }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        console.error("[Transcript] Failed to save:", response.status, errorText);
      } else {
        console.log("[Transcript] Saved transcript to backend successfully");
      }
    } catch (error) {
      console.error("[Transcript] Failed to save transcript:", error);
    }

    // Reset transcript state
    transcriptEntriesRef.current = [];
    sessionIdRef.current = "";
    sessionStartTimeRef.current = 0;
  }, []);

  // Immediate addTranscript for critical messages (system messages during connect)
  const addTranscriptImmediate = useCallback(
    (speaker: "user" | "assistant" | "system", text: string) => {
      setTranscript((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          speaker,
          text,
          timestamp: new Date(),
        },
      ]);
    },
    []
  );

  // Use batched version for high-frequency updates (history_updated handler)
  const addTranscript = addTranscriptBatched;

  const handleConnect = async () => {
    if (connectionStatus === "connected") {
      // Save transcript before cleanup (fire and forget)
      void saveTranscript();

      // Disconnect
      cleanup();
      setConnectionStatus("idle");
      setAudioStream(null);
      setCallDuration(0);
      addTranscript("system", "Session ended");
      return;
    }

    if (!selectedAgentId) {
      toast.error("Please select an agent first");
      return;
    }

    if (!selectedAgent) {
      toast.error("Agent data not loaded");
      return;
    }

    if (!selectedWorkspaceId) {
      toast.error("Please select a workspace first");
      return;
    }

    // When using "All Workspaces", warn that user-level API keys will be used
    const isAdminMode = selectedWorkspaceId === "all";

    // Set connecting state
    setConnectionStatus("connecting");

    // Initialize transcript tracking
    transcriptEntriesRef.current = [];
    sessionIdRef.current = crypto.randomUUID();
    sessionStartTimeRef.current = Date.now();

    try {
      addTranscriptImmediate("system", `Connecting to ${selectedAgent.name}...`);

      // Fetch ephemeral token from our backend
      // - For specific workspace: include workspace_id to use workspace API keys
      // - For "all" (admin mode): no workspace_id, uses user-level API keys
      const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
      const authToken = localStorage.getItem("access_token");
      const tokenUrl = isAdminMode
        ? `${apiBase}/api/v1/realtime/token/${selectedAgentId}`
        : `${apiBase}/api/v1/realtime/token/${selectedAgentId}?workspace_id=${selectedWorkspaceId}`;
      const tokenResponse = await fetch(tokenUrl, {
        headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
      });

      if (!tokenResponse.ok) {
        const errorText = await tokenResponse.text();
        throw new Error(`Failed to get token: ${errorText}`);
      }

      const tokenData = await tokenResponse.json();
      const ephemeralKey = tokenData.client_secret?.value;

      if (!ephemeralKey) {
        throw new Error("No ephemeral key received from server");
      }

      console.log("[WebRTC] Got ephemeral token:", ephemeralKey.substring(0, 10) + "...");

      // Manual WebRTC connection since SDK doesn't include required OpenAI-Beta header
      const pc = new RTCPeerConnection();
      const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const audioTrack = micStream.getAudioTracks()[0];
      if (audioTrack) {
        pc.addTrack(audioTrack);
      }
      // Store the audio stream for the visualizer
      setAudioStream(micStream);

      // Create data channel for events
      const dataChannel = pc.createDataChannel("oai-events");

      // Set up audio playback
      const audioElement = document.createElement("audio");
      audioElement.autoplay = true;
      pc.ontrack = (event) => {
        audioElement.srcObject = event.streams[0] ?? null;
      };

      // Store WebRTC resources in refs for proper cleanup
      webrtcRef.current = {
        peerConnection: pc,
        dataChannel,
        audioStream: micStream,
        audioElement,
      };

      // Create offer
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // Connect to OpenAI Realtime API with required header
      const response = await fetch("https://api.openai.com/v1/realtime/calls", {
        method: "POST",
        body: offer.sdp,
        headers: {
          "Content-Type": "application/sdp",
          Authorization: `Bearer ${ephemeralKey}`,
        },
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`OpenAI API error (${response.status}): ${errorText}`);
      }

      const answerSdp = await response.text();
      console.log("[WebRTC] Got SDP answer, setting remote description...");

      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
      console.log("[WebRTC] Remote description set!");

      // Handle data channel events
      dataChannel.onopen = () => {
        console.log("[WebRTC] Data channel opened!");
        setConnectionStatus("connected");

        // Start call timer
        setCallDuration(0);
        callTimerRef.current = setInterval(() => {
          setCallDuration((prev) => prev + 1);
        }, 1000);

        // Get tools from token response
        const tools = tokenData.tools ?? [];

        console.log("[WebRTC] Configuring session with", tools.length, "tools");

        // Send session update with agent config and tools
        const instructions = editedSystemPrompt || "You are a helpful voice assistant.";
        const sessionUpdate = {
          type: "session.update",
          session: {
            type: "realtime",
            instructions: instructions,
            audio: {
              input: {
                transcription: {
                  model: "whisper-1",
                  language: getWhisperCode(language) ?? undefined,
                },
                turn_detection:
                  turnDetection === "disabled"
                    ? null
                    : {
                        type: turnDetection === "semantic" ? "semantic_vad" : "server_vad",
                        threshold: threshold,
                        prefix_padding_ms: prefixPadding,
                        silence_duration_ms: silenceDuration,
                      },
              },
              output: {
                voice: voice,
              },
            },
            tools: tools,
            tool_choice: tools.length > 0 ? "auto" : "none",
          },
        };
        dataChannel.send(JSON.stringify(sessionUpdate));
        console.log(
          "[WebRTC] Sent session.update with tools:",
          tools.map((t: { name: string }) => t.name)
        );

        // Trigger initial greeting if one is configured
        const initialGreeting = tokenData.agent?.initial_greeting;
        if (initialGreeting) {
          dataChannel.send(
            JSON.stringify({
              type: "response.create",
              response: {
                instructions: `Start the conversation by saying exactly this (do not add anything else): "${initialGreeting}"`,
              },
            })
          );
          console.log("[WebRTC] Sent initial greeting:", initialGreeting);
        }
      };

      dataChannel.onmessage = async (event) => {
        try {
          const data = JSON.parse(event.data);
          console.log("[WebRTC] Event:", data.type, data);

          // Handle session.updated confirmation
          if (data.type === "session.updated") {
            const toolsConfigured = data.session?.tools?.length ?? 0;
            console.log("[WebRTC] Session updated - tools configured:", toolsConfigured);
          }

          // Handle transcription - use immediate updates for real-time feel
          if (data.type === "conversation.item.input_audio_transcription.completed") {
            addTranscriptImmediate("user", data.transcript);
            // Also capture for saving to backend
            if (data.transcript?.trim()) {
              transcriptEntriesRef.current.push({
                role: "user",
                content: data.transcript.trim(),
              });
            }
          } else if (data.type === "response.audio_transcript.done") {
            addTranscriptImmediate("assistant", data.transcript);
            // Also capture for saving to backend
            if (data.transcript?.trim()) {
              transcriptEntriesRef.current.push({
                role: "assistant",
                content: data.transcript.trim(),
              });
            }
          } else if (data.type === "response.function_call_arguments.done") {
            // Handle function/tool call
            const { call_id, name, arguments: argsJson } = data;
            console.log("[WebRTC] Function call:", name, argsJson);

            try {
              // Execute tool via backend API
              const toolResponse = await fetch(`${apiBase}/api/v1/tools/execute`, {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  ...(authToken && { Authorization: `Bearer ${authToken}` }),
                },
                body: JSON.stringify({
                  tool_name: name,
                  arguments: JSON.parse(argsJson),
                  agent_id: selectedAgentId,
                }),
              });

              const toolResult = await toolResponse.json();
              console.log("[WebRTC] Tool result:", toolResult);

              // Send function call output back to the model
              const outputEvent = {
                type: "conversation.item.create",
                item: {
                  type: "function_call_output",
                  call_id: call_id,
                  output: JSON.stringify(toolResult),
                },
              };
              dataChannel.send(JSON.stringify(outputEvent));

              // Trigger response generation
              const responseCreate = { type: "response.create" };
              dataChannel.send(JSON.stringify(responseCreate));

              // Handle call control actions
              if (toolResult.action === "end_call") {
                console.log("[WebRTC] End call action received, scheduling cleanup");
                // Wait a bit for the AI to finish its farewell, then end the session
                setTimeout(() => {
                  console.log("[WebRTC] Executing end_call cleanup");
                  void saveTranscript();
                  cleanup();
                  setConnectionStatus("idle");
                  setAudioStream(null);
                  setCallDuration(0);
                }, 3000); // 3 second delay for farewell
              }
            } catch (toolError) {
              console.error("[WebRTC] Tool execution error:", toolError);
              // Send error output
              const errorOutput = {
                type: "conversation.item.create",
                item: {
                  type: "function_call_output",
                  call_id: call_id,
                  output: JSON.stringify({ success: false, error: String(toolError) }),
                },
              };
              dataChannel.send(JSON.stringify(errorOutput));
              dataChannel.send(JSON.stringify({ type: "response.create" }));
            }
          } else if (data.type === "error") {
            console.error("[WebRTC] Error event:", data);
            addTranscript("system", `Error: ${data.error?.message ?? "Unknown error"}`);
          }
        } catch (e) {
          console.error("[WebRTC] Failed to parse message:", e);
        }
      };

      dataChannel.onerror = (event) => {
        console.error("[WebRTC] Data channel error:", event);
      };

      dataChannel.onclose = () => {
        console.log("[WebRTC] Data channel closed");
        setConnectionStatus("idle");
        setAudioStream(null);
        if (callTimerRef.current) {
          clearInterval(callTimerRef.current);
          callTimerRef.current = null;
        }
      };

      pc.onconnectionstatechange = () => {
        console.log("[WebRTC] Connection state:", pc.connectionState);
        if (pc.connectionState === "disconnected" || pc.connectionState === "failed") {
          void saveTranscript();
          cleanup();
          setConnectionStatus("idle");
          setAudioStream(null);
          setCallDuration(0);
        }
      };
    } catch (error: unknown) {
      const err = error as Error;
      console.error("[WebRTC] Connection error:", err);
      addTranscript("system", `Error: ${err.message}`);
      cleanup();
      setConnectionStatus("idle");
      setAudioStream(null);

      if (err.name === "NotAllowedError") {
        toast.error(
          "Microphone access denied. Please allow microphone access in your browser settings."
        );
      } else {
        toast.error(`Connection failed: ${err.message}`);
      }
    }
  };

  const handleConnectClick = () => {
    void handleConnect();
  };

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
  };

  return (
    <div className="-m-4 flex min-h-0 flex-1 md:-m-6 lg:-m-8">
      {/* Main Content */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left - Transcript with floating controls */}
        <div className="relative flex min-h-0 flex-1 flex-col">
          <ScrollArea className="min-h-0 flex-1 p-4 pb-16">
            {transcript.length === 0 ? (
              <div className="flex h-[calc(100vh-14rem)] items-center justify-center text-muted-foreground">
                Start a session to see the conversation
              </div>
            ) : (
              <div className="space-y-4">
                {transcript.map((item) => (
                  <div key={item.id}>
                    {item.speaker === "system" ? (
                      <div className="flex justify-center">
                        <div className="animate-[fadeIn_0.3s_ease-out_forwards] rounded-full bg-muted/50 px-3 py-1 text-xs text-muted-foreground opacity-0">
                          {item.text}
                        </div>
                      </div>
                    ) : (
                      <div
                        className={`flex ${item.speaker === "user" ? "justify-end" : "justify-start"}`}
                      >
                        <div
                          className={`max-w-[80%] space-y-1 ${item.speaker === "user" ? "items-end" : "items-start"}`}
                        >
                          <div
                            className={`flex animate-[fadeIn_0.2s_ease-out_forwards] items-center gap-2 text-[10px] font-medium uppercase tracking-wider opacity-0 ${
                              item.speaker === "user" ? "justify-end" : "justify-start"
                            } text-muted-foreground`}
                          >
                            <span>{item.speaker === "user" ? "You" : "Assistant"}</span>
                            <span className="opacity-50">
                              {item.timestamp.toLocaleTimeString([], {
                                hour: "2-digit",
                                minute: "2-digit",
                              })}
                            </span>
                          </div>
                          <div
                            className={`animate-[fadeIn_0.4s_ease-out_0.1s_forwards] rounded-2xl px-4 py-2.5 text-sm leading-relaxed opacity-0 ${
                              item.speaker === "user"
                                ? "rounded-br-md bg-primary text-primary-foreground"
                                : "rounded-bl-md bg-muted"
                            }`}
                          >
                            {item.text}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
                <div ref={transcriptEndRef} />
              </div>
            )}
          </ScrollArea>

          {/* Floating Control Bar */}
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2">
            <div className="flex items-center gap-3 rounded-full border bg-background/95 px-4 py-2 shadow-lg backdrop-blur-sm">
              <div className="flex items-center gap-2 font-mono text-sm text-muted-foreground">
                <span>{formatDuration(callDuration)}</span>
                <AudioVisualizer
                  stream={audioStream}
                  isActive={connectionStatus === "connected"}
                  barCount={12}
                />
              </div>

              <Button
                onClick={handleConnectClick}
                variant={connectionStatus === "connected" ? "destructive" : "default"}
                size="sm"
                className="gap-2 rounded-full"
                disabled={
                  (!selectedAgentId && connectionStatus === "idle") ||
                  connectionStatus === "connecting"
                }
              >
                {connectionStatus === "idle" && (
                  <>
                    <Play className="h-4 w-4" />
                    Start session
                  </>
                )}
                {connectionStatus === "connecting" && (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Connecting...
                  </>
                )}
                {connectionStatus === "connected" && (
                  <>
                    <Square className="h-3 w-3 fill-current" />
                    Stop
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>

        {/* Right - Settings Panel */}
        <div className="flex w-[320px] shrink-0 flex-col border-l bg-muted/20">
          <div className="flex-1 space-y-4 overflow-y-auto p-4">
            {/* Workspace Selection */}
            <div className="space-y-2">
              <Label className="text-sm font-medium">
                <span className="flex items-center gap-2">
                  <FolderOpen className="h-4 w-4" />
                  Workspace
                </span>
              </Label>
              <Select value={selectedWorkspaceId} onValueChange={setSelectedWorkspaceId}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a workspace" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Workspaces (Admin)</SelectItem>
                  {workspaces.map((ws) => (
                    <SelectItem key={ws.id} value={ws.id}>
                      {ws.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Agent Selection */}
            <div className="space-y-2">
              <Label className="text-sm font-medium">Agent</Label>
              <Select
                value={selectedAgentId}
                onValueChange={setSelectedAgentId}
                disabled={!selectedWorkspaceId}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={
                      !selectedWorkspaceId
                        ? "Select a workspace first"
                        : availableAgents.length === 0
                          ? selectedWorkspaceId === "all"
                            ? "No premium agents found"
                            : "No agents in this workspace"
                          : "Select an agent"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {availableAgents.map((agent) => (
                    <SelectItem key={agent.agent_id} value={agent.agent_id}>
                      {agent.agent_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {selectedWorkspaceId &&
                selectedWorkspaceId !== "all" &&
                availableAgents.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    Add agents to this workspace in{" "}
                    <Link href="/dashboard/workspaces" className="text-primary hover:underline">
                      Workspaces
                    </Link>
                  </p>
                )}
            </div>

            <Separator />

            {/* System Instructions */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label className="text-sm font-medium">System instructions</Label>
                {hasUnsavedChanges && selectedAgentId && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 gap-1 text-xs"
                    onClick={handleSaveSettings}
                    disabled={saveSettingsMutation.isPending}
                  >
                    <Save className="h-3 w-3" />
                    {saveSettingsMutation.isPending ? "Saving..." : "Save"}
                  </Button>
                )}
              </div>
              {selectedAgentId ? (
                <Textarea
                  value={editedSystemPrompt}
                  onChange={(e) => setEditedSystemPrompt(e.target.value)}
                  className="h-[80px] resize-none text-xs"
                  placeholder="Enter system instructions..."
                />
              ) : (
                <div className="flex h-[80px] items-center justify-center rounded-md border bg-muted/50 p-2 text-xs italic text-muted-foreground">
                  Select an agent to edit system instructions
                </div>
              )}
            </div>

            <Separator />

            {/* Voice */}
            <div className="space-y-2">
              <Label className="text-sm font-medium">Voice</Label>
              <Select value={voice} onValueChange={setVoice}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="marin">Marin (Recommended)</SelectItem>
                  <SelectItem value="cedar">Cedar (Recommended)</SelectItem>
                  <SelectItem value="shimmer">Shimmer</SelectItem>
                  <SelectItem value="alloy">Alloy</SelectItem>
                  <SelectItem value="coral">Coral</SelectItem>
                  <SelectItem value="echo">Echo</SelectItem>
                  <SelectItem value="sage">Sage</SelectItem>
                  <SelectItem value="verse">Verse</SelectItem>
                  <SelectItem value="ash">Ash</SelectItem>
                  <SelectItem value="ballad">Ballad</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label className="text-sm font-medium">
                Language ({availableLanguages.length} available)
              </Label>
              <Select value={language} onValueChange={setLanguage}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="max-h-[300px]">
                  {availableLanguages.map((lang) => (
                    <SelectItem key={lang.code} value={lang.code}>
                      {lang.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <Separator />

            {/* Turn Detection */}
            <div className="space-y-3">
              <Label className="text-sm font-medium">Automatic turn detection</Label>
              <ToggleGroup
                options={[
                  { value: "normal", label: "Normal" },
                  { value: "semantic", label: "Semantic" },
                  { value: "disabled", label: "Disabled" },
                ]}
                value={turnDetection}
                onChange={setTurnDetection}
              />

              {turnDetection !== "disabled" && (
                <div className="space-y-3">
                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-muted-foreground">Threshold</span>
                      <span>{threshold.toFixed(2)}</span>
                    </div>
                    <Slider
                      value={[threshold]}
                      onValueChange={(values) => setThreshold(values[0] ?? 0.5)}
                      min={0}
                      max={1}
                      step={0.01}
                    />
                  </div>

                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-muted-foreground">Prefix padding</span>
                      <span>{prefixPadding} ms</span>
                    </div>
                    <Slider
                      value={[prefixPadding]}
                      onValueChange={(values) => setPrefixPadding(values[0] ?? 300)}
                      min={0}
                      max={1000}
                      step={10}
                    />
                  </div>

                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-muted-foreground">Silence duration</span>
                      <span>{silenceDuration} ms</span>
                    </div>
                    <Slider
                      value={[silenceDuration]}
                      onValueChange={(values) => setSilenceDuration(values[0] ?? 500)}
                      min={0}
                      max={2000}
                      step={50}
                    />
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-sm text-muted-foreground">Idle timeout</span>
                    <Switch checked={idleTimeout} onCheckedChange={setIdleTimeout} />
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
