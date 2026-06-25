"use client";

import EmbedPage from "../page";

export default function PreviewPage() {
  return (
    <div className="relative min-h-screen bg-black">
      {/* Voice Pro Logo - centered */}
      <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2">
        <span
          className="animate-gradient-flow bg-clip-text text-3xl font-bold tracking-tight text-transparent"
          style={{
            backgroundImage: "linear-gradient(90deg, #e2e8f0, #94a3b8, #e2e8f0, #94a3b8, #e2e8f0)",
            backgroundSize: "200% 100%",
          }}
        >
          Voice Pro
        </span>
      </div>

      {/* Embed Widget */}
      <EmbedPage />
    </div>
  );
}
