"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";

export function Preloader() {
  const pathname = usePathname();
  const [isVisible, setIsVisible] = useState(true);

  // Skip preloader for embed routes
  const isEmbedRoute = pathname?.startsWith("/embed");

  useEffect(() => {
    // Don't show preloader for embed routes
    if (isEmbedRoute) {
      setIsVisible(false);
      return;
    }

    // Show preloader for minimum time then fade out
    const timer = setTimeout(() => {
      setIsVisible(false);
    }, 2000);

    return () => clearTimeout(timer);
  }, [isEmbedRoute]);

  return (
    <AnimatePresence>
      {isVisible && (
        <motion.div
          key="preloader"
          initial={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.5, ease: [0.4, 0, 0.2, 1] }}
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-black"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.9 }}
            transition={{
              duration: 0.5,
              ease: [0.4, 0, 0.2, 1],
            }}
            className="flex flex-col items-center gap-6"
          >
            {/* Logo */}
            <span
              className="animate-gradient-flow bg-clip-text text-4xl font-bold tracking-tight text-transparent md:text-5xl"
              style={{
                backgroundImage:
                  "linear-gradient(90deg, #e2e8f0, #94a3b8, #e2e8f0, #94a3b8, #e2e8f0)",
                backgroundSize: "200% 100%",
              }}
            >
              Voice Pro
            </span>

            {/* Loading dots */}
            <div className="flex gap-1.5">
              {[0, 1, 2].map((i) => (
                <motion.div
                  key={i}
                  className="h-2 w-2 rounded-full bg-slate-400"
                  animate={{ opacity: [0.3, 1, 0.3] }}
                  transition={{
                    duration: 1,
                    repeat: Infinity,
                    delay: i * 0.2,
                    ease: "easeInOut",
                  }}
                />
              ))}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
