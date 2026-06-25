"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { Mic, MicOff, X, Phone } from "lucide-react";
import { useParams, useSearchParams } from "next/navigation";

interface AgentConfig {
  public_id: string;
  name: string;
  greeting_message: string;
  button_text: string;
  theme: "light" | "dark" | "auto";
  position: string;
  primary_color: string;
  language: string;
  voice: string;
}

interface ToolDefinition {
  type: "function";
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

interface TokenResponse {
  client_secret: { value: string };
  agent: {
    name: string;
    voice: string;
    instructions: string;
    language: string;
    initial_greeting: string | null;
  };
  model: string;
  tools?: ToolDefinition[];
}

type ConnectionStatus = "idle" | "connecting" | "connected" | "error";
type AgentState = "idle" | "listening" | "thinking" | "speaking";

// WebRTC resources for proper cleanup
type WebRTCResources = {
  peerConnection: RTCPeerConnection | null;
  dataChannel: RTCDataChannel | null;
  audioStream: MediaStream | null;
  audioElement: HTMLAudioElement | null;
};

// Audio analysis resources
type AudioResources = {
  audioContext: AudioContext | null;
  analyser: AnalyserNode | null;
  dataArray: Uint8Array<ArrayBuffer> | null;
  animationFrame: number | null;
};

// Transcript entry type
type TranscriptEntry = {
  role: "user" | "assistant";
  content: string;
};

// Number of bars in the audio visualizer
const BAR_COUNT = 24;

export default function EmbedPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const publicId = params.publicId as string;
  const theme = (searchParams.get("theme") as "light" | "dark" | "auto") ?? "auto";
  const position = searchParams.get("position") ?? "bottom-right";
  const autostart = searchParams.get("autostart") === "true";

  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [isMuted, setIsMuted] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const [agentState, setAgentState] = useState<AgentState>("idle");
  const [frequencies, setFrequencies] = useState<number[]>(new Array(BAR_COUNT).fill(0));
  const [smoothedLevel, setSmoothedLevel] = useState(0);

  // WebRTC resource refs for proper cleanup
  const webrtcRef = useRef<WebRTCResources>({
    peerConnection: null,
    dataChannel: null,
    audioStream: null,
    audioElement: null,
  });

  // Audio analysis refs
  const audioRef = useRef<AudioResources>({
    audioContext: null,
    analyser: null,
    dataArray: null,
    animationFrame: null,
  });

  // AbortController for cancelling connection
  const abortControllerRef = useRef<AbortController | null>(null);

  // Transcript tracking
  const transcriptRef = useRef<TranscriptEntry[]>([]);
  const currentAssistantTextRef = useRef<string>("");
  const sessionIdRef = useRef<string>("");
  const sessionStartTimeRef = useRef<number>(0);

  // Autostart tracking (prevent multiple starts)
  const autostartTriggeredRef = useRef(false);

  // Detect system theme
  const [resolvedTheme, setResolvedTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    if (theme === "auto") {
      const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
      setResolvedTheme(mediaQuery.matches ? "dark" : "light");
      const handler = (e: MediaQueryListEvent) => setResolvedTheme(e.matches ? "dark" : "light");
      mediaQuery.addEventListener("change", handler);
      return () => mediaQuery.removeEventListener("change", handler);
    }
    setResolvedTheme(theme);
    return undefined;
  }, [theme]);

  // Notify parent window of state changes (for widget button)
  useEffect(() => {
    if (window.parent !== window) {
      window.parent.postMessage({ type: "voice-agent:state", state: agentState }, "*");
    }
  }, [agentState]);

  // Notify parent window of audio level (for widget button effects)
  useEffect(() => {
    if (window.parent !== window && smoothedLevel > 0.05) {
      window.parent.postMessage({ type: "voice-agent:audio-level", level: smoothedLevel }, "*");
    }
  }, [smoothedLevel]);

  // Fetch agent config
  useEffect(() => {
    async function fetchConfig() {
      try {
        const res = await fetch(`/api/public/embed/${publicId}/config`, {
          headers: { Origin: window.location.origin },
        });
        if (!res.ok) {
          const data = await res.json();
          throw new Error((data.detail as string) ?? "Failed to load agent");
        }
        const data = await res.json();
        setConfig(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load agent");
      }
    }
    if (publicId) {
      void fetchConfig();
    }
  }, [publicId]);

  // Audio analysis setup with frequency bands for visualizer
  const setupAudioAnalysis = useCallback((stream: MediaStream) => {
    try {
      const audioContext = new AudioContext();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 128;
      analyser.smoothingTimeConstant = 0.7;

      const source = audioContext.createMediaStreamSource(stream);
      source.connect(analyser);

      const dataArray = new Uint8Array(analyser.frequencyBinCount);

      audioRef.current = {
        audioContext,
        analyser,
        dataArray,
        animationFrame: null,
      };

      // Start audio level monitoring with frequency bands
      const updateLevel = () => {
        if (audioRef.current.analyser && audioRef.current.dataArray) {
          audioRef.current.analyser.getByteFrequencyData(audioRef.current.dataArray);
          const data = audioRef.current.dataArray;

          // Calculate frequency bands for visualizer bars
          const bands: number[] = [];
          const bufferLength = data.length;

          for (let i = 0; i < BAR_COUNT; i++) {
            // Use logarithmic distribution to favor lower frequencies (voice range)
            const startIndex = Math.floor(Math.pow(i / BAR_COUNT, 1.5) * bufferLength);
            const endIndex = Math.floor(Math.pow((i + 1) / BAR_COUNT, 1.5) * bufferLength);

            let sum = 0;
            const count = Math.max(1, endIndex - startIndex);
            for (let j = startIndex; j < endIndex && j < bufferLength; j++) {
              sum += data[j] ?? 0;
            }

            // Normalize to 0-1 range with boost
            const avg = sum / count / 255;
            bands.push(Math.min(1, avg * 1.8));
          }

          setFrequencies(bands);

          // Calculate overall level for glow effects
          const voiceRange = Array.from(data).slice(0, 16);
          const avg = voiceRange.reduce((a, b) => a + b, 0) / voiceRange.length;
          const normalizedLevel = Math.min(avg / 128, 1);

          // Smooth the level
          setSmoothedLevel((prev) => {
            if (normalizedLevel > prev) {
              return prev + (normalizedLevel - prev) * 0.3; // Quick rise
            } else {
              return prev + (normalizedLevel - prev) * 0.1; // Slower decay
            }
          });
        }

        audioRef.current.animationFrame = requestAnimationFrame(updateLevel);
      };

      updateLevel();
    } catch {
      // Audio analysis not critical, continue without it
    }
  }, []);

  // Cleanup audio analysis
  const cleanupAudioAnalysis = useCallback(() => {
    if (audioRef.current.animationFrame) {
      cancelAnimationFrame(audioRef.current.animationFrame);
    }
    if (audioRef.current.audioContext) {
      try {
        void audioRef.current.audioContext.close();
      } catch {
        // Ignore cleanup errors
      }
    }
    audioRef.current = {
      audioContext: null,
      analyser: null,
      dataArray: null,
      animationFrame: null,
    };
    setFrequencies(new Array(BAR_COUNT).fill(0));
    setSmoothedLevel(0);
  }, []);

  // Save transcript to backend
  const saveTranscript = useCallback(async () => {
    // Flush any remaining assistant text
    if (currentAssistantTextRef.current.trim()) {
      transcriptRef.current.push({
        role: "assistant",
        content: currentAssistantTextRef.current.trim(),
      });
      currentAssistantTextRef.current = "";
    }

    // Skip if no transcript entries or no session
    if (transcriptRef.current.length === 0 || !sessionIdRef.current) {
      return;
    }

    // Format transcript
    const transcriptText = transcriptRef.current
      .map((entry) => `[${entry.role === "user" ? "User" : "Assistant"}]: ${entry.content}`)
      .join("\n\n");

    // Calculate duration
    const durationSeconds = sessionStartTimeRef.current
      ? Math.floor((Date.now() - sessionStartTimeRef.current) / 1000)
      : 0;

    try {
      await fetch(`/api/public/embed/${publicId}/transcript`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Origin: window.location.origin,
        },
        body: JSON.stringify({
          session_id: sessionIdRef.current,
          transcript: transcriptText,
          duration_seconds: durationSeconds,
        }),
      });
    } catch {
      // Silently fail - transcript saving is not critical
    }

    // Reset transcript state
    transcriptRef.current = [];
    sessionIdRef.current = "";
    sessionStartTimeRef.current = 0;
  }, [publicId]);

  // Cleanup function
  const cleanup = useCallback(() => {
    const { peerConnection, dataChannel, audioStream, audioElement } = webrtcRef.current;

    cleanupAudioAnalysis();

    if (dataChannel) {
      try {
        dataChannel.close();
      } catch {
        // Ignore cleanup errors
      }
    }

    if (peerConnection) {
      try {
        peerConnection.close();
      } catch {
        // Ignore cleanup errors
      }
    }

    if (audioStream) {
      try {
        audioStream.getTracks().forEach((track) => track.stop());
      } catch {
        // Ignore cleanup errors
      }
    }

    if (audioElement) {
      try {
        audioElement.srcObject = null;
        audioElement.remove();
      } catch {
        // Ignore cleanup errors
      }
    }

    // Reset refs
    webrtcRef.current = {
      peerConnection: null,
      dataChannel: null,
      audioStream: null,
      audioElement: null,
    };

    setAgentState("idle");
  }, [cleanupAudioAnalysis]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      cleanup();
    };
  }, [cleanup]);

  // End voice session
  const endSession = useCallback(() => {
    // Abort any in-progress connection
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    // Save transcript before cleanup (fire and forget)
    void saveTranscript();

    cleanup();
    setStatus("idle");
    setIsExpanded(false);

    // Notify parent widget to close (for external widget integration)
    if (window.parent !== window) {
      window.parent.postMessage({ type: "voice-agent:close" }, "*");
    }
  }, [cleanup, saveTranscript]);

  // Start voice session using manual WebRTC
  const startSession = useCallback(async () => {
    if (!config) return;

    // Create AbortController for this connection attempt
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    // Initialize transcript tracking
    transcriptRef.current = [];
    currentAssistantTextRef.current = "";
    sessionIdRef.current = crypto.randomUUID();
    sessionStartTimeRef.current = Date.now();

    setStatus("connecting");
    setError(null);
    setIsExpanded(true);
    setAgentState("idle");

    try {
      // Get ephemeral token from backend
      const tokenRes = await fetch(`/api/public/embed/${publicId}/token`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Origin: window.location.origin,
        },
        signal: abortController.signal,
      });

      // Check if cancelled
      if (abortController.signal.aborted) return;

      if (!tokenRes.ok) {
        const errData = await tokenRes.json();
        throw new Error((errData.detail as string) ?? "Failed to get token");
      }

      const tokenData: TokenResponse = await tokenRes.json();
      const ephemeralKey = tokenData.client_secret.value;

      // Check if cancelled before WebRTC setup
      if (abortController.signal.aborted) return;

      // Manual WebRTC connection
      const pc = new RTCPeerConnection();
      const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const audioTrack = micStream.getAudioTracks()[0];
      if (audioTrack) {
        pc.addTrack(audioTrack);
      }

      // Setup audio analysis for visualization
      setupAudioAnalysis(micStream);

      // Create data channel for events
      const dataChannel = pc.createDataChannel("oai-events");

      // Set up audio playback
      const audioElement = document.createElement("audio");
      audioElement.autoplay = true;
      pc.ontrack = (event) => {
        audioElement.srcObject = event.streams[0] ?? null;
      };

      // Store WebRTC resources for cleanup
      webrtcRef.current = {
        peerConnection: pc,
        dataChannel,
        audioStream: micStream,
        audioElement,
      };

      // Check if cancelled after mic access
      if (abortController.signal.aborted) {
        // Clean up resources we just created
        micStream.getTracks().forEach((track) => track.stop());
        pc.close();
        return;
      }

      // Create offer
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // Check if cancelled before OpenAI call
      if (abortController.signal.aborted) {
        micStream.getTracks().forEach((track) => track.stop());
        pc.close();
        return;
      }

      // Connect to OpenAI Realtime API with required header
      const response = await fetch("https://api.openai.com/v1/realtime/calls", {
        method: "POST",
        body: offer.sdp,
        headers: {
          "Content-Type": "application/sdp",
          Authorization: `Bearer ${ephemeralKey}`,
          "OpenAI-Beta": "realtime=v1",
        },
        signal: abortController.signal,
      });

      // Check if cancelled
      if (abortController.signal.aborted) {
        micStream.getTracks().forEach((track) => track.stop());
        pc.close();
        return;
      }

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`OpenAI API error (${response.status}): ${errorText}`);
      }

      const answerSdp = await response.text();

      // Check if cancelled before setting remote description
      if (abortController.signal.aborted) {
        micStream.getTracks().forEach((track) => track.stop());
        pc.close();
        return;
      }

      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

      // Store tools for later reference
      const agentTools = tokenData.tools ?? [];

      // Handle data channel events
      dataChannel.onopen = () => {
        // Check if cancelled before sending session config
        if (abortController.signal.aborted) return;
        setStatus("connected");
        setAgentState("listening");

        // Build session config with tools
        const sessionConfig: Record<string, unknown> = {
          instructions: tokenData.agent.instructions,
          voice: tokenData.agent.voice,
          input_audio_transcription: {
            model: "whisper-1",
          },
          turn_detection: {
            type: "server_vad",
            threshold: 0.5,
            prefix_padding_ms: 300,
            silence_duration_ms: 200,
          },
        };

        // Add tools if available
        if (agentTools.length > 0) {
          sessionConfig.tools = agentTools;
          sessionConfig.tool_choice = "auto";
        }

        const sessionUpdate = {
          type: "session.update",
          session: sessionConfig,
        };
        dataChannel.send(JSON.stringify(sessionUpdate));

        // Only trigger initial greeting if one is configured
        // Without this, the agent waits for the user to speak first
        if (tokenData.agent.initial_greeting) {
          dataChannel.send(
            JSON.stringify({
              type: "response.create",
              response: {
                instructions: `Start the conversation by saying exactly this (do not add anything else): "${tokenData.agent.initial_greeting}"`,
              },
            })
          );
        }
      };

      // Helper to execute tool calls via backend
      const executeToolCall = async (
        callId: string,
        toolName: string,
        args: Record<string, unknown>
      ) => {
        try {
          const response = await fetch(`/api/public/embed/${publicId}/tool-call`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Origin: window.location.origin,
            },
            body: JSON.stringify({
              tool_name: toolName,
              arguments: args,
            }),
          });

          const result = (await response.json()) as Record<string, unknown>;

          // Send tool result back to OpenAI
          if (dataChannel.readyState === "open") {
            dataChannel.send(
              JSON.stringify({
                type: "conversation.item.create",
                item: {
                  type: "function_call_output",
                  call_id: callId,
                  output: JSON.stringify(result),
                },
              })
            );

            // Trigger response generation after tool result
            dataChannel.send(JSON.stringify({ type: "response.create" }));
          }

          // Handle call control actions (matching test page behavior)
          if (result.action === "end_call") {
            // Wait for the AI to finish its farewell, then end the session
            setTimeout(() => {
              endSession();
            }, 3000); // 3 second delay for farewell
          }
        } catch (err) {
          // Send error result back to OpenAI
          if (dataChannel.readyState === "open") {
            dataChannel.send(
              JSON.stringify({
                type: "conversation.item.create",
                item: {
                  type: "function_call_output",
                  call_id: callId,
                  output: JSON.stringify({
                    success: false,
                    error: err instanceof Error ? err.message : "Tool execution failed",
                  }),
                },
              })
            );
            dataChannel.send(JSON.stringify({ type: "response.create" }));
          }
        }
      };

      dataChannel.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);

          // Track agent state based on events
          if (data.type === "input_audio_buffer.speech_started") {
            setAgentState("listening");
          } else if (data.type === "input_audio_buffer.speech_stopped") {
            setAgentState("thinking");
          } else if (data.type === "response.audio.delta") {
            setAgentState("speaking");
          } else if (data.type === "response.audio.done") {
            setAgentState("listening");
          } else if (data.type === "response.done") {
            setAgentState("listening");
          } else if (data.type === "response.function_call_arguments.done") {
            // Handle tool calls by proxying to backend
            const callId = data.call_id as string;
            const toolName = data.name as string;
            const args = JSON.parse(data.arguments as string) as Record<string, unknown>;

            // Execute tool in background
            void executeToolCall(callId, toolName, args);
          } else if (data.type === "error") {
            setError(data.error?.message ?? "Unknown error");
          }

          // Capture transcript events
          if (data.type === "conversation.item.input_audio_transcription.completed") {
            // User speech transcription completed
            const userText = data.transcript as string;
            if (userText?.trim()) {
              transcriptRef.current.push({ role: "user", content: userText.trim() });
            }
          } else if (data.type === "response.audio_transcript.delta") {
            // Assistant speech transcript delta
            const delta = data.delta as string;
            if (delta) {
              currentAssistantTextRef.current += delta;
            }
          } else if (data.type === "response.audio_transcript.done") {
            // Assistant speech transcript complete - flush to transcript
            if (currentAssistantTextRef.current.trim()) {
              transcriptRef.current.push({
                role: "assistant",
                content: currentAssistantTextRef.current.trim(),
              });
            }
            currentAssistantTextRef.current = "";
          }
        } catch {
          // Ignore parse errors
        }
      };

      dataChannel.onerror = () => {
        setError("Connection error");
        setStatus("error");
      };

      dataChannel.onclose = () => {
        setStatus("idle");
        setIsExpanded(false);
      };

      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "disconnected" || pc.connectionState === "failed") {
          cleanup();
          setStatus("idle");
          setIsExpanded(false);
        }
      };
    } catch (err) {
      // Don't show error if it was an intentional cancellation
      if (err instanceof Error && err.name === "AbortError") {
        // Silently ignore abort errors - user cancelled
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to start session");
      setStatus("error");
      cleanup();
    }
  }, [config, publicId, cleanup, setupAudioAnalysis, endSession]);

  // Auto-start session when in widget mode
  useEffect(() => {
    if (autostart && config && !autostartTriggeredRef.current) {
      autostartTriggeredRef.current = true;
      void startSession();
    }
  }, [autostart, config, startSession]);

  // Listen for start message from widget (for restart after close)
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.data?.type === "voice-agent:start" && status === "idle" && config) {
        void startSession();
      }
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [status, config, startSession]);

  // Toggle mute
  const toggleMute = useCallback(() => {
    const { audioStream } = webrtcRef.current;
    if (audioStream) {
      const newMuted = !isMuted;
      setIsMuted(newMuted);
      audioStream.getAudioTracks().forEach((track) => {
        track.enabled = !newMuted;
      });
    }
  }, [isMuted]);

  // Position classes
  const positionClasses: Record<string, string> = {
    "bottom-right": "bottom-8 right-5",
    "bottom-left": "bottom-8 left-5",
    "top-right": "top-8 right-5",
    "top-left": "top-8 left-5",
  };

  const isDark = resolvedTheme === "dark";
  const primaryColor = config?.primary_color ?? "#6366f1";

  // Get state-based colors
  const getStateColor = () => {
    switch (agentState) {
      case "listening":
        return { color: "#22c55e", name: "Listening" }; // green
      case "thinking":
        return { color: "#f59e0b", name: "Thinking" }; // amber
      case "speaking":
        return { color: "#3b82f6", name: "Speaking" }; // blue
      default:
        return { color: primaryColor, name: "Ready" };
    }
  };

  const stateInfo = getStateColor();

  // Error state
  if (error && !config) {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-transparent">
        <div className="rounded-lg bg-red-50 p-4 text-red-600 shadow-lg">
          <p className="text-sm">{error}</p>
        </div>
      </div>
    );
  }

  // Loading state
  if (!config) {
    return (
      <div className={`fixed ${positionClasses[position] ?? positionClasses["bottom-right"]}`}>
        <div className="h-14 w-14 animate-pulse rounded-full bg-gray-200" />
      </div>
    );
  }

  return (
    <div
      className={`fixed ${positionClasses[position] ?? positionClasses["bottom-right"]} z-[9999] font-sans`}
    >
      {/* Expanded state - voice controls with audio visualizer */}
      {isExpanded ? (
        <div
          className="relative flex flex-col items-center gap-3 rounded-3xl p-6 transition-all duration-500"
          style={{
            backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
            backdropFilter: "blur(20px)",
            boxShadow: `0 0 ${40 + smoothedLevel * 60}px ${smoothedLevel * 20}px ${stateInfo.color}40`,
          }}
        >
          {/* Circular Audio Visualizer */}
          <div className="relative flex h-32 w-32 items-center justify-center">
            {/* Outer glow ring */}
            <div
              className="absolute inset-0 rounded-full"
              style={{
                background: `radial-gradient(circle, ${stateInfo.color}20 0%, transparent 70%)`,
                transform: `scale(${1.2 + smoothedLevel * 0.5})`,
                transition: "transform 0.15s ease-out",
              }}
            />

            {/* Audio bars in a circle */}
            <div className="absolute inset-0">
              {frequencies.map((level, i) => {
                const angle = (i / BAR_COUNT) * 360;
                const barHeight = 8 + level * 32;
                const opacity = 0.3 + level * 0.7;

                return (
                  <div
                    key={i}
                    className="absolute left-1/2 top-1/2 origin-bottom"
                    style={{
                      width: "3px",
                      height: `${barHeight}px`,
                      backgroundColor: stateInfo.color,
                      opacity,
                      transform: `translate(-50%, -100%) rotate(${angle}deg) translateY(-28px)`,
                      borderRadius: "2px",
                      transition: "height 0.05s ease-out, opacity 0.05s ease-out",
                    }}
                  />
                );
              })}
            </div>

            {/* Center orb */}
            <div
              className="relative z-10 flex h-16 w-16 items-center justify-center rounded-full"
              style={{
                background: `linear-gradient(135deg, ${stateInfo.color} 0%, ${primaryColor} 100%)`,
                boxShadow: `0 0 ${20 + smoothedLevel * 30}px ${stateInfo.color}80`,
                transform: `scale(${1 + smoothedLevel * 0.15})`,
                transition: "transform 0.1s ease-out, box-shadow 0.1s ease-out",
              }}
            >
              {/* Inner pulse effect */}
              <div
                className="absolute inset-2 rounded-full"
                style={{
                  background: `radial-gradient(circle, rgba(255,255,255,${0.3 + smoothedLevel * 0.4}) 0%, transparent 70%)`,
                }}
              />

              {/* Icon */}
              {status === "connecting" ? (
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
              ) : (
                <Phone className="h-6 w-6 text-white" fill="white" />
              )}
            </div>

            {/* State indicator ring */}
            <div
              className="absolute inset-0 rounded-full border-2"
              style={{
                borderColor: stateInfo.color,
                opacity: 0.5 + smoothedLevel * 0.5,
                transform: `scale(${1.1 + smoothedLevel * 0.1})`,
                transition: "transform 0.15s ease-out, opacity 0.15s ease-out",
              }}
            />
          </div>

          {/* Agent name and status */}
          <div className="text-center">
            <p className="text-sm font-semibold" style={{ color: isDark ? "#ffffff" : "#1f2937" }}>
              {config.name}
            </p>
            <p className="text-xs font-medium" style={{ color: stateInfo.color }}>
              {status === "connecting" ? "Connecting..." : stateInfo.name}
            </p>
          </div>

          {/* Control buttons */}
          <div className="flex items-center gap-2">
            {/* Mute button */}
            {status === "connected" && (
              <button
                onClick={toggleMute}
                className="rounded-full p-3 transition-all duration-200 hover:scale-105"
                style={{
                  backgroundColor: isMuted
                    ? "#ef4444"
                    : isDark
                      ? "rgba(255,255,255,0.1)"
                      : "rgba(0,0,0,0.05)",
                  color: isMuted ? "#ffffff" : isDark ? "#d1d5db" : "#4b5563",
                }}
              >
                {isMuted ? <MicOff className="h-5 w-5" /> : <Mic className="h-5 w-5" />}
              </button>
            )}

            {/* End call button */}
            <button
              onClick={endSession}
              className="rounded-full bg-red-500 p-3 text-white transition-all duration-200 hover:scale-105 hover:bg-red-600"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Error message */}
          {error && <p className="max-w-[200px] text-center text-xs text-red-500">{error}</p>}
        </div>
      ) : autostart ? (
        /* Autostart mode - show connecting indicator while session starts */
        <div className="flex flex-col items-center justify-center gap-3 p-4">
          <div className="relative flex h-16 w-16 items-center justify-center">
            <div
              className="absolute inset-0 animate-spin rounded-full"
              style={{
                background: `conic-gradient(from 0deg, ${primaryColor} 0deg, transparent 120deg)`,
                animationDuration: "1s",
              }}
            />
            <div className="absolute inset-1 rounded-full bg-[#212121]" />
            <div
              className="absolute inset-3 rounded-full"
              style={{ backgroundColor: primaryColor, opacity: 0.3 }}
            />
          </div>
          <p className="text-sm font-medium text-gray-300">
            {status === "connecting" ? "Connecting..." : "Starting..."}
          </p>
          {error && <p className="max-w-[200px] text-center text-xs text-red-500">{error}</p>}
        </div>
      ) : (
        /* Collapsed state - floating button with mini visualizer */
        <button
          onClick={() => void startSession()}
          className="group relative flex items-center gap-3 overflow-hidden rounded-full py-3 pl-4 pr-6 shadow-lg transition-all duration-300 hover:scale-105 hover:shadow-xl active:scale-95"
          style={{
            backgroundColor: primaryColor,
            color: "#ffffff",
            boxShadow: `0 8px 32px ${primaryColor}50`,
          }}
        >
          {/* Animated background gradient */}
          <div
            className="absolute inset-0 opacity-30"
            style={{
              background: `linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.3) 50%, transparent 100%)`,
              animation: "shimmer 2s infinite",
            }}
          />

          {/* Mini orb with pulse */}
          <div className="relative h-10 w-10">
            {/* Outer ring */}
            <div
              className="absolute inset-0 animate-spin rounded-full"
              style={{
                background: `conic-gradient(from 0deg, rgba(255,255,255,0.8) 0deg, rgba(255,255,255,0.2) 90deg, rgba(255,255,255,0.8) 180deg, rgba(255,255,255,0.2) 270deg, rgba(255,255,255,0.8) 360deg)`,
                animationDuration: "3s",
              }}
            />
            {/* Inner solid */}
            <div
              className="absolute inset-1 rounded-full"
              style={{ backgroundColor: primaryColor }}
            />
            {/* Center glow */}
            <div className="absolute inset-2 rounded-full bg-white/50 transition-all duration-300 group-hover:bg-white/70" />
            {/* Pulse ring */}
            <div
              className="absolute inset-0 animate-ping rounded-full opacity-20"
              style={{ backgroundColor: "white", animationDuration: "2s" }}
            />
          </div>

          {/* Button text */}
          <span className="relative z-10 text-sm font-semibold">{config.button_text}</span>
        </button>
      )}

      {/* Branding - hide in widget mode since widget shows its own */}
      {!autostart && (
        <div className="mt-3 text-center text-xs" style={{ color: isDark ? "#6b7280" : "#9ca3af" }}>
          Powered by{" "}
          <a
            href="https://voicenoob.com"
            target="_blank"
            rel="noopener noreferrer"
            className="transition-colors duration-200 hover:underline"
            style={{ color: primaryColor }}
          >
            Voice Pro
          </a>
        </div>
      )}

      <style jsx>{`
        @keyframes shimmer {
          0% {
            transform: translateX(-100%);
          }
          100% {
            transform: translateX(100%);
          }
        }
      `}</style>
    </div>
  );
}
